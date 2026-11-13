"""Source contracts: no service-specific eligibility or AI preprocessing here."""
from dataclasses import dataclass
from typing import Any, Protocol

@dataclass(frozen=True)
class SourceRecord:
    pr_id: str
    site: str
    division: str
    record_type: str
    status: str
    title: str
    source_row: int
    fields: dict[str, str]
    original: dict[str, str]

    def summary(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in
                ('pr_id', 'site', 'division', 'record_type', 'status', 'title', 'source_row')}

class RecordRepository(Protocol):
    def metadata(self) -> dict[str, Any]: ...
    def list_records(self, *, offset: int, limit: int, site: str | None = None,
                     record_type: str | None = None) -> dict[str, Any]: ...
    def get_record(self, pr_id: str) -> SourceRecord | None: ...
    def list_quarantine(self, *, offset: int, limit: int) -> dict[str, Any]: ...
