"""Authorized-scope hybrid candidate retrieval and 70/20/10 record-level ranking."""
from __future__ import annotations

import logging
from typing import Any

import numpy as np

from .core import BM25, MockEmbedder, MPNetEmbedder, jaccard, split_chunks, tokenize, unit_rows, weighted_score

LOG = logging.getLogger(__name__)


class MatchService:
    def __init__(self, *, manifest: dict, records: dict[str, dict], chunks: list[dict],
                 capas: dict[str, list[dict]], collection, embedder,
                 allowed_sites: set[str], candidate_k: int = 100):
        if not allowed_sites:
            raise ValueError('An explicit server-side allowed-sites set is required')
        if candidate_k < 1:
            raise ValueError('candidate_k must be positive')
        self.manifest = manifest
        self.records = records
        self.chunks = chunks
        self.capas = capas
        self.collection = collection
        self.embedder = embedder
        self.allowed_sites = allowed_sites
        self.candidate_k = candidate_k
        self._corpus_caches: dict[tuple[str, ...], tuple[list[dict], BM25]] = {}
        if embedder.model_name != manifest['model_name']:
            raise ValueError(f'Query model mismatch: {embedder.model_name!r} vs indexed {manifest["model_name"]!r}. Rebuild or set MATCH_MODEL_NAME.')
        if manifest.get('embedding_runtime', 'torch') != getattr(embedder, 'runtime', 'mock' if isinstance(embedder, MockEmbedder) else 'torch') and manifest.get('embedding_backend') == 'MPNet':
            raise ValueError('Query embedding runtime must match the indexed runtime')

    def sites(self) -> list[dict]:
        return [{'site': name, 'deviation_count': count}
                for name, count in sorted(self.manifest.get('site_counts', {}).items())
                if name in self.allowed_sites]

    def _authorized_site(self, site: str) -> str:
        if not site or site not in self.allowed_sites:
            raise PermissionError(f'Site {site!r} is not in the server-allowed list')
        if site not in self.manifest.get('site_counts', {}):
            raise ValueError(f'No indexed records for site {site!r}')
        return site

    def _scope_corpus(self, sites: tuple[str, ...]) -> tuple[list[dict], BM25]:
        if sites not in self._corpus_caches:
            selected = set(sites)
            subset = [c for c in self.chunks if c['site'] in selected]
            self._corpus_caches[sites] = (subset, BM25([tokenize(c['doc']) for c in subset]))
        return self._corpus_caches[sites]

    def record(self, pr_id: str, site: str) -> dict:
        site = self._authorized_site(site)
        record = self.records.get(str(pr_id))
        if record is None or record['site'] != site:
            raise KeyError('Record not found in the selected authorized site')
        return {**record, 'related_capas': self.capas.get(record['pr_id'], [])}

    def search(self, query: str, *, site: str | None = None, all_sites: bool = False, top_k: int = 10) -> dict:
        if all_sites:
            if site is not None:
                raise ValueError('Choose a site or all_sites=true, but not both.')
            selected_sites = tuple(item['site'] for item in self.sites())
        else:
            selected_sites = (self._authorized_site(site),)
        scope = {'site': 'All sites' if all_sites else site,
                 'all_sites': all_sites, 'searched_sites': list(selected_sites)}
        allowed_scope = set(selected_sites)
        query = str(query or '').strip()
        if len(query) > 30000:
            raise ValueError('Query must be at most 30000 characters')
        if len(query) < 8 or not tokenize(query):
            raise ValueError('Enter a meaningful deviation description (at least 8 characters)')
        if not 1 <= top_k <= 50:
            raise ValueError('top_k must be from 1 to 50')
        scoped_chunks, bm25 = self._scope_corpus(selected_sites)
        if not scoped_chunks:
            return {'query': query, **scope, 'results': [], 'result_count': 0}
        query_parts = split_chunks(query, self.manifest['chunk_size'], self.manifest['chunk_overlap'])
        query_vectors = unit_rows(self.embedder.encode(query_parts))
        if query_vectors.shape[1] != 768:
            raise ValueError('Incorrect query embedding dimension')
        query_tokens = tokenize(query)
        raw_bm25 = bm25.scores(query_tokens)
        max_bm25 = max(0., float(raw_bm25.max(initial=0)))
        scoped_by_id = {c['chunk_id']: (i, c) for i, c in enumerate(scoped_chunks)}
        candidate_ids: set[str] = set()

        # Both retrieval branches use the same authorized corpus. In all-sites mode,
        # BM25 is normalized over that combined corpus for a comparable ranking.
        site_filter = ({'site': selected_sites[0]} if len(selected_sites) == 1
                       else {'site': {'$in': list(selected_sites)}})
        semantic_results = self.collection.query(
            query_embeddings=query_vectors.tolist(),
            n_results=min(self.candidate_k, len(scoped_chunks)),
            where=site_filter,
            include=['distances', 'metadatas', 'documents'],
        )
        for batch in semantic_results.get('ids', []):
            for chunk_id in batch:
                if chunk_id in scoped_by_id:
                    candidate_ids.add(chunk_id)
        semantic_candidate_count = len(candidate_ids)

        # Independent BM25 candidates, NOT only semantic candidates (key v1 improvement).
        # Only positive BM25 candidates enter, so irrelevant zero-overlap chunks don't crowd out semantic candidates.
        if max_bm25 > 0:
            ranked = np.argsort(-raw_bm25, kind='stable')[:self.candidate_k]
            for i in ranked:
                if raw_bm25[i] > 0:
                    candidate_ids.add(scoped_chunks[int(i)]['chunk_id'])
        keyword_candidate_count = len(candidate_ids) - semantic_candidate_count
        if not candidate_ids:
            return {'query': query, **scope, 'results': [], 'result_count': 0}

        ordered_ids = sorted(candidate_ids)
        vector_result = self.collection.get(ids=ordered_ids, include=['embeddings'])
        embeddings = vector_result.get('embeddings')
        if embeddings is None:
            raise RuntimeError('Chroma did not return stored embeddings for scoring')
        vectors_by_id = {chunk_id: np.asarray(vector, dtype=np.float32)
                         for chunk_id, vector in zip(vector_result['ids'], embeddings)}
        if set(ordered_ids) - set(vectors_by_id):
            raise RuntimeError('Some candidate embeddings are missing from the vector index')

        best_for_record: dict[str, dict] = {}
        best_scores: dict[str, float] = {}
        for chunk_id in ordered_ids:
            doc_idx, chunk = scoped_by_id[chunk_id]
            # Recompute cosine for both semantic and lexical candidate branches so scoring is consistent.
            stored = unit_rows(vectors_by_id[chunk_id])[0]
            semantic = max(0., min(1., float(np.max(query_vectors @ stored))))
            bm25_unit = float(raw_bm25[doc_idx] / max_bm25) if max_bm25 > 0 else 0.
            token_unit = max(jaccard(tokenize(part), tokenize(chunk['doc'])) for part in query_parts)
            score = weighted_score(semantic, bm25_unit, token_unit)
            record = self.records[chunk['pr_id']]
            if record['site'] not in allowed_scope or record['site'] != chunk['site']:
                raise RuntimeError('Candidate outside the selected sites detected; refusing results')
            candidate = {
                'pr_id': record['pr_id'],
                'record_name': record['record_name'],
                'site': record['site'],
                'division': record['division'],
                'dr_type': record['dr_type'],
                'status': record['status'],
                'criticality': record['criticality'],
                'date_opened': record['date_opened'],
                'date_closed': record['date_closed'],
                'responsible_department': record['responsible_department'],
                'matched_chunk_id': chunk_id,
                'matched_field': chunk.get('source_field', 'COMBINED_TEXT'),
                'matched_excerpt': chunk['doc'][:480],
                'semantic_score': round(semantic, 6),
                'bm25_score': round(bm25_unit, 6),
                'jaccard_score': round(token_unit, 6),
                'match_score': round(score, 6),
                'score_percent': round(100 * score, 2),
                'related_capa_count': len(self.capas.get(record['pr_id'], [])),
                'related_capas': self.capas.get(record['pr_id'], []),
            }
            old = best_for_record.get(record['pr_id'])
            if old is None or score > best_scores[record['pr_id']] or (
                score == best_scores[record['pr_id']] and chunk_id < old['matched_chunk_id']
            ):
                best_for_record[record['pr_id']] = candidate
                best_scores[record['pr_id']] = score

        results = sorted(best_for_record.values(), key=lambda r: (-best_scores[r['pr_id']], r['pr_id']))[:top_k]
        return {
            'query': query, **scope,
            'model_name': self.manifest['model_name'],
            'mock_embeddings': self.manifest['embedding_backend'] != 'MPNet',
            'ranking': {'semantic': 0.70, 'bm25': 0.20, 'jaccard': 0.10},
            'candidate_chunks': len(ordered_ids),
            'semantic_candidate_chunks': semantic_candidate_count,
            'keyword_only_candidate_chunks': keyword_candidate_count,
            'result_count': len(results),
            'results': results,
            'disclaimer': 'Scores are relative ranking signals, not probabilities of recurrence.',
        }
