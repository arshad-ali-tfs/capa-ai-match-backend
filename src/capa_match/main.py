from datetime import date
from .cli import configure

configure()

import os
import json
import logging
from threading import Lock, Thread
from pathlib import Path, PurePosixPath, PureWindowsPath
from functools import lru_cache
from contextlib import asynccontextmanager
from pydantic import BaseModel, ConfigDict, Field
from fastapi import HTTPException, Query
from typing import Literal
from match_v1.scope import status_counts
from match_v1.contracts import SearchRequest, SearchFilters
from match_v1.search import SiteAccessError
from capa_match.infrastructure.web import create_service
from capa_match.infrastructure.storage import local_directory
from capa_match.intelligence import SearchIntelligence, OverviewRequest, AssistanceUnavailable
from capa_match import cases
from capa_match.provenance import index_generation

app = create_service('match')
_warmup_lock = Lock()


@lru_cache(maxsize=1)
def assistant():
    return SearchIntelligence()


def query_model_state(service):
    """Prepare the complete local retrieval path while filters are chosen."""
    if service.manifest['embedding_backend'] != 'MPNet':
        return 'ready'
    model = service.embedder
    with _warmup_lock:
        state = getattr(model, '_match_warmup_state', None)
        if state is not None:
            return state
        model._match_warmup_state = 'warming'
        def prepare():
            try:
                from .warmup import prepare_search
                prepare_search(service)
                model._match_warmup_state = 'ready'
            except Exception as error:
                model._match_warmup_state = 'unavailable'
                logging.getLogger(__name__).warning('MATCH query-model warm-up failed (%s).', type(error).__name__)
        Thread(target=prepare, name='match-query-model-warmup', daemon=True).start()
        return model._match_warmup_state


def prepare_local_search():
    if os.getenv('MATCH_WARM_SEARCH', '1') == '0':
        return
    try:
        query_model_state(engine())
    except HTTPException:
        # Health/source inspection remains available when assets need setup.
        logging.getLogger(__name__).warning('MATCH search warm-up awaits valid local assets.')


@asynccontextmanager
async def search_lifespan(application):
    prepare_local_search()
    yield


app.router.lifespan_context = search_lifespan

@lru_cache(maxsize=1)
def engine():
    from match_v1.api import get_service
    try:
        os.environ['MATCH_INDEX_DIR'] = str(local_directory(os.environ['MATCH_INDEX_DIR']))
        manifest = json.loads((Path(os.environ['MATCH_INDEX_DIR']) / 'manifest.json').read_text(encoding='utf8'))
        from match_v1.core import TEXT_NORMALIZATION_VERSION
        if manifest.get('source_statistics',{}).get('text_normalization_version')!=TEXT_NORMALIZATION_VERSION:
            raise HTTPException(503,detail='The search index needs rebuilding to preserve scientific source text. Rebuild the MATCH index before searching.')
        from capa_match.infrastructure.data.repository import SqliteRecordRepository
        from capa_match.infrastructure.storage import local_file
        repository = SqliteRecordRepository(local_file(os.environ['CAPA_SOURCE_SNAPSHOT']))
        snapshot = repository.metadata()['snapshot_id']
        if manifest.get('source_snapshot_id') != snapshot:
            raise ValueError('MATCH index belongs to a different source snapshot. Rebuild the index.')
        from .identifiers import SourceIdentifiers, SnapshotMatchService
        return SnapshotMatchService(get_service(), SourceIdentifiers(repository))
    except (OSError, ValueError, RuntimeError, KeyError, ImportError) as error:
        raise HTTPException(503, detail='MATCH index or model configuration is unavailable.') from error

def public_model_name(value):
    model = str(value)
    # A local checkpoint can be configured; its host directory is not UI metadata.
    if PurePosixPath(model).is_absolute() or PureWindowsPath(model).is_absolute():
        model = model.replace('\\', '/').rstrip('/').rsplit('/', 1)[-1]
    return model


@app.get('/api/v1/match/index')
def index_metadata():
    """Index readiness and public coverage, without filesystem or source details."""
    service = engine()
    manifest = service.manifest
    allowed_sites = service.sites()
    return {
        'ready': True,
        'semantic_state': query_model_state(service),
        'model_name': public_model_name(manifest['model_name']),
        'embedding_backend': manifest['embedding_backend'],
        'embedding_runtime': manifest.get('embedding_runtime', 'torch'),
        'mock_embeddings': manifest['embedding_backend'] != 'MPNet',
        'status_counts': status_counts(record for record in service.records.values() if record['site'] in service.allowed_sites),
        'records': len(service.records),
        'chunks': len(service.chunks),
        'searchable_records': sum(item['deviation_count'] for item in allowed_sites),
        'division_scope': sorted({record.get('division', '') for record in service.records.values()
                                  if record['site'] in service.allowed_sites and record.get('division')}),
        'allowed_sites': allowed_sites,
        'text_fields': manifest.get('text_fields') or ['DETAILED_DESCRIPTION'],
        'source_snapshot_id': manifest.get('source_snapshot_id'),
        'ranking': {'semantic': 0.70, 'bm25': 0.20, 'jaccard': 0.10},
        'disclaimer': 'Scores are relative ranking signals, not probabilities of recurrence.',
        'warning': 'Site access uses the server allowed-site configuration. Local mode does not provide enterprise authentication.' if os.getenv('CAPA_ENV', 'local') == 'local' else None,
        'ai_assistance_configured': assistant().llm.configured,
        'progressive_search': True,
        'text_normalization_version': manifest.get('source_statistics',{}).get('text_normalization_version'),
        'index_generation': index_generation(manifest),
    }


@app.get('/api/v1/match/sites')
def sites():
    return {'sites': engine().sites()}

