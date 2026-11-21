"""Query planning, hard constraints, immutable evidence and LLM failure isolation."""
import io
import json
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient
from match_v1.contracts import SearchRequest
from test_explorer import explorer
from capa_match import main
from capa_match.intelligence import AssistanceUnavailable, BoundedCache, SearchIntelligence, StructuredLLM, requested_years, verified_capa_line


class FakeLLM:
    configured = True
    def __init__(self, plan=None):
        self.plan = plan or {'retrieval_query': 'equipment calibration failure', 'years': [], 'criticalities': [], 'site': None}
        self.calls = []
        self.invalid_refs = False
        self.unavailable = False

    def invoke(self, name, instructions, data, schema, max_tokens):
        self.calls.append((name, instructions, data))
        if self.unavailable:
            raise AssistanceUnavailable('test provider failure')
        if name == 'match_query':
            return self.plan, {'model': 'fictional-model', 'input_tokens': 12, 'output_tokens': 8}
        if name == 'match_relevance':
            return {'ranked':[{'id':row['id'],'relationship':'related','evidence':row['title'] or row['description'][:120]} for row in data['records'][:data['maximum_results']]]}, {'input_tokens':30,'output_tokens':20}
        if name == 'match_grounding':
            return {'supported_lines': [], 'answer_line': None, 'evidence': []}, {'input_tokens': 10, 'output_tokens': 5}
        refs = ['UNKNOWN'] if self.invalid_refs else [data['records'][0]['id']]
        return {'lines': [{'text': text, 'references': refs} for text in [
            'The selected historical record describes a calibration event.',
            'The supplied evidence does not establish patient impact.']]}, {'input_tokens': 40, 'output_tokens': 30}


def request(query='find calibration failures', **kwargs):
    return SearchRequest(query=query, all_sites=True, assist=True, **kwargs)


def test_original_and_rewrite_are_both_searched_and_fused_without_mutating_request():
    service, llm = explorer(), FakeLLM()
    helper = SearchIntelligence(llm)
    req = request(filters={'years': [2021], 'criticalities': ['Minor']})
    result = helper.search(service, req)
    assert result['query'] == req.query
    assert result['submitted_criteria']['filters']['years'] == [2021]
    assert result['interpretation'] == {'query': llm.plan['retrieval_query'], 'origin': 'llm'}
    assert all(row['date_opened'].startswith('2021') and row['criticality'] == 'Minor' for row in result['results'])
    assert all(row['rank_fusion'] and row['match_score'] is not None for row in result['results'])
    assert result['total_matches'] is None and not result['has_more']
    assert req.filters.years == [2021] and req.query == 'find calibration failures'
    assert len({row['pr_id'] for row in result['results']}) == result['result_count']


@pytest.mark.parametrize('query,mode', [('EQ-001','auto'),('PR101','auto'),('101','auto'),('','auto'),('"HPLC calibration"','auto'),('HPLC','keyword'),('broad meaning','semantic')])
def test_explicit_literal_browse_and_manual_modes_do_not_call_query_llm(query, mode):
    llm = FakeLLM()
    SearchIntelligence(llm).search(explorer(), request(query, mode=mode))
    assert llm.calls == []


def test_long_query_keeps_identifier_hard_and_shows_investigation_evidence():
    service, llm = explorer(), FakeLLM()
    service.additional_documents_loader = lambda: {'101': {'INVESTIGATION': 'FICTIONAL equipment ASSET-009 failed.'}}
    query = 'please find historical cases where ASSET-009 had an issue during the calibration verification activities in our laboratory'
    result = SearchIntelligence(llm).search(service, request(query))
    assert result['required_identifiers'] == ['ASSET-009']
    assert [row['pr_id'] for row in result['results']] == ['101']
    assert result['results'][0]['matched_field'] == 'INVESTIGATION'
    assert result['results'][0]['matched_spans']
    unknown = SearchIntelligence(llm).search(service, request(query.replace('ASSET-009','ASSET-999')))
    assert unknown['results'] == [] and unknown['summary_id'] is None


def test_invented_rewrite_identifier_falls_back_and_keeps_source_id():
    llm = FakeLLM({'retrieval_query': 'calibration EQ-999 failure', 'years': [], 'criticalities': [], 'site': None})
    result = SearchIntelligence(llm).search(explorer(), request('calibration EQ-001 failures'))
    assert result['interpretation']['origin'] == 'local'
    assert all(row['pr_id'] in {'101','102'} for row in result['results'])


