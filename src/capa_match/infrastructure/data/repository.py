"""Read-only local adapter. Replace behind RecordRepository when Helix is available."""
from __future__ import annotations
import json
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from capa_match.infrastructure.contracts import SourceRecord

class SqliteRecordRepository:
    def __init__(self, snapshot: Path):
        self.snapshot = snapshot.resolve(strict=True)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.snapshot.as_uri() + '?mode=ro', uri=True)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    def metadata(self):
        with self._connect() as connection:
            return json.loads(connection.execute('SELECT payload FROM metadata').fetchone()[0])

    def list_records(self, *, offset=0, limit=25, site=None, record_type=None):
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError('offset >= 0 and 1 <= limit <= 100 required')
        clauses, values = [], []
        for name, value in [('site', site), ('record_type', record_type)]:
            if value is not None:
                clauses.append(name + ' = ?')
                values.append(value)
        condition = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        with self._connect() as connection:
            total = connection.execute('SELECT COUNT(*) FROM records' + condition, values).fetchone()[0]
            selected = connection.execute('SELECT pr_id,site,division,record_type,status,title,source_row FROM records' + condition + ' ORDER BY pr_id LIMIT ? OFFSET ?', [*values, limit, offset]).fetchall()
        return {'items': [dict(row) for row in selected], 'total': total,
                'offset': offset, 'limit': limit}

    def get_record(self, pr_id):
        with self._connect() as connection:
            row = connection.execute('SELECT * FROM records WHERE pr_id=?', (pr_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result['fields'] = json.loads(result['fields'])
        result['original'] = json.loads(result['original'])
        return SourceRecord(**result)

    def list_quarantine(self, *, offset=0, limit=25):
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError('offset >= 0 and 1 <= limit <= 100 required')
        with self._connect() as connection:
            rows=connection.execute('SELECT source_row,reason,original FROM quarantine ORDER BY source_row LIMIT ? OFFSET ?', (limit,offset)).fetchall()
            total=connection.execute('SELECT COUNT(*) FROM quarantine').fetchone()[0]
        return {'items':[{**dict(row),'original':json.loads(row['original'])} for row in rows], 'total':total, 'offset':offset, 'limit':limit}
