"""Local TrackWise/GTW flat-file ingestion and explicit CAPA parent linkage."""
from __future__ import annotations

import csv
import re
from pathlib import Path

from .core import clean_for_index, normalize_pr_id
from .scope import POLICY_VERSION, workflow_state


def _get(row: dict[str, str], key: str) -> str:
    return str(row.get(key) or '').strip()


def load_gtw(path: str | Path, *, division: str = 'DPD', limit: int = 200,
             site: str | None = None, text_fields: tuple[str, ...] = ('DETAILED_DESCRIPTION',),
             status_scope: str = 'completed'):
    """Select deviations for an explicit division/status policy; retain CAPA links.

    `limit=0` means the entire eligible input corpus. No LLM calls.
    Record selection is independent of DR_TYPE (a business rule still to confirm).
    """
    if limit < 0:
        raise ValueError('limit must be >= 0')
    if status_scope not in {'completed', 'ongoing', 'all'}:
        raise ValueError('Unknown index status scope')
    if not Path(path).is_file():
        raise FileNotFoundError(path)
    if not text_fields:
        raise ValueError('At least one indexed text field is required')
    records: list[dict] = []
    seen: set[str] = set()
    capas_raw: list[dict] = []
    scanned = eligible = invalid_id = duplicate_id = 0
    with open(path, newline='', encoding='utf-8-sig', errors='replace') as fh:
        # Large source narratives can exceed Python's default CSV field limit.
        csv.field_size_limit(2**31 - 1)
        reader = csv.DictReader(fh)
        fields = set(reader.fieldnames or [])
        required = {'PR_ID', 'RECORD_TYPE', 'SITE', 'STATUS', 'PSG_Division'} | set(text_fields)
        missing = required - fields
        if missing:
            raise ValueError(f'GTW CSV is missing required fields: {sorted(missing)}')
        for row in reader:
            scanned += 1
            rec_type = _get(row, 'RECORD_TYPE').casefold()
            if rec_type == 'capa':
                capas_raw.append(row)
                continue
            if rec_type != 'deviation':
                continue
            if status_scope != 'all' and workflow_state(_get(row, 'STATUS')) != status_scope:
                continue
            root_type = _get(row, 'ROOT_PARENT_RECORD_TYPE')
            if root_type and root_type.casefold() != 'deviation':
                continue
            if division and _get(row, 'PSG_Division').casefold() != division.casefold():
                continue
            if site and _get(row, 'SITE') != site:
                continue
            field_texts = {field: clean_for_index(_get(row, field)) for field in text_fields}
            text = '\n\n'.join(t for t in field_texts.values() if t).strip()
            if not text:
                continue
            eligible += 1
            ident = normalize_pr_id(_get(row, 'PR_ID'))
            if not ident:
                invalid_id += 1
                continue
            if ident in seen:
                duplicate_id += 1
                continue
            if limit and len(records) >= limit:
                continue
            seen.add(ident)
            records.append({
                'pr_id': ident,
                'site': _get(row, 'SITE'),
                'division': _get(row, 'PSG_Division'),
                'status': _get(row, 'STATUS'),
                'record_name': _get(row, 'RECORD_NAME'),
                'dr_type': _get(row, 'DR_TYPE'),
                'criticality': _get(row, 'Criticality'),
                'responsible_department': _get(row, 'RESPONSIBLE_DEPARTMENT'),
                'date_opened': _get(row, 'DATE_OPENED'),
                'date_closed': _get(row, 'DATE_CLOSED'),
                'text': text,
                'indexed_field_texts': {name: value for name, value in field_texts.items() if value},
            })
    if not records:
        raise ValueError('No matching historical deviations. Check division/site/status/columns.')

    selected = {r['pr_id'] for r in records}
    links: dict[str, dict[str, dict]] = {pr: {} for pr in selected}
    for row in capas_raw:
        capa_id = normalize_pr_id(_get(row, 'PR_ID'))
        if not capa_id:
            continue
        parents = {normalize_pr_id(_get(row, name)) for name in ('PARENT_ID', 'ROOT_PARENT_ID')}
        parents.discard('')
        for parent in parents & selected:
            links[parent][capa_id] = {
                'pr_id': capa_id,
                'record_name': _get(row, 'RECORD_NAME'),
                'status': _get(row, 'STATUS'),
                'capa_type': _get(row, 'CAPA_TYPE'),
                'date_closed': _get(row, 'DATE_CLOSED'),
                'plan_excerpt': clean_for_index(_get(row, 'CORRECTIONS_AND_CAPA_PLAN'))[:400],
            }
    capas = {k: sorted(v.values(), key=lambda x: x['pr_id']) for k, v in links.items()}
    stats = {
        'source_rows': scanned,
        'eligible_deviation_rows': eligible,
        'indexed_deviations': len(records),
        'skipped_invalid_pr_id': invalid_id,
        'skipped_duplicate_pr_id': duplicate_id,
        'total_capa_rows_in_source': len(capas_raw),
        'linked_capas': sum(len(v) for v in capas.values()),
        'sites': len({r['site'] for r in records}),
        'division_filter': division,
        'site_filter': site,
        'indexed_fields': list(text_fields),
        'status_scope': status_scope,
        'scope_policy_version': POLICY_VERSION,
    }
    return records, capas, stats
