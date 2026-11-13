"""Explicit search scope shared by the standalone and integrated MATCH APIs."""
from pydantic import BaseModel, Field, model_validator


class SearchRequest(BaseModel):
    query: str = Field(min_length=8, max_length=30000)
    site: str | None = Field(default=None, min_length=1)
    all_sites: bool = Field(default=False, description='Search all indexed sites allowed by the server.')
    top_k: int = Field(default=10, ge=1, le=50)

    @model_validator(mode='after')
    def require_one_scope(self):
        if self.all_sites == (self.site is not None):
            raise ValueError('Choose a site or all_sites=true, but not both.')
        return self
