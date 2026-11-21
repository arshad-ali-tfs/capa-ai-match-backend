"""Explicit search scope shared by the standalone and integrated MATCH APIs."""
from datetime import date
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class SearchFilters(BaseModel):
    model_config = ConfigDict(extra='forbid')
    divisions: list[str] = Field(default_factory=list, max_length=50)
    statuses: list[str] = Field(default_factory=list, max_length=50)
    record_scope: Literal['completed', 'ongoing', 'all'] = 'completed'
    include_cancelled: bool = False
    years: list[int] = Field(default_factory=list, max_length=100)
    criticalities: list[str] = Field(default_factory=list, max_length=20)
    dr_types: list[str] = Field(default_factory=list, max_length=50)
    departments: list[str] = Field(default_factory=list, max_length=100)
    date_from: date | None = None
    date_to: date | None = None
    capa: Literal['any', 'linked', 'completed', 'none'] = 'any'

    @model_validator(mode='after')
    def valid_values(self):
        if any(not 1900 <= year <= 2200 for year in self.years):
            raise ValueError('Opened years must be between 1900 and 2200.')
        for values in (self.divisions, self.statuses, self.criticalities, self.dr_types, self.departments):
            if any(not value.strip() or len(value) > 200 for value in values):
                raise ValueError('Filter values must contain 1–200 characters.')
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError('Opened from must be on or before opened to.')
        return self


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query: str = Field(default='', max_length=30000)
    site: str | None = Field(default=None, min_length=1)
    all_sites: bool = Field(default=False, description='Search all indexed sites allowed by the server.')
    top_k: int = Field(default=10, ge=1, le=50)
    filters: SearchFilters = Field(default_factory=SearchFilters)
    mode: Literal['auto', 'keyword', 'exact', 'semantic'] = 'auto'
    offset: int = Field(default=0, ge=0, le=1000000)
    assist: bool = False

    @property
    def use_explorer(self):
        # Existing narrative clients (including AUTHOR) retain hybrid scores.
        return self.assist or bool({'mode', 'filters', 'offset'} & self.model_fields_set) or len(self.query.strip()) < 8

    @model_validator(mode='after')
    def require_one_scope(self):
        if self.all_sites == (self.site is not None):
            raise ValueError('Choose a site or all_sites=true, but not both.')
        return self