def test_selected_filters_and_site_are_never_relaxed_or_replaced():
    service = explorer()
    chosen_site = service.records['101']['site']
    llm = FakeLLM({'retrieval_query': 'calibration failure', 'years': [2022], 'criticalities': ['Minor'], 'site': 'Forbidden site'})
    req = SearchRequest(query='Minor calibration in 2022', site=chosen_site, filters={'years':[2020], 'criticalities':['Major']}, assist=True)
    result = SearchIntelligence(llm).search(service, req)
    assert result['site'] == chosen_site and result['filters']['years'] == [2020] and result['filters']['criticalities'] == ['Major']
    assert [row['pr_id'] for row in result['results']] == ['101']


def test_unauthorized_scope_fails_before_llm_or_evidence_is_sent():
    llm=FakeLLM()
    with pytest.raises(PermissionError):
        SearchIntelligence(llm).search(explorer(), SearchRequest(query='find calibration failures',site='FORBIDDEN',assist=True))
    assert not llm.calls


def test_unrequested_filters_and_unmentioned_site_cannot_be_invented():
    service=explorer()
    llm=FakeLLM({'retrieval_query':'calibration failure','years':[2020],'criticalities':['Critical'],'site':service.records['101']['site']})
    result=SearchIntelligence(llm).search(service,request('find calibration failures'))
    assert result['filters']['years'] == [] and result['filters']['criticalities'] == [] and result['all_sites']


def test_explicit_unavailable_year_stays_zero_instead_of_broadening_to_all_years():
    llm=FakeLLM({'retrieval_query':'HPLC calibration','years':[2099],'criticalities':[],'site':None})
    result=SearchIntelligence(llm).search(explorer(),request('HPLC calibration in 2099'))
    assert result['filters']['years'] == [2099] and result['results'] == []


def test_natural_filter_only_request_browses_and_reuses_plan_on_next_page():
    llm=FakeLLM({'retrieval_query':'','years':[2021],'criticalities':[],'site':None})
    helper=SearchIntelligence(llm)
    first=helper.search(explorer(),request('show all deviations in 2021',top_k=1))
    assert first['search_mode']=='browse' and first['total_matches']==2
    second=helper.search(explorer(),request('show all deviations in 2021',top_k=1,offset=1))
    assert first['results'][0]['pr_id'] != second['results'][0]['pr_id']
    assert len(llm.calls)==1


def test_empty_rewrite_cannot_discard_specific_event_words():
    llm=FakeLLM({'retrieval_query':'','years':[2021],'criticalities':[],'site':None})
    result=SearchIntelligence(llm).search(explorer(),request('show HPLC deviations in 2021'))
    assert result['search_mode']=='hybrid' and result['interpretation']['query']


def test_unavailable_llm_keeps_search_working_and_caches_fallback():
    llm=FakeLLM();llm.unavailable=True
    helper=SearchIntelligence(llm)
    first=helper.search(explorer(),request('OOS equipment calibration'))
    assert first['results'] and first['interpretation']['origin']=='local'
    assert 'out of specification' in first['interpretation']['query']
    helper.search(explorer(),request('OOS equipment calibration'))
    assert len(llm.calls)==1


def test_overview_only_uses_returned_authorized_records_and_preserves_capa_plan():
    service,llm=explorer(),FakeLLM()
    service.capas={'101':[{'pr_id':'CAPA-1','status':'Closed - Done','plan_excerpt':'FICTIONAL calibration frequency revised.'}]}
    helper=SearchIntelligence(llm)
    result=helper.search(service,request('101'))
    summary=helper.overview(service,result['summary_id'])
    assert len(summary['lines'])==3 and summary['reviewed_ids']==['101']
    evidence=llm.calls[-1][2]['records']
    assert evidence[0]['linked_capas'][0]['plan_excerpt']=='FICTIONAL calibration frequency revised.'
    assert {row['id'] for row in evidence}=={'101'}
    assert 'UNTRUSTED' in llm.calls[-1][1]
    repeat=helper.overview(service,result['summary_id'])
    assert repeat['cached'] and len(llm.calls)==2


def test_unknown_citations_are_rejected_instead_of_shown_as_evidence():
    llm=FakeLLM();llm.invalid_refs=True
    helper=SearchIntelligence(llm);service=explorer()
    result=helper.search(service,request('101'))
    summary=helper.overview(service,result['summary_id'])
    assert summary['verification_status']=='unavailable' and not summary['citations']
    assert summary['verification_attempts']['draft']==2


def test_concurrent_overview_requests_share_one_verified_pipeline():
    llm=FakeLLM();helper=SearchIntelligence(llm);service=explorer()
    token=helper.search(service,request('101'))['summary_id']
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows=list(pool.map(lambda _:helper.overview(service,token),range(2)))
    assert len(llm.calls)==2 and sum(row['cached'] for row in rows)==1


