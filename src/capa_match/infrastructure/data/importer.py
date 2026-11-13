"""Stream a CSV/XLSX export into an immutable, indexed local source snapshot."""
from __future__ import annotations
import csv
from contextlib import closing
from datetime import date, datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Iterator

SCHEMA_VERSION = 'source-v1'
REQUIRED = {'pr_id', 'site', 'record_type'}


def field_name(value: str) -> str:
    return re.sub(r'[^a-z0-9]+', '_', value.strip().lower()).strip('_')


def text(value) -> str:
    if value is None:
        return ''
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError('Non-finite number in workbook')
        return str(int(value)) if value.is_integer() else str(value)
    return str(value)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def rows(path: Path, sheet: str | None) -> Iterator[tuple[int, dict[str, str]]]:
    """Original source text is retained. XLSX formula cells are refused; the importer never evaluates formulas."""
    def convert(headers, values):
        if not headers or any(not x for x in headers):
            raise ValueError('Every source column needs a nonempty header')
        names = [field_name(x) for x in headers]
        if len(set(names)) != len(names):
            raise ValueError('Source headers collide after normalisation')
        if not REQUIRED <= set(names):
            raise ValueError('Missing source columns: ' + ', '.join(sorted(REQUIRED-set(names))))
        if len(values) > len(headers):
            raise ValueError('Row contains more values than column headers')
        return dict(zip(headers, [text(v) for v in values] + ['']*(len(headers)-len(values))))

    if path.suffix.lower() == '.csv':
        if sheet is not None:
            raise ValueError('--sheet applies to XLSX files only')
        csv.field_size_limit(2**31 - 1)
        with path.open(encoding='utf-8-sig', newline='') as handle:
            reader = csv.reader(handle, strict=True)
            headers = next(reader, [])
            convert(headers, [])
            for number, values in enumerate(reader, 2):
                yield number, convert(headers, values)
    elif path.suffix.lower() == '.xlsx':
        from openpyxl import load_workbook
        workbook = load_workbook(path, read_only=True, data_only=False)
        try:
            if sheet is None and len(workbook.sheetnames) != 1:
                raise ValueError('Workbook has multiple sheets; specify --sheet explicitly')
            worksheet = workbook[sheet] if sheet else workbook[workbook.sheetnames[0]]
            iterator = iter(worksheet.iter_rows())
            headers = [text(c.value) for c in next(iterator, ())]
            convert(headers, [])
            for number, cells in enumerate(iterator, 2):
                if any(c.data_type == 'f' for c in cells):
                    raise ValueError('Formula cells require an export containing values, not formulas')
                yield number, convert(headers, [c.value for c in cells])
        finally:
            workbook.close()
    else:
        raise ValueError('Supported source formats: .csv and .xlsx')


def import_snapshot(source: Path, output: Path, *, sheet: str | None = None) -> Path:
    source = source.resolve(strict=True)
    source_hash = sha256(source)
    identity = hashlib.sha256((SCHEMA_VERSION + ':' + source_hash + ':' + (sheet or '')).encode()).hexdigest()
    output.mkdir(parents=True, exist_ok=True)
    destination = output / (identity + '.sqlite3')
    if destination.exists():
        with closing(sqlite3.connect(destination.as_uri()+'?mode=ro', uri=True)) as database:
            metadata = json.loads(database.execute('SELECT payload FROM metadata').fetchone()[0])
        if metadata['snapshot_id'] != identity:
            raise ValueError('Existing snapshot identity mismatch')
        return destination
    descriptor, temporary = tempfile.mkstemp(prefix='import-', suffix='.sqlite3', dir=output)
    os.close(descriptor)
    database = sqlite3.connect(temporary)
    try:
        database.executescript('''
        CREATE TABLE records (pr_id TEXT PRIMARY KEY, site TEXT NOT NULL, division TEXT NOT NULL,
            record_type TEXT NOT NULL, status TEXT NOT NULL, title TEXT NOT NULL,
            source_row INTEGER NOT NULL, fields TEXT NOT NULL, original TEXT NOT NULL);
        CREATE TABLE quarantine (source_row INTEGER NOT NULL, reason TEXT NOT NULL, original TEXT NOT NULL);
        CREATE TABLE metadata (payload TEXT NOT NULL);
        CREATE INDEX record_filter ON records(site, record_type, pr_id);
        ''')
        total = accepted = rejected = 0
        reasons: dict[str, int] = {}
        for number, original in rows(source, sheet):
            total += 1
            fields = {field_name(k): v for k, v in original.items()}
            ident = fields['pr_id'].strip()
            reason = ''
            if not ident:
                reason = 'missing_pr_id'
            elif not fields['site'].strip() or not fields['record_type'].strip():
                reason = 'missing_site_or_record_type'
            elif database.execute('SELECT 1 FROM records WHERE pr_id=?', (ident,)).fetchone():
                reason = 'duplicate_pr_id'
            if reason:
                database.execute('INSERT INTO quarantine VALUES (?,?,?)', (number, reason, json.dumps(original, ensure_ascii=False)))
                rejected += 1
                reasons[reason] = reasons.get(reason, 0) + 1
                continue
            database.execute('INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?)',
                (ident, fields['site'].strip(), fields.get('psg_division', '').strip(),
                 fields['record_type'].strip(), fields.get('status', '').strip(),
                 fields.get('record_name', ''), number,
                 json.dumps(fields, ensure_ascii=False), json.dumps(original, ensure_ascii=False)))
            accepted += 1
        if sha256(source) != source_hash:
            raise ValueError('Source changed during import; no snapshot published')
        assert total == accepted + rejected
        metadata = {'snapshot_id': identity, 'schema_version': SCHEMA_VERSION,
                    'source_sha256': source_hash, 'source_file': source.name, 'sheet': sheet,
                    'source_rows': total, 'accepted_rows': accepted, 'quarantined_rows': rejected,
                    'quarantine_reasons': reasons, 'duplicate_policy': 'first retained; later occurrence quarantined',
                    'imported_at': datetime.now(timezone.utc).isoformat()}
        database.execute('INSERT INTO metadata VALUES (?)', (json.dumps(metadata),))
        database.commit()
        database.close()
        # Refuse to overwrite an immutable published snapshot, even with simultaneous importers.
        try:
            os.link(temporary, destination)
        except FileExistsError:
            pass
        return destination
    finally:
        database.close()
        Path(temporary).unlink(missing_ok=True)