@app.post('/api/v1/match/search')
def search(request: SearchRequest):
    try:
        if request.assist:
            result = assistant().search(engine(), request)
        elif not request.use_explorer:
            result = engine().search_legacy(request.query, site=request.site, all_sites=request.all_sites, top_k=request.top_k)
        else:
            result = engine().search(request.query, site=request.site, all_sites=request.all_sites, top_k=request.top_k,
                                     filters=request.filters.model_dump(mode='json'), mode=request.mode, offset=request.offset)
        if 'model_name' in result:
            result = {**result, 'model_name': public_model_name(result['model_name'])}
        return result
    except SiteAccessError as error:
        raise HTTPException(403, detail='Site is not authorised.') from error
    except ValueError as error:
        raise HTTPException(400, detail=str(error)) from error
    except (OSError, RuntimeError) as error:
        raise HTTPException(503, detail='MATCH search is unavailable. Check the index and model configuration.') from error


@app.post('/api/v1/match/overview')
def evidence_overview(request: OverviewRequest):
    try:
        return assistant().overview(engine(), request.search_id)
    except KeyError as error:
        raise HTTPException(404, detail='This search overview expired. Search again to prepare it.') from error
    except SiteAccessError as error:
        raise HTTPException(403, detail='Site is not authorised.') from error
    except (AssistanceUnavailable, OSError, RuntimeError, ValueError) as error:
        raise HTTPException(503, detail='The AI evidence overview is unavailable. You can still review the source results.') from error


class RelevanceRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    review_id: str = Field(pattern=r'^[a-f0-9]{32}$')


@app.post('/api/v1/match/review')
def relevance_review(request: RelevanceRequest):
    try:
        return assistant().performance.review(engine(), request.review_id)
    except KeyError as error:
        raise HTTPException(404, detail='This relevance review expired. Search again.') from error
    except SiteAccessError as error:
        raise HTTPException(403, detail='Site is not authorised.') from error
    except (OSError, RuntimeError, ValueError) as error:
        raise HTTPException(503, detail='Relevance review is unavailable. Initial source candidates remain available.') from error


@app.post('/api/v1/match/facets')
def filter_options(request: SearchRequest):
    try:
        return engine().facets(site=request.site, all_sites=request.all_sites,
                               filters=request.filters.model_dump(mode='json'))
    except SiteAccessError as error:
        raise HTTPException(403, detail='Site is not authorised.') from error
    except ValueError as error:
        raise HTTPException(400, detail=str(error)) from error
    except (OSError, RuntimeError) as error:
        raise HTTPException(503, detail='MATCH filter options are unavailable.') from error

@lru_cache(maxsize=1)
def watchlist_service():
    from .watchlist import Watchlist
    return Watchlist()


@app.get('/api/v1/match/matched-records/{pr_id}/watchlist')
def selected_watchlist(pr_id: str, site: str, as_of: date | None = None,
                       divisions: list[str] | None = Query(default=None, max_length=50),
                       statuses: list[str] | None = Query(default=None, max_length=50),
                       record_scope: Literal['completed','ongoing','all'] = 'all', include_cancelled: bool = False):
    try:
        filters=SearchFilters(divisions=divisions or [],statuses=statuses or [],record_scope=record_scope,include_cancelled=include_cancelled).model_dump(mode='json')
    except ValueError as error:
        raise HTTPException(422, detail='Invalid division or status filters.') from error
    try:
        return watchlist_service().inspect(engine(), pr_id, site, as_of, filters=filters)
    except SiteAccessError as error:
        raise HTTPException(403, detail='Site is not authorised.') from error
    except KeyError as error:
        raise HTTPException(404, detail='Record not found for the selected site.') from error
    except (ValueError, OSError, RuntimeError) as error:
        raise HTTPException(503, detail='The retained indexed history is unavailable. Record details remain available.') from error


@app.get('/api/v1/match/matched-records/{pr_id}')
def matched_record(pr_id: str, site: str):
    try:
        from .retained import enriched
        service = engine()
        return enriched(service, service.record(pr_id, site))
    except SiteAccessError as error:
        raise HTTPException(403, detail='Site is not authorised.') from error
    except (KeyError, ValueError) as error:
        raise HTTPException(404, detail='Record not found for the selected site.') from error


@app.get('/api/v1/match/cases')
def saved_cases():
    try:
        return {'items':cases.list_cases(engine())}
    except (OSError, TimeoutError, ValueError) as error:
        raise HTTPException(503,detail='Saved investigations are temporarily unavailable. Retry shortly.') from error


@app.post('/api/v1/match/cases',status_code=201)
def save_case(request:cases.CaseRequest):
    try:
        return cases.save(engine(),assistant(),request)
    except KeyError as error:
        raise HTTPException(409,detail='The search expired or its source changed. Search again before saving.') from error
    except ValueError as error:
        raise HTTPException(422,detail=str(error)) from error
    except SiteAccessError as error:
        raise HTTPException(403,detail='Site is not authorised.') from error
    except (OSError, TimeoutError) as error:
        raise HTTPException(503,detail='The investigation could not be stored. Your source records were not changed; retry shortly.') from error


@app.get('/api/v1/match/cases/{case_id}')
def saved_case(case_id:str):
    try:
        return cases.get(case_id,engine())
    except (FileNotFoundError,ValueError) as error:
        raise HTTPException(404,detail='Saved investigation not found.') from error
    except SiteAccessError as error:
        raise HTTPException(403,detail='The saved investigation contains a site outside the current allowed scope.') from error
    except (OSError, TimeoutError) as error:
        raise HTTPException(503,detail='The saved investigation is temporarily unavailable. Retry shortly.') from error
