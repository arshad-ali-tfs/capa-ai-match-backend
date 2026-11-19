"""Investigator search: hard scopes, literal equipment IDs and truthful totals."""
import pytest
import numpy as np
from fastapi.testclient import TestClient
from threading import Event
from types import SimpleNamespace
from match_v1 import api
from match_v1.contracts import SearchRequest
from match_v1.core import split_chunks
from match_v1.explorer import MatchExplorer, identifiers, literal_pattern
from test_search import make_service


def explorer():
    service = make_service()
    variants = [
        ('101', '2020-02-01', 'Major', 'Laboratory', 'Product Deviation', 'HPLC EQ-001 calibration drift at startup.'),
        ('102', '2021-03-01', 'Minor', 'Production', 'Site Deviation', 'Equipment EQ001 calibration failed. ' + 'retained source text. ' * 40 + 'EQ001 late evidence marker.'),
        ('103', '2021-04-01', 'Critical', 'Laboratory', 'Product Deviation', 'HPLC EQ-0010 calibration issue.'),
        ('104', '2022-05-01', 'Major', 'Laboratory', 'Site Deviation', 'HPLC EQ-002 calibration drift.'),
        ('107', '', 'Minor', 'Production', 'Site Deviation', 'Packing material reconciliation incomplete.'),
        ('108', '2022-06-01', 'Major', 'Laboratory', 'Product Deviation', 'Balance verification failed at 10 degrees in Jan-2020.'),
    ]
    for key, opened, severity, department, kind, text in variants:
        service.records[key].update(date_opened=opened, criticality=severity, responsible_department=department,
                                    dr_type=kind, text=text, indexed_field_texts={'DETAILED_DESCRIPTION': text})
    service._corpus_caches.clear()
    service.chunks = []
    service.collection.items.clear()
    for record in service.records.values():
        for seq, text in enumerate(split_chunks(record['text'])):
            chunk = {'chunk_id': f'{record["pr_id"]}_chunk_{seq}', 'pr_id': record['pr_id'],
                     'site': record['site'], 'division': record['division'], 'doc': text, 'source_field': 'DETAILED_DESCRIPTION'}
            service.chunks.append(chunk)
    service.collection.upsert(ids=[c['chunk_id'] for c in service.chunks], documents=[c['doc'] for c in service.chunks],
                              metadatas=[{key: c[key] for key in ('site', 'pr_id', 'division')} for c in service.chunks],
                              embeddings=service.embedder.encode([c['doc'] for c in service.chunks]).tolist())
    service.candidate_k = 100
    return service


def test_filters_only_browse_is_paginated_and_never_calls_the_model():
    service = explorer()
    service.embedder.encode = lambda _: pytest.fail('Browsing must not embed a query')
    first = service.search('', all_sites=True, top_k=2)
    second = service.search('', all_sites=True, top_k=2, offset=2)
    assert first['search_mode'] == 'browse' and first['total_matches'] == first['eligible_record_count'] == 6
    assert first['has_more'] and len(first['results']) == 2
    assert not {r['pr_id'] for r in first['results']} & {r['pr_id'] for r in second['results']}
    assert all(row['match_score'] is None for row in first['results'])
    assert [row['pr_id'] for row in first['results']] == ['108', '104']


def test_structured_filters_combine_or_within_a_field_and_and_between_fields():
    service = explorer()
    result = service.search('', all_sites=True, filters={'years': [2020, 2021], 'criticalities': ['Major', 'Critical'],
                                                       'departments': ['Laboratory'], 'dr_types': ['Product Deviation']})
    assert {r['pr_id'] for r in result['results']} == {'101', '103'}
    assert result['eligible_record_count'] == 2 and result['scope_record_count'] == 6
    assert service.search('', all_sites=True, filters={'date_from': '2021-03-01', 'date_to': '2021-04-01'})['total_matches'] == 2
    assert service.search('', all_sites=True, filters={'years': [2099]})['results'] == []


def test_capa_filters_use_explicit_index_links_and_done_status_only():
    service = explorer()
    service.capas = {'101': [{'status': 'Closed - Done'}], '102': [{'status': 'Closed - Cancelled'}], '103': [{'status': 'Open'}]}
    assert service.search('', all_sites=True, filters={'capa': 'linked'})['total_matches'] == 3
    done = service.search('', all_sites=True, filters={'capa': 'completed'})
    assert [r['pr_id'] for r in done['results']] == ['101']
    assert service.search('', all_sites=True, filters={'capa': 'none'})['total_matches'] == 3