def test_changed_site_authority_or_snapshot_invalidates_cached_evidence():
    helper=SearchIntelligence(FakeLLM());service=explorer()
    token=helper.search(service,request('101'))['summary_id']
    service.allowed_sites=set()
    with pytest.raises(KeyError):helper.overview(service,token)


def test_same_evidence_reuses_overview_across_repeated_searches():
    helper=SearchIntelligence(FakeLLM());service=explorer()
    first=helper.search(service,request('101'))['summary_id']
    helper.overview(service,first)
    second=helper.search(service,request('101'))['summary_id']
    result=helper.overview(service,second)
    assert first!=second and result['search_id']==second and result['cached']
    assert len(helper.llm.calls)==2


def test_long_overview_gets_one_bounded_evidence_and_length_revision():
    llm=FakeLLM();helper=SearchIntelligence(llm);service=explorer();original=llm.invoke
    def long_first(name,instructions,data,*args):
        if name=='match_overview' and 'draft_to_check' not in data:
            return {'lines':[{'text':'Fictional retained source calibration evidence ' * 18 + '.', 'references':['101']} for _ in range(3)]},{'input_tokens':10,'output_tokens':20}
        if name=='match_overview':assert 'at most 40 words TOTAL' in instructions
        return original(name,instructions,data,*args)
    llm.invoke=long_first
    token=helper.search(service,request('101'))['summary_id']
    result=helper.overview(service,token)
    assert result['usage']['revision_pass'] and sum(len(line['text'].split()) for line in result['lines'])<=85
    assert helper.overview(service,token)['cached']


def test_explicit_site_alias_is_applied_even_when_model_omits_it():
    service=explorer()
    helper=SearchIntelligence(FakeLLM())
    plan=helper.interpret(service,request('HPLC calibration in Toronto'))
    assert plan['site']=='TRO (Toronto)' and not plan['all_sites']


def test_relevance_review_rejects_invented_ids_and_keeps_retrieved_candidates():
    llm=FakeLLM();helper=SearchIntelligence(llm);service=explorer()
    original=llm.invoke
    def altered(name,*args):
        if name=='match_relevance':return {'ranked':[{'id':'NOT-RETRIEVED','relationship':'direct','evidence':'invented'}]},{}
        return original(name,*args)
    llm.invoke=altered
    result=helper.search(service,request())
    assert result['results'] and all(row['pr_id'] in service.records for row in result['results'])
    assert not any('candidate_relationship' in row for row in result['results'])


def test_relevance_review_can_remove_incidental_candidates_and_add_sourced_reason():
    llm=FakeLLM();helper=SearchIntelligence(llm);service=explorer()
    original=llm.invoke
    def selected(name,instructions,data,*args):
        if name=='match_relevance':return {'ranked':[{'id':data['records'][-1]['id'],'relationship':'direct','evidence':data['records'][-1]['title']}]},{}
        return original(name,instructions,data,*args)
    llm.invoke=selected
    result=helper.search(service,request())
    assert len(result['results'])==1 and result['results'][0]['candidate_relationship']=='direct'
    assert result['results'][0]['match_reasons'][0].startswith('Source evidence: “')


def test_bounded_cache_evicts_and_expires():
    cache=BoundedCache(limit=1,ttl=5)
    cache.put('a',1);cache.put('b',2)
    assert cache.get('a') is None and cache.get('b')==2
    cache.items['b']=(0,2)
    assert cache.get('b') is None


def test_http_overview_validates_token_and_keeps_existing_contract(monkeypatch):
    service=explorer();helper=SearchIntelligence(FakeLLM())
    monkeypatch.setattr(main,'engine',lambda:service)
    monkeypatch.setattr(main,'assistant',lambda:helper)
    client=TestClient(main.app)
    result=client.post('/api/v1/match/search',json={'query':'101','all_sites':True,'assist':True}).json()
    response=client.post('/api/v1/match/overview',json={'search_id':result['summary_id']})
    assert response.status_code==200
    assert client.post('/api/v1/match/overview',json={'search_id':'0'*32}).status_code==404
    assert client.post('/api/v1/match/overview',json={'search_id':'invalid','records':[]}).status_code==422
    legacy=client.post('/api/v1/match/search',json={'query':'calibration failed','all_sites':True}).json()
    assert 'summary_id' not in legacy and 'submitted_criteria' not in legacy


