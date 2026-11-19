"""Read-only, authorized record exploration around the retained hybrid ranker.

Structured filters and literal identifiers constrain retrieval before ranking.
Exact searches use full retained fields, so chunk boundaries cannot hide an ID.
"""
from __future__ import annotations

from collections import Counter
from datetime import date
from functools import lru_cache
from threading import Lock
import re
import numpy as np

from .contracts import SearchFilters
from .core import split_chunks, unit_rows

FIELDS = {'years': None, 'criticalities': 'criticality', 'dr_types': 'dr_type',
          'departments': 'responsible_department'}
TITLE_WEIGHT = .30
CODE = re.compile(r'(?<![A-Za-z0-9])[A-Za-z0-9]+(?:[-_./\u2010-\u2015][A-Za-z0-9]+)*(?![A-Za-z0-9])')
NON_CODES = {'jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'sept', 'oct', 'nov', 'dec',
             'january', 'february', 'march', 'april', 'june', 'july', 'august', 'september', 'october',
            'november', 'december', 'ph', 'ml', 'mg', 'mm', 'nm', 'cm', 'ppm', 'ppb', 'min', 'hr',
            'at', 'on', 'in', 'of', 'for', 'to', 'from', 'after', 'before', 'about', 'by', 'and', 'or',
            'was', 'is', 'a', 'an', 'the', 'temperature', 'temp', 'day', 'days', 'week', 'year'}
SPACED_CODES = {'eq', 'eqp', 'equipment', 'hplc', 'uplc', 'gc', 'lc', 'mfg', 'ins', 'pt', 'room', 'line', 'id'}
SPACED_CODE = re.compile(r'\b(?:' + '|'.join(sorted(SPACED_CODES, key=len, reverse=True)) + r')\s+\d+(?:[-_./][A-Za-z0-9]+)*\b', re.I)


@lru_cache(maxsize=8192)
def opened_date(value):
    try:
        return date.fromisoformat(str(value or '')[:10])
    except ValueError:
        return None


def literal_pattern(value):
    """Whole literal tokens; separator variations preserve every letter/digit."""
    parts = re.findall(r'[A-Za-z]+|[0-9]+', value)
    if not parts:
        return re.compile(r'(?!)')
    middle = re.escape(parts[0])
    for previous, current in zip(parts, parts[1:]):
        # Keep numeric/alpha group boundaries: SOP-12-34 is not SOP-1234.
        separator = r'[-_./\u2010-\u2015\s]+' if previous.isdigit() == current.isdigit() else r'[-_./\u2010-\u2015\s]*'
        middle += separator + re.escape(current)
    return re.compile(r'(?<![A-Za-z0-9])(?<![A-Za-z0-9][-_/.])' + middle + r'(?![A-Za-z0-9]|[-_/.][A-Za-z0-9])', re.I)


def phrase_pattern(value):
    return re.compile(r'(?<!\w)' + r'\s+'.join(re.escape(part) for part in value.split()) + r'(?!\w)', re.I)


def identifiers(query):
    result = []
    matches = sorted([*CODE.finditer(query), *SPACED_CODE.finditer(query)], key=lambda match: (match.start(), -len(match.group())))
    previous_end = -1
    for match in matches:
        if match.start() < previous_end:
            continue
        value = match.group()
        if not re.search('[A-Za-z]', value) or not re.search('[0-9]', value) or len(value) > 100:
            continue
        prefix = re.search(r'[A-Za-z]+', value)[0].lower()
        if value[0].isdigit() and prefix in {'am', 'pm', 'st', 'nd', 'rd', 'th', 'f', 'c'}:
            continue
        if prefix not in NON_CODES and value.lower() not in {item.lower() for item in result}:
            result.append(value)
            previous_end = match.end()
    return result[:10]


def excerpt(text, patterns, length=480):
    hits = sorted((match.start(), match.end()) for pattern in patterns for match in pattern.finditer(text))
    start = max(0, hits[0][0] - 110) if hits else 0
    start = min(start, max(0, len(text) - length))
    value = text[start:start + length]
    spans = []
    for left, right in hits:
        if start <= left < right <= start + len(value):
            span = {'start': left - start, 'end': right - start}
            if not spans or span['start'] >= spans[-1]['end']:
                spans.append(span)
    return value, spans[:40]


