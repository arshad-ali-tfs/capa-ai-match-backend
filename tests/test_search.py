from pathlib import Path
import pytest
from match_v1.core import MockEmbedder, split_chunks
from match_v1.ingest import load_gtw
from match_v1.search import MatchService
from match_v1.testing import MemoryCollection

DEMO = Path(__file__).parent / 'fixtures' / 'demo_gtw.csv'


def make_service():
    records, capas, _ = load_gtw(DEMO, division='DPD', limit=0)
    chunks = []
    for rec in records:
        for seq, part in enumerate(split_chunks(rec['text'])):
            chunks.append({'chunk_id':f"{rec['pr_id']}_chunk_{seq}", 'pr_id':rec['pr_id'],
                           'site':rec['site'], 'division':rec['division'], 'doc':part})
    model, coll = MockEmbedder(), MemoryCollection()
    coll.upsert(ids=[x['chunk_id'] for x in chunks], documents=[x['doc'] for x in chunks],
                metadatas=[{field:x[field] for field in ('pr_id','site','division')} for x in chunks],
                embeddings=model.encode([x['doc'] for x in chunks]).tolist())
    manifest={'model_name':model.model_name, 'embedding_backend':'MOCK_UNFIT_FOR_REAL_SEARCH',
              'site_counts':{'TRO (Toronto)': 6,'GRC (Greenville, NC)': 2},
              'chunk_size':500,'chunk_overlap':50}
    return MatchService(manifest=manifest, records={x['pr_id']:x for x in records}, chunks=chunks,
                        capas=capas, collection=coll, embedder=model, allowed_sites={'TRO (Toronto)'},candidate_k=3)


def test_search_returns_ranked_scoped_results():
    srvc = make_service()
    result = srvc.search('HPLC bracket system suitability failed during testing of six lots',
                         site='TRO (Toronto)', top_k=5)
    assert result['result_count'] >= 3
    assert len({r['pr_id'] for r in result['results']}) == result['result_count']
    assert all(r['site'] == 'TRO (Toronto)' for r in result['results'])
    assert result['results'][0]['pr_id'] == '101'
    assert result['results'][0]['related_capa_count'] == 1
    assert result['results'][0]['related_capas'][0]['pr_id'] == '901'
    for row in result['results']:
        expected=.7*row['semantic_score']+.2*row['bm25_score']+.1*row['jaccard_score']
        assert abs(row['match_score']-expected)<0.000004


def test_unauthorized_site_is_rejected():
    srvc=make_service()
    with pytest.raises(PermissionError):
        srvc.search('HPLC failure and calibration problem', site='GRC (Greenville, NC)')
    with pytest.raises(PermissionError):
        srvc.record('105', site='GRC (Greenville, NC)')


def test_short_keywords_search_and_meaningless_query_is_rejected():
    result = make_service().search('HPLC', site='TRO (Toronto)')
    assert result['search_mode'] == 'keyword' and result['results']
    with pytest.raises(ValueError):
        make_service().search('???', site='TRO (Toronto)')


def test_retrieval_with_keyword_candidates_not_in_semantic_topk():
    srvc = make_service()
    srvc.candidate_k = 1
    # Force the semantic branch to return an unrelated chunk, to prove that
    # lexical retrieval can rescue an exact phrase regardless of ANN results.
    original = srvc.collection.query
    def forced_semantic(query_embeddings, n_results, where, include):
        output = original(query_embeddings=query_embeddings, n_results=n_results, where=where, include=include)
        output['ids'] = [['101_chunk_0'] for _ in output['ids']]
        return output
    srvc.collection.query = forced_semantic
    result = srvc.search('balance calibration verification failed on samples',
                         site='TRO (Toronto)', top_k=6)
    assert result['keyword_only_candidate_chunks'] > 0
    assert any(r['pr_id']=='108' for r in result['results'])


def test_no_cross_site_record_access():
    srvc=make_service()
    with pytest.raises(KeyError):
        srvc.record('105', site='TRO (Toronto)')