def test_provider_uses_structured_nonstored_server_side_request(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','fictional-test-key')
    monkeypatch.setenv('MATCH_ASSIST_ENABLED','1')
    monkeypatch.setenv('OPENAI_BASE_URL','https://api.openai.com/v1')
    captured=[]
    def respond(req,timeout):
        captured.append(json.loads(req.data))
        return io.BytesIO(json.dumps({'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':'{"value":1}'}]}]}).encode())
    monkeypatch.setattr('capa_match.intelligence.urlopen',respond)
    result,_=StructuredLLM().invoke('test','fictional instructions',{'value':'fictional'}, {'type':'object'}, 50)
    assert result=={'value':1} and captured[0]['store'] is False
    assert captured[0]['text']['format']['strict'] and 'fictional-test-key' not in json.dumps(captured)


def test_provider_rejects_insecure_remote_endpoint(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','fictional-test-key');monkeypatch.setenv('MATCH_ASSIST_ENABLED','1')
    monkeypatch.setenv('OPENAI_BASE_URL','http://untrusted.example/v1')
    with pytest.raises(AssistanceUnavailable):StructuredLLM().invoke('test','',{}, {}, 50)


def test_evidence_review_uses_separately_configured_model(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','fictional-test-key');monkeypatch.setenv('MATCH_ASSIST_ENABLED','1')
    monkeypatch.setenv('OPENAI_BASE_URL','https://api.openai.com/v1')
    monkeypatch.setenv('MATCH_ASSIST_MODEL','fictional-query-model');monkeypatch.setenv('MATCH_REVIEW_MODEL','fictional-review-model')
    bodies=[]
    def respond(req,timeout):
        bodies.append(json.loads(req.data))
        return io.BytesIO(json.dumps({'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':'{}'}]}]}).encode())
    monkeypatch.setattr('capa_match.intelligence.urlopen',respond)
    client=StructuredLLM()
    client.invoke('match_query','',{}, {}, 50);client.invoke('match_relevance','',{}, {}, 50);client.invoke('match_overview','',{}, {}, 50)
    assert [body['model'] for body in bodies]==['fictional-query-model','fictional-review-model','fictional-review-model']


@pytest.mark.parametrize('query,expected', [
    ('deviations in 2025 and 2026',[2025,2026]),('deviations from 2023 to 2025',[2023,2024,2025]),
    ('cases between 2022 and 2024',[2022,2023,2024]),('this year',[2026]),('last year',[2025]),
    ('past 2 years',[2025,2026]),('since 2024',[2024,2025,2026]),('batch EQ-2025',None),
])
def test_requested_years_are_explicit_and_preserve_ranges(query,expected):
    assert requested_years(query,2026)==expected


def test_provider_failure_never_broadens_explicit_year_or_classification():
    llm=FakeLLM();llm.unavailable=True
    result=SearchIntelligence(llm).search(explorer(),request('show all Critical deviations in 2099'))
    assert result['search_mode']=='browse' and result['filters']['years']==[2099]
    assert result['filters']['criticalities']==['Critical'] and result['total_matches']==0


def test_invalid_relevance_quote_falls_back_to_recorded_title_without_displaying_fabrication():
    llm=FakeLLM();helper=SearchIntelligence(llm);service=explorer();original=llm.invoke
    def fabricated(name,instructions,data,*args):
        if name=='match_relevance':return {'ranked':[{'id':data['records'][0]['id'],'relationship':'direct','evidence':'Fabricated statement about a nonexistent patient impact.'}]},{}
        return original(name,instructions,data,*args)
    llm.invoke=fabricated
    result=helper.search(service,request())
    assert result['results'] and result['results'][0]['candidate_relationship']=='direct'
    assert 'Fabricated' not in result['results'][0]['match_reasons'][0]
    assert service.records[result['results'][0]['pr_id']]['record_name'] in result['results'][0]['match_reasons'][0]


def test_capa_sentence_uses_retained_links_not_narrative_mentions_or_llm_claims():
    sources=[{'pr_id':'A','related_capas':[{'pr_id':'C1','status':'Closed - Done'},{'pr_id':'C2','status':'Closed - Cancelled'}]},
             {'pr_id':'B','related_capas':[{'pr_id':'C1','status':'Closed - Done'}]}, {'pr_id':'C','related_capas':[]}]
    line=verified_capa_line(sources)
    assert line['text'].startswith('1 of 2 retained linked CAPAs is marked Closed - Done')
    assert 'no link is retained for 1 of 3' in line['text']
    assert line['references']==['A','B','C']
    missing=verified_capa_line([{'pr_id':'A','text':'A narrative mentions CAPA 999 as completed.','related_capas':[]}])
    assert missing['text'].startswith('No retained CAPA links')
