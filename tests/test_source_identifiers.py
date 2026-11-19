"""Exact source IDs survive MATCH normalization and AUTHOR/source handoffs."""
import csv
import json

import pytest
from fastapi.testclient import TestClient

from capa_match import main
from capa_match.infrastructure.data.importer import import_snapshot
from capa_match.infrastructure.data.repository import SqliteRecordRepository
from capa_match.infrastructure.web import create_service
from match_v1 import api
from match_v1.core import MockEmbedder, split_chunks
from match_v1.ingest import load_gtw
from match_v1.search import MatchService
from match_v1.testing import MemoryCollection


@pytest.fixture
def leading_zero_source(tmp_path, monkeypatch):
    source = tmp_path / 'fictional.csv'
    fields = ['PR_ID', 'PARENT_ID', 'SITE', 'PSG_Division', 'RECORD_TYPE', 'STATUS', 'DETAILED_DESCRIPTION', 'RECORD_NAME', 'INVESTIGATION']
    with source.open('w', encoding='utf8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for pr_id, kind, site, parent in [('00042', 'Deviation', 'Site A', ''), ('00043', 'Deviation', 'Site B', ''), ('00901', 'CAPA', 'Site A', '00042')]:
            writer.writerow({'PR_ID': pr_id, 'PARENT_ID': parent, 'SITE': site, 'PSG_Division': 'DPD',
                             'RECORD_TYPE': kind, 'STATUS': 'Closed - Done', 'RECORD_NAME': 'Fictional bracket event',
                             'DETAILED_DESCRIPTION': 'FICTIONAL TEST: verification bracket failed before testing.',
                             'INVESTIGATION': 'FICTIONAL: equipment EQ-900 investigation-only evidence.' if pr_id == '00042' else ''})
    snapshot = import_snapshot(source, tmp_path / 'snapshots')
    repository = SqliteRecordRepository(snapshot)
    records, capas, _ = load_gtw(source, limit=0)
    embedder, collection = MockEmbedder(), MemoryCollection()
    chunks = []
    for record in records:
        for position, text in enumerate(split_chunks(record['text'])):
            chunks.append({'chunk_id': f'{record["pr_id"]}_chunk_{position}', 'pr_id': record['pr_id'],
                           'site': record['site'], 'division': record['division'], 'doc': text})
    collection.upsert(ids=[item['chunk_id'] for item in chunks], documents=[item['doc'] for item in chunks],
                      metadatas=[{'site': item['site']} for item in chunks],
                      embeddings=embedder.encode([item['doc'] for item in chunks]).tolist())
    manifest = {'source_snapshot_id': repository.metadata()['snapshot_id'], 'model_name': embedder.model_name,
                'embedding_backend': 'MOCK_UNFIT_FOR_REAL_SEARCH', 'embedding_runtime': 'mock',
                'site_counts': {'Site A': 1, 'Site B': 1}, 'chunk_size': 500, 'chunk_overlap': 50}
    service = MatchService(manifest=manifest, records={record['pr_id']: record for record in records},
                           chunks=chunks, capas=capas, collection=collection, embedder=embedder,
                           allowed_sites={'Site A'})
    index = tmp_path / 'index'
    index.mkdir()
    (index / 'manifest.json').write_text(json.dumps(manifest), encoding='utf8')
    monkeypatch.setenv('CAPA_SOURCE_SNAPSHOT', str(snapshot))
    monkeypatch.setenv('MATCH_INDEX_DIR', str(index))
    monkeypatch.setenv('AUTHOR_DATA_MODE', 'csv')
    monkeypatch.setattr(api, 'get_service', lambda: service)
    main.engine.cache_clear()
    yield repository, service
    main.engine.cache_clear()


def test_leading_zero_ids_survive_search_details_and_source_identity(leading_zero_source):
    repository, original_service = leading_zero_source
    query = 'FICTIONAL TEST: verification bracket failed before testing.'
    baseline = original_service.search(query, site='Site A')['results'][0]
    with TestClient(main.app) as client:
        response = client.post('/api/v1/match/search', json={'query': query, 'site': 'Site A'})
        assert response.status_code == 200
        record = response.json()['results'][0]
        assert record['pr_id'] == record['source_pr_id'] == '00042'
        assert record['match_pr_id'] == '42'
        assert record['matched_chunk_id'] == baseline['matched_chunk_id']
        for field in ['match_score', 'score_percent', 'semantic_score', 'bm25_score', 'jaccard_score', 'matched_excerpt']:
            assert record[field] == baseline[field]
        assert record['related_capas'][0]['pr_id'] == '00901'
        assert record['related_capas'][0]['match_pr_id'] == '901'
        detail = client.get('/api/v1/match/matched-records/' + record['pr_id'], params={'site': 'Site A'})
        assert detail.status_code == 200 and detail.json()['pr_id'] == '00042'
        assert detail.json()['source_snapshot_id'] == repository.metadata()['snapshot_id']
        assert client.get('/api/v1/match/matched-records/42', params={'site': 'Site A'}).status_code == 404
        assert client.get('/api/v1/match/matched-records/00043', params={'site': 'Site A'}).status_code == 404
        assert client.get('/api/v1/match/matched-records/00043', params={'site': 'Site B'}).status_code == 403
    assert repository.get_record(record['pr_id']).pr_id == '00042'


def test_index_site_mismatch_refuses_source_mapping(leading_zero_source):
    _, service = leading_zero_source
    service.records['42']['site'] = 'Site B'
    with TestClient(main.app) as client:
        assert client.get('/api/v1/match/index').status_code == 503


def test_all_sites_preserves_exact_ids_and_capa_links(leading_zero_source):
    _, service = leading_zero_source
    service.allowed_sites.add('Site B')
    with TestClient(main.app) as client:
        response = client.post('/api/v1/match/search', json={
            'query': 'FICTIONAL TEST: verification bracket failed before testing.', 'all_sites': True})
        assert response.status_code == 200
        records = {record['pr_id']: record for record in response.json()['results']}
        assert set(records) == {'00042', '00043'}
        assert records['00042']['related_capas'][0]['pr_id'] == '00901'
        assert records['00043']['site'] == 'Site B'
        assert client.get('/api/v1/match/matched-records/00043', params={'site': records['00043']['site']}).status_code == 200


def test_investigation_only_identifiers_are_searchable_and_details_retain_their_field(leading_zero_source):
    with TestClient(main.app) as client:
        response = client.post('/api/v1/match/search', json={'query': 'EQ900', 'mode': 'auto', 'all_sites': True})
        assert response.status_code == 200
        result = response.json()
        assert result['total_matches'] == 1 and result['results'][0]['pr_id'] == '00042'
        assert result['results'][0]['matched_field'] == 'INVESTIGATION'
        assert result['results'][0]['match_score'] is None
        detail = client.get('/api/v1/match/matched-records/00042', params={'site': 'Site A'}).json()
        assert detail['literal_field_texts']['INVESTIGATION'] == 'FICTIONAL: equipment EQ-900 investigation-only evidence.'
        denied = client.post('/api/v1/match/search', json={'query': 'EQ900', 'mode': 'auto', 'site': 'Site B'})
        assert denied.status_code == 403
