"""Local-only FastAPI search + browser UI. Not enterprise SSO or production RBAC."""
from __future__ import annotations

from functools import lru_cache
import json
import os
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from .contracts import SearchRequest

from .core import MockEmbedder, make_mpnet_embedder
from .index import load_index_metadata, open_chroma
from .search import MatchService

app = FastAPI(title='CAPA.AI MATCH Local v1', version='1.0.0', docs_url='/docs')


@lru_cache(maxsize=1)
def get_service() -> MatchService:
    index_dir = os.environ.get('MATCH_INDEX_DIR', 'local_index')
    allowed_json = os.environ.get('MATCH_ALLOWED_SITES_JSON', '[]')
    try:
        decoded = json.loads(allowed_json)
    except (TypeError, ValueError) as exc:
        raise RuntimeError('MATCH_ALLOWED_SITES_JSON must be a JSON array') from exc
    if not isinstance(decoded, list) or any(not isinstance(site, str) or not site.strip() for site in decoded):
        raise RuntimeError('MATCH_ALLOWED_SITES_JSON must be a JSON array of nonempty site strings')
    allowed = set(decoded)
    if not allowed:
        raise RuntimeError('No sites authorized. Use: python -m match_v1.cli serve --site "SITE NAME"')
    manifest, records, chunks, capas = load_index_metadata(index_dir)
    model = manifest['model_name']
    if manifest['embedding_backend'] == 'MPNet':
        requested = os.environ.get('MATCH_MODEL_NAME', model)
        if requested != model:
            raise RuntimeError('MATCH_MODEL_NAME must exactly match the indexed model')
        embedder = make_mpnet_embedder(model, runtime=manifest.get('embedding_runtime', 'torch'))
    elif manifest['embedding_backend'] == 'MOCK_UNFIT_FOR_REAL_SEARCH':
        embedder = MockEmbedder()
    else:
        raise RuntimeError('Unknown embedding backend. Rebuild the index.')
    if not allowed <= set(manifest['site_counts']):
        raise RuntimeError('An allowed site is absent from the index. Check the sites command.')
    collection = open_chroma(index_dir, manifest=manifest)
    if collection.count() != len(chunks):
        raise RuntimeError('Vector count differs from index metadata. Rebuild the index.')
    return MatchService(manifest=manifest, records=records, chunks=chunks,
                        capas=capas, collection=collection,
                        embedder=embedder, allowed_sites=allowed)


def service_dependency() -> MatchService:
    try:
        return get_service()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get('/api/health')
def health(service: MatchService = Depends(service_dependency)):
    return {'ready': True, 'model_name': service.manifest['model_name'],
            'embedding_backend': service.manifest['embedding_backend'],
            'embedding_runtime': service.manifest.get('embedding_runtime', 'torch'),
            'records': len(service.records), 'chunks': len(service.chunks),
            'text_fields': service.manifest.get('text_fields', ['DETAILED_DESCRIPTION']),
            'allowed_sites': service.sites(),
            'warning': 'Local development only; no enterprise authentication.'}


@app.get('/api/sites')
def sites(service: MatchService = Depends(service_dependency)):
    return {'sites': service.sites()}


@app.post('/api/search')
def search(request: SearchRequest, service: MatchService = Depends(service_dependency)):
    try:
        return service.search(request.query, site=request.site, all_sites=request.all_sites, top_k=request.top_k)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get('/api/records/{pr_id}')
def record(pr_id: str, site: str, service: MatchService = Depends(service_dependency)):
    try:
        return service.record(pr_id, site)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
