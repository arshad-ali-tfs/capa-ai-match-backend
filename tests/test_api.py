import json

import pytest
from fastapi.testclient import TestClient

from match_v1 import api
from test_search import make_service


@pytest.fixture
def client():
    api.app.dependency_overrides[api.service_dependency] = make_service
    with TestClient(api.app) as client:
        yield client
    api.app.dependency_overrides.clear()
    api.get_service.cache_clear()


def test_browser_and_health(client):
    assert client.get('/').status_code == 404
    health = client.get('/api/health')
    assert health.status_code == 200 and health.json()['ready']
    assert health.json()['allowed_sites'] == [{'site': 'TRO (Toronto)', 'deviation_count': 6}]
    assert client.get('/docs').status_code == 200


def test_search_and_capa_record(client):
    result = client.post('/api/search', json={
        'query': 'HPLC bracket standard failed system suitability during testing',
        'site': 'TRO (Toronto)', 'top_k': 5,
    })
    assert result.status_code == 200
    assert result.json()['mock_embeddings'] is True
    first = result.json()['results'][0]
    assert first['pr_id'] == '101'
    assert first['related_capas'][0]['pr_id'] == '901'
    detail = client.get('/api/records/101', params={'site': 'TRO (Toronto)'})
    assert detail.status_code == 200 and detail.json()['related_capas'][0]['pr_id'] == '901'


def test_site_rejection_and_cross_site_record(client):
    response = client.post('/api/search', json={'query': 'HPLC system suitability failed',
                                               'site': 'GRC (Greenville, NC)'})
    assert response.status_code == 403
    assert client.get('/api/records/105', params={'site': 'TRO (Toronto)'}).status_code == 404


@pytest.mark.parametrize('payload', [
    {'query': 'HPLC', 'site': 'TRO (Toronto)', 'mode': 'made-up'},
    {'query': 'HPLC bracket failed', 'site': 'TRO (Toronto)', 'top_k': 51},
    {'query': 'x' * 30001, 'site': 'TRO (Toronto)'},
])
def test_invalid_request(client, payload):
    assert client.post('/api/search', json=payload).status_code == 422


@pytest.mark.parametrize('value', ['"TRO (Toronto)"', '{"TRO (Toronto)":true}', '[123]', '[""]', '[[]]', 'invalid'])
def test_bad_allowed_site_configuration(monkeypatch, value):
    api.get_service.cache_clear()
    monkeypatch.setenv('MATCH_ALLOWED_SITES_JSON', value)
    with TestClient(api.app) as client:
        for endpoint in ('/api/health', '/api/sites'):
            response = client.get(endpoint)
            assert response.status_code == 503
            assert 'JSON array' in response.json()['detail']
    api.get_service.cache_clear()


def test_missing_index_is_service_unavailable(monkeypatch, tmp_path):
    api.get_service.cache_clear()
    monkeypatch.setenv('MATCH_INDEX_DIR', str(tmp_path / 'missing'))
    monkeypatch.setenv('MATCH_ALLOWED_SITES_JSON', json.dumps(['TRO (Toronto)']))
    with TestClient(api.app) as client:
        assert client.get('/api/health').status_code == 503
        assert client.get('/api/sites').status_code == 503
        response = client.post('/api/search', json={'query': 'HPLC bracket failed', 'site': 'TRO (Toronto)'})
        assert response.status_code == 503
    api.get_service.cache_clear()


def test_standalone_all_sites_respects_authorized_sites(client):
    response = client.post('/api/search', json={'query': 'HPLC system suitability failed', 'all_sites': True, 'top_k': 50})
    assert response.status_code == 200
    assert response.json()['searched_sites'] == ['TRO (Toronto)']
    assert all(record['site'] == 'TRO (Toronto)' for record in response.json()['results'])
    assert client.post('/api/search', json={'query': 'HPLC bracket failed'}).status_code == 422
    assert client.post('/api/search', json={'query': 'HPLC bracket failed', 'site': 'TRO (Toronto)', 'all_sites': True}).status_code == 422
