from .cli import configure

configure()

import os
import json
import logging
from threading import Lock, Thread
from pathlib import Path, PurePosixPath, PureWindowsPath
from functools import lru_cache
from fastapi import HTTPException
from match_v1.contracts import SearchRequest
from match_v1.search import SiteAccessError
from capa_match.infrastructure.web import create_service
from capa_match.infrastructure.storage import local_directory

app = create_service('match')
_warmup_lock = Lock()


def query_model_state(service):
    """Prepare the local query model while investigators choose their filters."""
    if service.manifest['embedding_backend'] != 'MPNet':
        return 'ready'
    model = service.embedder
    if getattr(model, '_session', None) is not None or getattr(model, '_model', None) is not None:
        return 'ready'
    with _warmup_lock:
        state = getattr(model, '_match_warmup_state', None)
        if state is not None:
            return state
        model._match_warmup_state = 'warming'
        def prepare():
            try:
                model.encode(['Historical deviation search.'])
                model._match_warmup_state = 'ready'
            except Exception as error:
                model._match_warmup_state = 'unavailable'
                logging.getLogger(__name__).warning('MATCH query-model warm-up failed (%s).', type(error).__name__)
        Thread(target=prepare, name='match-query-model-warmup', daemon=True).start()
        return model._match_warmup_state

@lru_cache(maxsize=1)
def engine():
    from match_v1.api import get_service
    try:
        os.environ['MATCH_INDEX_DIR'] = str(local_directory(os.environ['MATCH_INDEX_DIR']))
        manifest = json.loads((Path(os.environ['MATCH_INDEX_DIR']) / 'manifest.json').read_text(encoding='utf8'))
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
    }


@app.get('/api/v1/match/sites')
def sites():
    return {'sites': engine().sites()}

@app.post('/api/v1/match/search')
def search(request: SearchRequest):
    try:
        if not request.use_explorer:
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

@app.get('/api/v1/match/matched-records/{pr_id}')
def matched_record(pr_id: str, site: str):
    try:
        return engine().record(pr_id, site)
    except SiteAccessError as error:
        raise HTTPException(403, detail='Site is not authorised.') from error
    except (KeyError, ValueError) as error:
        raise HTTPException(404, detail='Record not found for the selected site.') from error