@pytest.mark.parametrize('query', ['EQ-001', 'eq001', 'EQ_001', 'EQ 001'])
def test_identifier_variants_preserve_digits_and_exclude_prefix_collisions(query):
    service = explorer()
    service.embedder.encode = lambda _: pytest.fail('An exact ID search must not embed a query')
    result = service.search(query, all_sites=True)
    assert {r['pr_id'] for r in result['results']} == {'101', '102'}
    assert result['search_mode'] == 'identifier' and result['total_matches'] == 2
    assert result['eligible_record_count'] == 6
    assert all(row['match_score'] is None for row in result['results'])


def test_unknown_identifier_has_no_semantic_fallback_and_filters_are_not_relaxed():
    service = explorer()
    assert service.search('EQ-999999', all_sites=True)['results'] == []
    result = service.search('EQ001', all_sites=True, filters={'criticalities': ['Critical']})
    assert result['results'] == [] and result['eligible_record_count'] == 1


def test_mixed_prefixes_and_suffixes_are_part_of_the_identifier_not_guessed_away():
    assert identifiers('73-MFG-001') == ['73-MFG-001']
    assert identifiers('HPLC 001 calibration') == ['HPLC 001']
    pattern = literal_pattern('73-MFG-001')
    assert pattern.search('equipment 73MFG001 was checked')
    assert not pattern.search('equipment 74-MFG-001 was checked')
    assert not literal_pattern('MFG-001').search('equipment 73-MFG-001 was checked')
    assert not literal_pattern('EQ-001').search('asset EQ-001.1 was checked')
    assert literal_pattern('SOP-12-34').search('SOP_12_34')
    assert not literal_pattern('SOP-12-34').search('SOP-1234')
    assert not literal_pattern('SOP-12-34').search('SOP-1-234')


def test_identifier_plus_keywords_and_long_narrative_have_explicit_intent():
    service = explorer()
    result = service.search('EQ-001 calibration', all_sites=True)
    assert {r['pr_id'] for r in result['results']} == {'101', '102'}
    assert result['required_identifiers'] == ['EQ-001']
    query = 'A long investigation narrative about EQ-001 concerns multiple production steps and compares possible similar calibration deviations elsewhere'
    result = service.search(query, all_sites=True)
    assert result['search_mode'] == 'hybrid' and result['required_identifiers'] == []


def test_short_keywords_and_exact_phrases_find_real_text_and_highlight_late_hits():
    service = explorer()
    assert service.search('HPLC', all_sites=True)['total_matches'] == 3
    phrase = service.search('"late evidence marker"', all_sites=True)
    assert [row['pr_id'] for row in phrase['results']] == ['102']
    row = phrase['results'][0]
    assert 'late evidence marker' in row['matched_excerpt']
    assert row['matched_spans']
    for span in row['matched_spans']:
        assert row['matched_excerpt'][span['start']:span['end']] == 'late evidence marker'
    assert service.search('HPLCX', all_sites=True, mode='keyword')['results'] == []


def test_source_id_lookup_is_exact_and_keeps_leading_zeros():
    service = explorer()
    service.source_ids['101'] = '00101'
    assert service.search('00101', all_sites=True)['results'][0]['pr_id'] == '101'
    assert service.search('PR_ID: 00101', all_sites=True)['search_mode'] == 'record_id'
    assert service.search('101', all_sites=True)['results'] == []
    assert service.search('105', all_sites=True)['results'] == []  # outside allowed sites
    assert service.search('PR00101', all_sites=True)['results'][0]['pr_id'] == '101'


@pytest.mark.parametrize('query', ['production', 'problem', 'previous', 'product deviation'])
def test_ordinary_words_starting_pr_are_not_misread_as_record_ids(query):
    assert explorer().search(query, all_sites=True)['search_mode'] == 'keyword'


def test_facets_and_filtered_semantic_candidates_stay_within_authorized_scope():
    service = explorer()
    facets = service.facets(all_sites=True, filters={'criticalities': ['Major']})
    assert facets['scope_record_count'] == 6 and facets['eligible_record_count'] == 3
    assert {row['value'] for row in facets['facets']['criticalities']} == {'Major', 'Minor', 'Critical'}
    assert all('Greenville' not in str(row) for row in facets['facets'].values())
    captured = []
    original = service.collection.query
    def query(**kwargs):
        captured.append(kwargs['where'])
        return original(**kwargs)
    service.collection.query = query
    result = service.search('HPLC calibration failed at startup', all_sites=True, filters={'years': [2020]})
    assert {r['pr_id'] for r in result['results']} == {'101'}
    assert captured == [{'$and': [{'site': 'TRO (Toronto)'}, {'pr_id': {'$in': ['101']}}]}]
    assert result['total_matches'] is None and result['eligible_record_count'] == 1
    with pytest.raises(PermissionError):
        service.facets(site='GRC (Greenville, NC)')


