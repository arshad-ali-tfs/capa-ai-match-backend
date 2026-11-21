from concurrent.futures import ThreadPoolExecutor
from threading import Event
import pytest
from fastapi.testclient import TestClient
from capa_match.intelligence import SearchIntelligence
from capa_match import main
from test_explorer import explorer
from test_intelligence import FakeLLM,request


def test_full_result_cache_skips_retrieval_and_issues_a_fresh_bound_overview(monkeypatch):
    service=explorer();llm=FakeLLM();helper=SearchIntelligence(llm);calls=[];original=service.search
    def track(*args,**kwargs):calls.append(1);return original(*args,**kwargs)
    monkeypatch.setattr(service,'search',track)
    first=helper.search(service,request());retrievals=len(calls);model_calls=len(llm.calls)
    second=helper.search(service,request())
    assert second['cache_hit'] and second['results']==first['results']
    assert len(calls)==retrievals and len(llm.calls)==model_calls
    assert second['summary_id']!=first['summary_id'] and second['summary_id']
    second['results'][0]['record_name']='tampered client copy'
    assert helper.search(service,request())['results']==first['results']


def test_cache_cannot_cross_filters_authority_or_source_generation():
    service=explorer();helper=SearchIntelligence(FakeLLM())
    first=helper.search(service,request(filters={'criticalities':['Minor']}))
    other=helper.search(service,request(filters={'criticalities':['Major']}))
    assert not other['cache_hit'] and all(row['criticality']=='Major' for row in other['results'])
    service.manifest['source_snapshot_id']='changed-source'
    assert not helper.search(service,request(filters={'criticalities':['Minor']}))['cache_hit']
    service.allowed_sites=set()
    with pytest.raises(PermissionError):helper.search(service,request(filters={'criticalities':['Minor']}))


def test_simultaneous_identical_searches_share_the_complete_pipeline():
    service=explorer();helper=SearchIntelligence(FakeLLM())
    with ThreadPoolExecutor(max_workers=2) as pool:values=list(pool.map(lambda _:helper.search(service,request()),range(2)))
    assert sum(value['cache_hit'] for value in values)==1
    assert [call[0] for call in helper.llm.calls].count('match_query')==1
    assert [call[0] for call in helper.llm.calls].count('match_relevance')==1


def test_initial_candidates_arrive_without_waiting_for_relevance_and_have_no_overview():
    service=explorer();helper=SearchIntelligence(FakeLLM())
    initial=helper.search(service,request(progressive=True))
    assert initial['results'] and initial['review_state']=='pending' and initial['summary_id'] is None
    assert [call[0] for call in helper.llm.calls]==['match_query']
    assert all('candidate_relationship' not in row for row in initial['results'])
    final=helper.performance.review(service,initial['review_id'])
    blocking=SearchIntelligence(FakeLLM()).search(service,request())
    assert final['results']==blocking['results'] and final['filters']==blocking['filters']
    assert final['review_state']=='ai_reviewed' and final['summary_id']
    again=helper.search(service,request(progressive=True))
    assert again['cache_hit'] and again['review_state']=='ai_reviewed'


def test_duplicate_review_requests_share_the_same_result_and_model_call():
    service=explorer();helper=SearchIntelligence(FakeLLM())
    initial=helper.search(service,request(progressive=True))
    with ThreadPoolExecutor(max_workers=2) as pool:values=list(pool.map(lambda _:helper.performance.review(service,initial['review_id']),range(2)))
    assert values[0]['results']==values[1]['results']
    assert [call[0] for call in helper.llm.calls].count('match_relevance')==1


def test_cached_search_cannot_be_reused_for_a_different_result_limit_or_page():
    service=explorer();helper=SearchIntelligence(FakeLLM())
    helper.search(service,request('101',top_k=10))
    smaller=helper.search(service,request('101',top_k=1))
    assert not smaller['cache_hit'] and smaller['limit']==1


def test_review_rechecks_authority_and_rejects_injected_or_expired_handles():
    service=explorer();helper=SearchIntelligence(FakeLLM())
    initial=helper.search(service,request(progressive=True))
    with pytest.raises(KeyError):helper.performance.review(service,'0'*32)
    service.allowed_sites=set()
    with pytest.raises(KeyError):helper.performance.review(service,initial['review_id'])
    assert [call[0] for call in helper.llm.calls]==['match_query']


def test_provider_failure_keeps_local_candidates_and_never_claims_review_completion():
    service=explorer();llm=FakeLLM();helper=SearchIntelligence(llm)
    initial=helper.search(service,request(progressive=True));llm.unavailable=True
    final=helper.performance.review(service,initial['review_id'])
    assert final['review_state']=='local_ranking' and final['results']==initial['results']
    assert final['summary_id'] and all('candidate_relationship' not in row for row in final['results'])
    value=helper.performance.completed.get(helper.performance.key(service,request(progressive=True)))
    assert value['until']-__import__('time').monotonic()<=15


def test_mid_review_source_change_cannot_publish_or_cache_the_wrong_evidence():
    service=explorer();llm=FakeLLM();helper=SearchIntelligence(llm)
    initial=helper.search(service,request(progressive=True));original=llm.invoke
    def changed(name,*args):
        result=original(name,*args)
        if name=='match_relevance':service.manifest['source_snapshot_id']='replaced-while-reviewing'
        return result
    llm.invoke=changed
    with pytest.raises(KeyError):helper.performance.review(service,initial['review_id'])
    assert not helper.performance.completed.items


def test_http_progressive_contract_preserves_server_owned_sources(monkeypatch):
    service=explorer();helper=SearchIntelligence(FakeLLM())
    monkeypatch.setattr(main,'engine',lambda:service);monkeypatch.setattr(main,'assistant',lambda:helper)
    with TestClient(main.app) as client:
        result=client.post('/api/v1/match/search',json={'query':'find calibration failures','all_sites':True,'mode':'auto','assist':True,'progressive':True})
        assert result.status_code==200 and result.json()['review_state']=='pending'
        token=result.json()['review_id']
        assert client.post('/api/v1/match/review',json={'review_id':token,'records':[]}).status_code==422
        assert client.post('/api/v1/match/review',json={'review_id':'0'*32}).status_code==404
        final=client.post('/api/v1/match/review',json={'review_id':token})
        assert final.status_code==200 and final.json()['review_state']=='ai_reviewed'
        assert final.json()['summary_id']
