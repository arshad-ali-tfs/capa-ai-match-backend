import json
import pytest
from capa_match import cases
from capa_match.intelligence import SearchIntelligence
from match_v1.contracts import SearchRequest
from match_v1.search import SiteAccessError
from test_explorer import explorer
from test_intelligence import FakeLLM


def setup_case(monkeypatch,tmp_path):
    monkeypatch.setenv('CAPA_DATA_DIR',str(tmp_path))
    service=explorer(); helper=SearchIntelligence(FakeLLM())
    result=helper.search(service,SearchRequest(query='101',all_sites=True,assist=True))
    request=cases.CaseRequest(search_id=result['search_id'],title='Fictional investigator comparison',reviewer='Fictional reviewer',
        notes='Fictional rationale retained for a test.',decisions=[{'pr_id':'101','disposition':'include','rationale':'Same fictional event; differences reviewed.'}])
    return service,helper,result,request


def test_saved_case_survives_cache_expiry_with_exact_sources_and_criteria(monkeypatch,tmp_path):
    service,helper,result,request=setup_case(monkeypatch,tmp_path)
    helper.overview(service,result['summary_id'])
    saved=cases.save(service,helper,request)
    helper.searches.items.clear();helper.overviews.items.clear()
    loaded=cases.get(saved['id'],service)
    assert loaded['criteria']==result['submitted_criteria']
    assert loaded['sources'][0]['pr_id']=='101' and loaded['overview']['lines']
    assert loaded['decisions'][0]['rationale']==request.decisions[0].rationale
    assert len(loaded['provenance']['assistant_recipe']['sha256'])==64
    assert cases.list_cases(service)[0]['id']==saved['id']


def test_excluded_case_evidence_is_retained_for_its_exclusion_rationale(monkeypatch,tmp_path):
    service,helper,result,request=setup_case(monkeypatch,tmp_path)
    request.decisions[0].disposition='exclude'
    saved=cases.save(service,helper,request)
    assert saved['sources'][0]['pr_id']=='101' and saved['decisions'][0]['disposition']=='exclude'


def test_saved_case_is_not_visible_after_site_authority_is_removed(monkeypatch,tmp_path):
    service,helper,result,request=setup_case(monkeypatch,tmp_path)
    saved=cases.save(service,helper,request)
    service.allowed_sites=set()
    with pytest.raises(SiteAccessError):cases.get(saved['id'],service)
    assert cases.list_cases(service)==[]


def test_client_cannot_invent_a_candidate_or_save_expired_evidence(monkeypatch,tmp_path):
    service,helper,result,request=setup_case(monkeypatch,tmp_path)
    request.decisions[0].pr_id='NOT-RETRIEVED'
    with pytest.raises(ValueError):cases.save(service,helper,request)
    request.search_id='0'*32
    with pytest.raises(KeyError):cases.save(service,helper,request)


def test_metadata_and_source_changes_are_explicit_on_reopening(monkeypatch,tmp_path):
    service,helper,result,request=setup_case(monkeypatch,tmp_path)
    saved=cases.save(service,helper,request)
    service.manifest['source_snapshot_id']='changed-source'
    service.manifest['storage_directory']='generations/new'
    loaded=cases.get(saved['id'],service)
    assert loaded['source_changed'] and loaded['index_changed']
    assert loaded['sources']==saved['sources']


def test_empty_search_can_be_saved_without_invented_sources(monkeypatch,tmp_path):
    service,helper,_,_=setup_case(monkeypatch,tmp_path)
    result=helper.search(service,SearchRequest(query='EQ-NONEXISTENT-99999999',all_sites=True,assist=True))
    assert result['summary_id'] is None and result['search_id']
    request=cases.CaseRequest(search_id=result['search_id'],title='No matching asset',reviewer='Reviewer',notes='Exact identifier returned no source records.')
    saved=cases.save(service,helper,request)
    assert saved['sources']==saved['result']['results']==[]


def test_temporal_query_is_not_a_direct_match_merely_because_an_asset_matches():
    service=explorer();helper=SearchIntelligence(FakeLLM())
    result=helper.search(service,SearchRequest(query='Equipment readings failed after maintenance',all_sites=True,assist=True))
    assert result['results'] and all(row['candidate_relationship']=='related' and row['temporal_verification'] for row in result['results'])
    summary=helper.overview(service,result['summary_id'])
    assert 'not verified' in summary['lines'][0]['text']


def test_unsupported_summary_claim_is_removed_when_no_original_quote_supports_it():
    service=explorer();llm=FakeLLM();original=llm.invoke
    def ungrounded(name,*args):
        if name=='match_grounding':return {'supported_lines':[0,1],'answer_line':None,'evidence':[{'line':0,'passage_id':'INVENTED'}]},{}
        return original(name,*args)
    llm.invoke=ungrounded;helper=SearchIntelligence(llm)
    result=helper.search(service,SearchRequest(query='101',all_sites=True,assist=True))
    summary=helper.overview(service,result['summary_id'])
    assert summary['verification_status']=='unavailable' and not summary['citations']
    assert summary['verification_attempts']['grounding']==3
    assert 'Invented patient impact' not in str(summary['lines'])
