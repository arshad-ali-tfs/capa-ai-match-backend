"""Common API plumbing. Business workflows belong to their owning service."""
from __future__ import annotations
from dataclasses import asdict
import os
from pathlib import Path
from uuid import uuid4
from fastapi import FastAPI, HTTPException, Query
from capa_match.infrastructure.contracts import RecordRepository
from capa_match.infrastructure.data.repository import SqliteRecordRepository


def create_service(name: str, repository: RecordRepository | None = None) -> FastAPI:
    if name != 'match':
        raise ValueError('This backend serves only MATCH.')
    # An Entra/Databricks identity adapter must replace this local-only boundary before deployment.
    if os.getenv('CAPA_ENV', 'local') not in {'local', 'databricks'}:
        raise RuntimeError('CAPA_ENV must be local or databricks')
    if os.getenv('CAPA_ENV') == 'databricks' and not os.getenv('DATABRICKS_APP_NAME'):
        raise RuntimeError('Databricks mode must run behind the Databricks Apps identity proxy')
    if repository is None and os.getenv('CAPA_SOURCE_SNAPSHOT'):
        from capa_match.infrastructure.storage import local_file
        repository = SqliteRecordRepository(local_file(os.environ['CAPA_SOURCE_SNAPSHOT']))
    app = FastAPI(title='CAPA.AI ' + name.upper(), version='0.1.0')
    from fastapi.middleware.cors import CORSMiddleware
    import json
    origins = json.loads(os.getenv('CAPA_ALLOWED_ORIGINS_JSON', '["http://localhost:5173","http://127.0.0.1:5173","http://localhost:8100","http://127.0.0.1:8100"]'))
    if not isinstance(origins, list) or any(not isinstance(origin, str) for origin in origins):
        raise ValueError('CAPA_ALLOWED_ORIGINS_JSON must be a JSON array of origins.')
    if origins:
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True, allow_methods=['*'], allow_headers=['*'])
    prefix = '/api/v1/' + name

    @app.middleware('http')
    async def request_id(request, call_next):
        request.state.request_id = str(uuid4())
        response = await call_next(request)
        response.headers['X-Request-ID'] = request.state.request_id
        return response

    def source() -> RecordRepository:
        if repository is None:
            raise HTTPException(503, detail='Import a source file and configure CAPA_SOURCE_SNAPSHOT first.')
        return repository

    @app.get(prefix + '/health')
    def health():
        return {'service': name, 'status': 'alive', 'mode': os.getenv('CAPA_ENV', 'local'), 'identity_proxy': os.getenv('CAPA_ENV') == 'databricks'}

    @app.get(prefix + '/ready')
    def ready():
        try:
            metadata = source().metadata()
        except (OSError, ValueError) as exc:
            raise HTTPException(503, detail='Source snapshot is unavailable.') from exc
        return {'service': name, 'source_ready': True, 'snapshot_id': metadata['snapshot_id'],
                'workflow_configuration': 'Check the owning service; source readiness does not verify model connectivity.'}

    @app.get(prefix + '/capabilities')
    def capabilities():
        return {'source_browsing': name != 'match', 'business_workflow': 'available',
                'trackwise_connection': False, 'model_calls': bool(os.getenv('OPENAI_API_KEY')) and os.getenv('MATCH_ASSIST_ENABLED', '1') == '1',
                'optional_ai_assistance': True}

    @app.get(prefix + '/source')
    def source_metadata():
        return source().metadata()

    # MATCH must use its own site-scoped indexed record endpoint.
    # Generic snapshot browsing would bypass that service's retained site policy.
    if name == 'match':
        return app

    @app.get(prefix + '/source/quarantine')
    def quarantine(offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100)):
        return {**source().list_quarantine(offset=offset,limit=limit), 'snapshot_id':source().metadata()['snapshot_id']}

    @app.get(prefix + '/records')
    def records(offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100),
                site: str | None = None, record_type: str | None = None):
        result = source().list_records(offset=offset, limit=limit, site=site, record_type=record_type)
        return {**result, 'snapshot_id': source().metadata()['snapshot_id']}

    @app.get(prefix + '/records/{pr_id}')
    def record(pr_id: str):
        item = source().get_record(pr_id)
        if item is None and pr_id == 'SYNTHETIC-MAP-001':
            from capa_match.infrastructure.synthetic import SYNTHETIC_RECORD
            from capa_match.infrastructure.contracts import SourceRecord
            raw=SYNTHETIC_RECORD
            item=SourceRecord(pr_id,raw['SITE'],raw['PSG_Division'],raw['RECORD_TYPE'],raw['STATUS'],raw['RECORD_NAME'],0,{k.lower():v for k,v in raw.items()},raw)
        if item is None:
            raise HTTPException(404, detail='Record not found.')
        return {**asdict(item), 'snapshot_id': 'synthetic-fixture-v1' if pr_id == 'SYNTHETIC-MAP-001' else source().metadata()['snapshot_id']}

    return app