@pytest.mark.parametrize('query', ['calibration at 10 degrees', 'an event in Jan-2020', 'pH 7 excursion', 'occurred at 1pm', 'the 1st sample'])
def test_measurements_and_months_are_not_silently_treated_as_equipment_ids(query):
    assert explorer().search(query, all_sites=True)['required_identifiers'] == []


@pytest.mark.parametrize('filters', [{'date_from': '2022-01-01', 'date_to': '2020-01-01'}, {'unknown': ['x']}, {'years': [1]}])
def test_invalid_filters_are_rejected_instead_of_ignored(filters):
    with pytest.raises(ValueError):
        explorer().search('', all_sites=True, filters=filters)


def test_explorer_http_contract_and_facets_need_explicit_scope():
    service = explorer()
    api.app.dependency_overrides[api.service_dependency] = lambda: service
    try:
        with TestClient(api.app) as client:
            assert client.post('/api/search', json={'all_sites': True}).json()['search_mode'] == 'browse'
            assert client.post('/api/facets', json={'all_sites': True}).json()['scope_record_count'] == 6
            assert client.post('/api/facets', json={}).status_code == 422
            assert client.post('/api/search', json={'all_sites': True, 'filters': {'made_up': True}}).status_code == 422
            assert client.post('/api/search', json={'site': 'TRO (Toronto)', 'query': 'HPLC'}).status_code == 200
    finally:
        api.app.dependency_overrides.clear()


def test_existing_narrative_clients_keep_their_hybrid_score_contract():
    service = explorer()
    api.app.dependency_overrides[api.service_dependency] = lambda: service
    try:
        with TestClient(api.app) as client:
            legacy = client.post('/api/search', json={'all_sites': True, 'query': 'HPLC calibration'}).json()
            assert legacy['results'] and all(isinstance(row['match_score'], float) for row in legacy['results'])
            modern = client.post('/api/search', json={'all_sites': True, 'query': 'HPLC calibration', 'mode': 'auto'}).json()
            assert modern['search_mode'] == 'keyword'
            assert modern['results'] and all(row['match_score'] is None for row in modern['results'])
    finally:
        api.app.dependency_overrides.clear()


def test_event_title_can_prioritize_an_actual_event_without_rewriting_narrative_scores():
    service = explorer()
    calls = []
    def encode(texts):
        calls.append(list(texts))
        vectors = np.zeros((len(texts), 768), dtype=np.float32)
        for i, text in enumerate(texts):
            vectors[i, 0 if text == 'Actual wrong-material event' else 1] = 1
        return vectors
    service.embedder.encode = encode
    search = MatchExplorer(service)
    query = np.zeros((1, 768), dtype=np.float32); query[0, 0] = 1
    rows = [{'pr_id': '101', 'record_name': 'Generic materials discussion', 'match_score': .90, 'match_reasons': []},
            {'pr_id': '102', 'record_name': 'Actual wrong-material event', 'match_score': .80, 'match_reasons': []},
            {'pr_id': '103', 'record_name': '', 'match_score': .50, 'match_reasons': []}]
    ranked = search.rerank(rows, query)
    assert ranked[0]['pr_id'] == '102'
    assert ranked[0]['match_score'] == .80 and ranked[0]['relevance_score'] == pytest.approx(.86)
    assert ranked[-1]['relevance_score'] == .50  # missing title never supplies invented evidence
    search.rerank(rows, query)
    assert len(calls) == 1


def test_query_model_warms_in_background_once_without_blocking_filter_browsing():
    from capa_match.main import query_model_state
    started, release = Event(), Event()
    calls = []
    def encode(texts):
        calls.append(texts); started.set(); release.wait(timeout=5)
    model = SimpleNamespace(encode=encode)
    service = SimpleNamespace(manifest={'embedding_backend': 'MPNet'}, embedder=model)
    try:
        assert query_model_state(service) == 'warming'
        assert started.wait(timeout=2)
        assert query_model_state(service) == 'warming'
        assert len(calls) == 1
    finally:
        release.set()


def test_investigation_search_is_loaded_once_and_not_needed_for_browsing():
    service = explorer()
    calls = []
    def source():
        calls.append(True)
        return {'101': {'INVESTIGATION': 'Fictional pump ASSET-009 investigation evidence.'}}
    service.additional_documents_loader = source
    service.search('', all_sites=True)
    assert not calls
    result = service.search('ASSET009', all_sites=True)
    assert [row['pr_id'] for row in result['results']] == ['101']
    assert result['results'][0]['matched_field'] == 'INVESTIGATION'
    service.search('investigation', all_sites=True)
    assert len(calls) == 1
