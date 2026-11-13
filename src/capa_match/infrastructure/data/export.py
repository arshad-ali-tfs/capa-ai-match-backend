"""Compatibility boundary: preserve original column names/text for existing flow code."""
import csv
import json
from pathlib import Path
import sqlite3
from .repository import SqliteRecordRepository


def export_csv(repository: SqliteRecordRepository, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with repository._connect() as db, destination.open('w', encoding='utf-8-sig', newline='') as out:
        rows = db.execute('SELECT original FROM records ORDER BY source_row')
        writer = None
        for row in rows:
            original = json.loads(row[0])
            if writer is None:
                writer = csv.DictWriter(out, fieldnames=list(original))
                writer.writeheader()
            writer.writerow(original)
    return destination
