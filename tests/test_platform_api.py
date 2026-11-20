"""Platform MATCH contract exercised with the real offline retrieval pipeline."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from capa_match import main
from capa_match.infrastructure.data.importer import import_snapshot
from capa_match.infrastructure.data.repository import SqliteRecordRepository
from match_v1 import api
from match_v1.core import MockEmbedder, split_chunks, TEXT_NORMALIZATION_VERSION
from match_v1.ingest import load_gtw
from match_v1.search import MatchService
from match_v1.testing import MemoryCollection

DEMO = Path(__file__).parent / 'fixtures' / 'demo_gtw.csv'
SITE = 'TRO (Toronto)'
QUERY = 'HPLC bracket standard failed system suitability during testing'


@pytest.fixture
def platform_client(monkeypatch, tmp_path):
    snapshot = import_snapshot(DEMO, tmp_path / 'snapshots')
    snapshot_id = SqliteRecordRepository(snapshot).metadata()['snapshot_id']
    records, capas, _ = load_gtw(DEMO, division='DPD', limit=0)
    model, collection = MockEmbedder(), MemoryCollection()
    chunks = []
    for record in records:
        for field, text in record['indexed_field_texts'].items():
            for position, text_chunk in enumerate(split_chunks(text)):
                chunks.append({'chunk_id': f'{record["pr_id"]}_{field}_{position}',
                               'pr_id': record['pr_id'], 'site': record['site'],
                               'division': record['division'], 'doc': text_chunk,
                               'source_field': field})
    collection.upsert(ids=[item['chunk_id'] for item in chunks],
                      documents=[item['doc'] for item in chunks],
                      metadatas=[{'site': item['site']} for item in chunks],
                      embeddings=model.encode([item['doc'] for item in chunks]).tolist())
    manifest = {'model_name': model.model_name, 'embedding_backend': 'MOCK_UNFIT_FOR_REAL_SEARCH',
                'embedding_runtime': 'mock', 'source_snapshot_id': snapshot_id,
                'site_counts': {name: sum(record['site'] == name for record in records)
                                for name in {record['site'] for record in records}},
                'chunk_size': 500, 'chunk_overlap': 50, 'text_fields': ['DETAILED_DESCRIPTION'],
                'storage_directory': 'private-storage-path',
                'source_statistics': {'source_file': 'private-source-file.csv', 'text_normalization_version': TEXT_NORMALIZATION_VERSION}}
    index = tmp_path / 'index'
    index.mkdir()
    (index / 'manifest.json').write_text(json.dumps(manifest), encoding='utf8')
    service = MatchService(manifest=manifest, records={item['pr_id']: item for item in records},
                           chunks=chunks, capas=capas, collection=collection, embedder=model,
                           allowed_sites={SITE})
    monkeypatch.setenv('CAPA_SOURCE_SNAPSHOT', str(snapshot))
    monkeypatch.setenv('MATCH_INDEX_DIR', str(index))
    monkeypatch.setenv('CAPA_ENV', 'local')
    monkeypatch.setattr(api, 'get_service', lambda: service)
    main.engine.cache_clear()
    with TestClient(main.app) as client:
        yield client, service, index
    main.engine.cache_clear()


def test_index_coverage_model_and_allowed_site_contract(platform_client):
    client, service, _ = platform_client
    response = client.get('/api/v1/match/index')
    assert response.status_code == 200
    value = response.json()
    assert value['ready'] and value['mock_embeddings']
    assert value['records'] == 8 and value['searchable_records'] == 6
    assert value['chunks'] == len(service.chunks)
    assert value['allowed_sites'] == [{'site': SITE, 'deviation_count': 6}]
    assert value['text_fields'] == ['DETAILED_DESCRIPTION']
    assert value['embedding_runtime'] == 'mock'
    assert value['ranking'] == {'semantic': .7, 'bm25': .2, 'jaccard': .1}
    assert 'private-' not in response.text and 'Greenville' not in response.text
    assert 'source_statistics' not in value and 'storage_directory' not in value
    assert response.headers['x-request-id']
    assert client.get('/api/v1/match/sites').json()['sites'] == value['allowed_sites']
    # The platform liveness endpoint stays separate from index readiness.
    assert client.get('/api/v1/match/health').json()['status'] == 'alive'


def test_real_search_preserves_ranking_fields_and_parent_linked_capas(platform_client):
    client, _, _ = platform_client
    response = client.post('/api/v1/match/search', json={'query': QUERY, 'site': SITE, 'top_k': 5})
    assert response.status_code == 200
    value = response.json()
    assert value['result_count'] == len(value['results']) == 5
    assert value['mock_embeddings'] and value['candidate_chunks'] > 0
    first = value['results'][0]
    assert first['pr_id'] == '101' and first['matched_field'] == 'DETAILED_DESCRIPTION'
    assert first['matched_excerpt'] and first['dr_type'] and first['status'] and first['criticality']
    assert first['related_capa_count'] == 1
    capa = first['related_capas'][0]
    assert capa['pr_id'] == '901'
    assert {'record_name', 'status', 'capa_type', 'date_closed', 'plan_excerpt'} <= capa.keys()
    assert capa['plan_excerpt']
    for record in value['results']:
        assert record['site'] == SITE
        expected = .7 * record['semantic_score'] + .2 * record['bm25_score'] + .1 * record['jaccard_score']
        assert record['score_percent'] == pytest.approx(expected * 100, abs=.006)


def test_matched_record_has_complete_indexed_fields_and_capa_metadata(platform_client):
    client, service, _ = platform_client
    response = client.get('/api/v1/match/matched-records/101', params={'site': SITE})
    assert response.status_code == 200
    value = response.json()
    assert value['indexed_field_texts'] == service.records['101']['indexed_field_texts']
    assert value['text'] == service.records['101']['text']
    assert {'date_opened', 'date_closed', 'responsible_department', 'criticality', 'division'} <= value.keys()
    assert value['related_capas'] == service.capas['101']


@pytest.mark.parametrize('top_k', [1, 5, 10, 20, 30, 50])
def test_supported_result_limits(platform_client, top_k):
    client, _, _ = platform_client
    response = client.post('/api/v1/match/search', json={'query': QUERY, 'site': SITE, 'top_k': top_k})
    assert response.status_code == 200
    assert response.json()['result_count'] == min(top_k, 6)


@pytest.mark.parametrize('changes', [{'top_k': 0}, {'top_k': 51}, {'mode': 'made-up'}, {'query': 'x' * 30001}, {'site': ''}])
def test_request_boundaries(platform_client, changes):
    client, _, _ = platform_client
    assert client.post('/api/v1/match/search', json={'query': QUERY, 'site': SITE, **changes}).status_code == 422


def test_meaningless_query_and_site_boundaries(platform_client):
    client, _, _ = platform_client
    assert client.post('/api/v1/match/search', json={'query': '???', 'site': SITE}).status_code == 400
    assert client.post('/api/v1/match/search', json={'query': QUERY, 'site': 'GRC (Greenville, NC)'}).status_code == 403
    assert client.get('/api/v1/match/matched-records/105', params={'site': SITE}).status_code == 404
    assert client.get('/api/v1/match/matched-records/105', params={'site': 'GRC (Greenville, NC)'}).status_code == 403


def test_source_version_mismatch_refuses_metadata_search_and_record(platform_client):
    client, _, index = platform_client
    manifest = json.loads((index / 'manifest.json').read_text(encoding='utf8'))
    manifest['source_snapshot_id'] = 'different-source'
    (index / 'manifest.json').write_text(json.dumps(manifest), encoding='utf8')
    for response in [client.get('/api/v1/match/index'), client.get('/api/v1/match/sites'),
                     client.post('/api/v1/match/search', json={'query': QUERY, 'site': SITE}),
                     client.get('/api/v1/match/matched-records/101', params={'site': SITE})]:
        assert response.status_code == 503
        assert str(index) not in response.text


def test_runtime_search_failure_is_safe_and_retryable(platform_client, monkeypatch):
    client, service, _ = platform_client
    def unavailable(_):
        raise RuntimeError('private-model-location could not be opened')
    monkeypatch.setattr(service.embedder, 'encode', unavailable)
    response = client.post('/api/v1/match/search', json={'query': QUERY, 'site': SITE})
    assert response.status_code == 503
    assert 'private-model-location' not in response.text


@pytest.mark.parametrize('model', ['C:\\private\\models\\mpnet', '/private/models/mpnet'])
def test_local_model_metadata_hides_host_directory(platform_client, model):
    client, service, _ = platform_client
    service.manifest['model_name'] = model
    assert client.get('/api/v1/match/index').json()['model_name'] == 'mpnet'
    result = client.post('/api/v1/match/search', json={'query': QUERY, 'site': SITE})
    assert result.status_code == 200 and result.json()['model_name'] == 'mpnet'


@pytest.mark.parametrize('mode', [None, 'auto', 'semantic'])
def test_model_file_access_failure_is_unavailability_not_a_site_denial(platform_client, mode):
    client, service, _ = platform_client
    def inaccessible(_):
        raise PermissionError('private-model-directory is inaccessible')
    service.embedder.encode = inaccessible
    response = client.post('/api/v1/match/search', json={'query': QUERY, 'site': SITE, **({'mode': mode} if mode else {})})
    assert response.status_code == 503
    assert 'private-model-directory' not in response.text


def test_all_sites_api_returns_combined_results_and_site_scoped_details(platform_client):
    client, service, _ = platform_client
    service.allowed_sites.add('GRC (Greenville, NC)')
    response = client.post('/api/v1/match/search', json={'query': QUERY, 'all_sites': True, 'top_k': 8})
    assert response.status_code == 200
    value = response.json()
    assert value['all_sites'] and value['site'] == 'All sites'
    assert set(value['searched_sites']) == service.allowed_sites
    assert value['result_count'] == 8
    assert {record['site'] for record in value['results']} == service.allowed_sites
    assert value['source_snapshot_id']
    other = next(record for record in value['results'] if record['pr_id'] == '105')
    assert client.get('/api/v1/match/matched-records/105', params={'site': other['site']}).status_code == 200
    assert client.get('/api/v1/match/matched-records/105', params={'site': SITE}).status_code == 404
    assert client.get('/api/v1/match/matched-records/105', params={'site': 'All sites'}).status_code == 403


def test_all_sites_api_respects_server_allowed_sites(platform_client):
    client, _, _ = platform_client
    response = client.post('/api/v1/match/search', json={'query': 'Electronic batch record signature correction', 'all_sites': True, 'top_k': 50})
    assert response.status_code == 200
    value = response.json()
    assert value['searched_sites'] == [SITE]
    assert value['result_count'] == 6
    assert all(record['site'] == SITE for record in value['results'])


@pytest.mark.parametrize('scope', [{}, {'all_sites': False}, {'site': None}, {'site': ''},
                                   {'all_sites': True, 'site': SITE},
                                   {'all_sites': True, 'site': 'GRC (Greenville, NC)'}])
def test_api_requires_an_unambiguous_explicit_search_scope(platform_client, scope):
    client, _, _ = platform_client
    response = client.post('/api/v1/match/search', json={'query': QUERY, **scope})
    assert response.status_code == 422