def test_all_sites_ranks_one_combined_corpus_and_preserves_single_site_results():
    service = make_service()
    service.candidate_k = 100
    query = 'HPLC bracket system suitability failed during testing of six lots'
    baseline = service.search(query, site='TRO (Toronto)', top_k=5)
    service.allowed_sites.add('GRC (Greenville, NC)')
    filters = []
    original = service.collection.query
    def capture(**kwargs):
        filters.append(kwargs['where'])
        return original(**kwargs)
    service.collection.query = capture
    result = service.search(query, all_sites=True, top_k=8)
    assert result['all_sites'] and result['site'] == 'All sites'
    assert result['searched_sites'] == ['GRC (Greenville, NC)', 'TRO (Toronto)']
    assert filters == [{'site': {'$in': result['searched_sites']}}]
    assert result['result_count'] == 8
    assert {row['site'] for row in result['results']} == service.allowed_sites
    assert len({row['pr_id'] for row in result['results']}) == 8
    scores = [row.get('relevance_score', row['match_score']) for row in result['results']]
    assert scores == sorted(scores, reverse=True)
    for row in result['results']:
        expected = .7 * row['semantic_score'] + .2 * row['bm25_score'] + .1 * row['jaccard_score']
        assert row['match_score'] == pytest.approx(expected, abs=.000004)
    assert service.search(query, site='TRO (Toronto)', top_k=5) == baseline
    # A cached broader corpus must never be reused after the allowed scope narrows.
    service.allowed_sites.remove('GRC (Greenville, NC)')
    restricted = service.search(query, all_sites=True, top_k=8)
    assert restricted['searched_sites'] == ['TRO (Toronto)']
    assert restricted['result_count'] == 6
    assert all(row['site'] == 'TRO (Toronto)' for row in restricted['results'])


def test_all_sites_keyword_candidates_cannot_reach_a_disallowed_site():
    service = make_service()
    service.candidate_k = 100
    result = service.search('Electronic batch record missing operator signature correction', all_sites=True, top_k=50)
    assert result['searched_sites'] == ['TRO (Toronto)']
    assert {row['pr_id'] for row in result['results']}.isdisjoint({'105', '106'})
    assert all(row['site'] == 'TRO (Toronto)' for row in result['results'])
    with pytest.raises(PermissionError):
        service.record('106', site='GRC (Greenville, NC)')


def test_all_sites_keyword_branch_recovers_a_case_from_another_allowed_site():
    service = make_service()
    service.allowed_sites.add('GRC (Greenville, NC)')
    service.candidate_k = 1
    original = service.collection.query
    def unrelated_semantic(**kwargs):
        result = original(**kwargs)
        result['ids'] = [['101_chunk_0'] for _ in result['ids']]
        return result
    service.collection.query = unrelated_semantic
    result = service.search('Electronic batch record missing operator signature correction', all_sites=True, top_k=5)
    assert result['keyword_only_candidate_chunks'] > 0
    assert result['results'][0]['pr_id'] == '106'
    assert result['results'][0]['site'] == 'GRC (Greenville, NC)'


def test_all_sites_refuses_conflicting_scope_and_corrupt_site_metadata():
    service = make_service()
    with pytest.raises(ValueError, match='not both'):
        service.search('HPLC bracket suitability failure', site='TRO (Toronto)', all_sites=True)
    service.allowed_sites.add('GRC (Greenville, NC)')
    service.candidate_k = 100
    service.records['105']['site'] = 'TRO (Toronto)'
    with pytest.raises(RuntimeError, match='selected sites'):
        service.search('HPLC bracket suitability failure', all_sites=True)


def test_empty_all_sites_search_finishes_with_explicit_scope():
    service = make_service()
    service.chunks = []
    result = service.search('HPLC bracket suitability failure', all_sites=True)
    assert result['all_sites'] and result['searched_sites'] == ['TRO (Toronto)']
    assert result['results'] == [] and result['result_count'] == 0