class MatchExplorer:
    def __init__(self, service):
        self.service = service
        self.documents = {}
        self.chunks_by_id = {chunk['chunk_id']: chunk for chunk in service.chunks}
        self.title_vectors = {}
        self._additional_loaded = False
        self._source_lock = Lock()
        for key, record in service.records.items():
            fields = record.get('indexed_field_texts') or {'COMBINED_TEXT': record.get('text', '')}
            self.documents[key] = [('RECORD_NAME', record.get('record_name', '')),
                                   *[(name, text) for name, text in fields.items() if text]]

    def ensure_source_documents(self):
        if self._additional_loaded or self.service.additional_documents_loader is None:
            return
        with self._source_lock:
            if self._additional_loaded:
                return
            for key, fields in self.service.additional_documents_loader().items():
                if key in self.documents:
                    existing = {name for name, _ in self.documents[key]}
                    self.documents[key].extend((name, text) for name, text in fields.items() if name not in existing and text)
            self._additional_loaded = True

    def scope(self, site, all_sites):
        if all_sites:
            if site is not None:
                raise ValueError('Choose a site or all_sites=true, but not both.')
            sites = [item['site'] for item in self.service.sites()]
        else:
            sites = [self.service._authorized_site(site)]
        records = [record for record in self.service.records.values() if record['site'] in sites]
        return records, {'site': 'All sites' if all_sites else site,
                         'all_sites': all_sites, 'searched_sites': sites}

    def filters(self, values):
        value = SearchFilters.model_validate(values or {}).model_dump(mode='json')
        for key in FIELDS:
            value[key] = sorted(set(item.strip() if isinstance(item, str) else item for item in value[key]))
        return value

    def accepts(self, record, filters, omit=None):
        opened = opened_date(record.get('date_opened'))
        for key, field in FIELDS.items():
            if key == omit or not filters[key]:
                continue
            value = opened.year if key == 'years' and opened else None if key == 'years' else record.get(field, '').strip()
            choices = filters[key]
            if key != 'years':
                value, choices = value.casefold(), [item.casefold() for item in choices]
            if value not in choices:
                return False
        if filters['date_from'] and (not opened or opened.isoformat() < filters['date_from']):
            return False
        if filters['date_to'] and (not opened or opened.isoformat() > filters['date_to']):
            return False
        if omit != 'capa':
            capas = self.service.capas.get(record['pr_id'], [])
            completed = any(re.fullmatch(r'closed\s*-\s*done', item.get('status', '').strip(), re.I) for item in capas)
            if (filters['capa'] == 'linked' and not capas or filters['capa'] == 'none' and capas
                    or filters['capa'] == 'completed' and not completed):
                return False
        return True

    def facets(self, *, site=None, all_sites=False, filters=None):
        records, scope = self.scope(site, all_sites)
        filters = self.filters(filters)
        eligible = [record for record in records if self.accepts(record, filters)]
        facets = {}
        for key, field in FIELDS.items():
            counts = Counter()
            for record in records:
                if self.accepts(record, filters, omit=key):
                    opened = opened_date(record.get('date_opened'))
                    value = opened.year if key == 'years' and opened else None if key == 'years' else record.get(field, '').strip()
                    if value is not None and value != '':
                        counts[value] += 1
            facets[key] = [{'value': value, 'count': counts[value]} for value in sorted(counts, reverse=key == 'years')]
        return {**scope, 'filters': filters, 'facets': facets, 'scope_record_count': len(records),
                'eligible_record_count': len(eligible),
                'missing_opened_dates': sum(opened_date(record.get('date_opened')) is None for record in eligible)}

    def best_text(self, record, patterns):
        documents = self.documents.get(record['pr_id'], [])
        field, text = max(documents, key=lambda item: (sum(bool(pattern.search(item[1])) for pattern in patterns), bool(item[1]), item[0] != 'RECORD_NAME'), default=('COMBINED_TEXT', ''))
        value, spans = excerpt(text, patterns)
        return field, value, spans

    def row(self, record, kind, patterns, reasons):
        field, value, spans = self.best_text(record, patterns)
        capas = self.service.capas.get(record['pr_id'], [])
        return {**{key: record.get(key, '') for key in ('pr_id', 'record_name', 'site', 'division', 'dr_type', 'status', 'criticality',
                                                       'date_opened', 'date_closed', 'responsible_department')},
                'matched_field': field, 'matched_excerpt': value, 'matched_spans': spans,
                'match_kind': kind, 'match_reasons': reasons, 'match_score': None,
                'semantic_score': None, 'bm25_score': None, 'jaccard_score': None, 'score_percent': None,
                'related_capa_count': len(capas), 'related_capas': capas}

    @staticmethod
    def recent(record):
        opened = opened_date(record.get('date_opened'))
        return (-(opened.toordinal() if opened else 0), record['pr_id'])

    def rerank(self, rows, query_vectors):
        titles = {row['pr_id']: row.get('record_name', '').strip() for row in rows if row.get('record_name', '').strip()}
        missing = [(key, text) for key, text in titles.items() if (key, text) not in self.title_vectors]
        if missing:
            vectors = unit_rows(self.service.embedder.encode([text for _, text in missing]))
            if vectors.shape != (len(missing), 768):
                raise RuntimeError('Event-title embeddings do not match the query model.')
            self.title_vectors.update({(key, text): vector for (key, text), vector in zip(missing, vectors)})
        for row in rows:
            vector = self.title_vectors.get((row['pr_id'], titles.get(row['pr_id'], '')))
            title_score = float(np.clip(np.max(query_vectors @ vector), 0, 1)) if vector is not None else None
            row['event_title_score'] = round(title_score, 6) if title_score is not None else None
            row['relevance_score'] = round((1 - TITLE_WEIGHT) * row['match_score'] + TITLE_WEIGHT * title_score, 6) if title_score is not None else row['match_score']
            if title_score is not None:
                row['match_reasons'].append('The recorded event title was also compared with the query')
        return sorted(rows, key=lambda row: (-row['relevance_score'], -row['match_score'], row['pr_id']))

    def search(self, query='', *, site=None, all_sites=False, top_k=10, filters=None, mode='auto', offset=0):
        records, scope = self.scope(site, all_sites)
        filters = self.filters(filters)
        if mode not in {'auto', 'keyword', 'exact', 'semantic'}:
            raise ValueError('Unknown search mode.')
        if not 1 <= top_k <= 50 or not 0 <= offset <= 1000000:
            raise ValueError('Invalid result limit or page offset.')
        query = str(query or '').strip()
        if len(query) > 30000:
            raise ValueError('Query must be at most 30000 characters.')
        eligible = [record for record in records if self.accepts(record, filters)]
        source_ids = {self.service.source_ids.get(record['pr_id'], record['pr_id']): record['pr_id'] for record in records}
        prefix = re.fullmatch(r'(?:PR(?:_ID)?|deviation(?:\s+ID)?)(?:\s*[:#-]\s*|\s+)([A-Za-z0-9._-]+)|PR(\d+)', query, re.I)
        id_value = query if query in source_ids else next(value for value in prefix.groups() if value is not None) if prefix else query
        id_lookup = mode in {'auto', 'exact'} and (query in source_ids or prefix is not None or query.isdigit())
        required_ids = identifiers(query) if mode in {'auto', 'keyword'} and len(query.split()) <= 12 and not id_lookup else []
        if query and not id_lookup and (required_ids or mode in {'keyword', 'exact'} or mode == 'auto' and (len(query.split()) <= 2 or query.startswith('"'))):
            self.ensure_source_documents()
        patterns = [literal_pattern(value) for value in required_ids]
        identity_matches = [record for record in eligible if all(any(pattern.search(text) for _, text in self.documents[record['pr_id']]) for pattern in patterns)]
        words = re.findall(r'[A-Za-z0-9]+', query)
        if query and not id_lookup and (not words or len(''.join(words)) < 2):
            raise ValueError('Enter a complete identifier or at least two letters/numbers, or leave search empty to browse.')
        quoted = len(query) > 1 and query.startswith('"') and query.endswith('"')
        only_ids = bool(required_ids) and re.sub(r'[^A-Za-z0-9]', '', query).casefold() == ''.join(re.sub(r'[^A-Za-z0-9]', '', value).casefold() for value in required_ids)
        resolved = ('browse' if not query else 'record_id' if id_lookup else 'identifier' if only_ids
                    else 'exact' if mode == 'exact' or quoted else 'keyword' if mode == 'keyword' or mode == 'auto' and len(words) <= 2
                    else 'semantic' if mode == 'semantic' else 'hybrid')
        envelope = {'query': query, **scope, 'filters': filters, 'requested_mode': mode, 'search_mode': resolved,
                    'scope_record_count': len(records), 'eligible_record_count': len(eligible),
                    'required_identifiers': required_ids, 'identifier_record_count': len(identity_matches) if required_ids else None,
                    'offset': offset, 'limit': top_k, 'model_name': self.service.manifest['model_name'],
                    'mock_embeddings': self.service.manifest['embedding_backend'] != 'MPNet'}
        reasons = [f'Identifier {value} found in retained source text' for value in required_ids]
        if resolved in {'hybrid', 'semantic'}:
            if offset:
                raise ValueError('Pagination is available for browse, keyword and exact searches; change the result limit for ranked searches.')
            selected_ids = {record['pr_id'] for record in identity_matches}
            # Restrict both vector retrieval and BM25 before candidates are selected.
            restricted = None if selected_ids == {record['pr_id'] for record in records} else selected_ids
            parts = split_chunks(query, self.service.manifest['chunk_size'], self.service.manifest['chunk_overlap'])
            query_vectors = unit_rows(self.service.embedder.encode(parts)) if selected_ids else None
            result = self.service._hybrid_search(query, site=site, all_sites=all_sites, top_k=min(50, max(top_k, top_k * 3)),
                                                 record_ids=restricted, semantic_only=resolved == 'semantic',
                                                 encoded_query=query_vectors) if selected_ids else {'results': [], 'result_count': 0}
            for row in result['results']:
                row['match_kind'] = resolved
                row['match_reasons'] = reasons + (['Related meaning in indexed text'] if resolved == 'semantic' else ['Ranked by meaning, keyword relevance and word overlap'])
                chunk = self.chunks_by_id.get(row.get('matched_chunk_id'))
                row['matched_excerpt'], row['matched_spans'] = excerpt(chunk['doc'] if chunk else row['matched_excerpt'], [phrase_pattern(word) for word in words[:32]])
            result['results'] = self.rerank(result['results'], query_vectors)[:top_k] if result['results'] else []
            result['result_count'] = len(result['results'])
            explanation = f'Relevance combines {(1 - TITLE_WEIGHT) * 100:.0f}% narrative rank and {TITLE_WEIGHT * 100:.0f}% recorded event-title meaning. '
            explanation += ('Narrative rank uses semantic similarity. ' if resolved == 'semantic' else 'Narrative rank combines meaning (70%), keyword relevance (20%) and word overlap (10%). ')
            explanation += 'When no event title is available, the narrative rank is used alone. Scores prioritize review within the filtered population; they are not probabilities of correctness or recurrence.'
            return {**result, **envelope, 'total_matches': None, 'has_more': False, 'ranking_explanation': explanation}
        if resolved == 'record_id':
            selected = [record for record in eligible if record['pr_id'] == source_ids.get(id_value)]
            patterns, reasons = [], ['Exact deviation ID']
        elif resolved == 'identifier':
            selected = identity_matches
        elif resolved == 'browse':
            selected, patterns, reasons = eligible, [], ['Matches the applied filters']
        else:
            if resolved == 'exact':
                term = query[1:-1].strip() if quoted else query
                if not term:
                    raise ValueError('Enter a nonempty phrase inside quotes.')
                patterns = [phrase_pattern(term)]
                reasons = ['Exact phrase found in retained source text']
            else:
                if len(words) > 32:
                    raise ValueError('Keyword searches support up to 32 words. Use Smart or Meaning for a full narrative.')
                remaining = query
                for pattern in patterns:
                    remaining = pattern.sub(' ', remaining)
                patterns = patterns + [phrase_pattern(word) for word in dict.fromkeys(re.findall(r'[A-Za-z0-9]+', remaining))]
                reasons += ['All query words found in retained source text']
            selected = [record for record in identity_matches if all(any(pattern.search(text) for _, text in self.documents[record['pr_id']]) for pattern in patterns)]
        selected.sort(key=self.recent)
        results = [self.row(record, resolved, patterns, reasons) for record in selected[offset:offset + top_k]]
        return {**envelope, 'results': results, 'result_count': len(results), 'total_matches': len(selected),
                'has_more': offset + len(results) < len(selected)}
