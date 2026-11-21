"""Automatic repair must terminate without weakening original-source verification."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError, URLError
from io import BytesIO
import json
import pytest
from capa_match.answering import draft_lines
from capa_match.intelligence import AssistanceUnavailable, SearchIntelligence, StructuredLLM
from capa_match.overview_resilience import OverviewSession, OverviewUnavailable, provider_timeout, _deadline
from test_direct_answers import AnswerLLM, ACTION, SOURCE, run
from test_explorer import explorer
from test_intelligence import request


class Scripted(AnswerLLM):
    def __init__(self, draft_failures=0, grounding_failures=0, failure='format'):
        super().__init__();self.remaining={'match_overview':draft_failures,'match_grounding':grounding_failures};self.failure=failure
    def invoke(self,name,instructions,data,schema,max_tokens):
        if self.remaining.get(name,0):
            self.remaining[name]-=1;self.calls.append((name,instructions,deepcopy(data),schema))
            if self.failure=='transport':raise AssistanceUnavailable('Private provider details must not be displayed.')
            if self.failure=='permanent':raise AssistanceUnavailable('Private authentication details.',reason='request_rejected',retryable=False)
            return {'lines':[]},{'input_tokens':5,'output_tokens':5}
        return super().invoke(name,instructions,data,schema,max_tokens)


@pytest.mark.parametrize('phase',['draft','grounding'])
@pytest.mark.parametrize('failure',['format','transport'])
def test_temporary_or_malformed_output_is_repaired_internally_then_source_verified(phase,failure):
    llm=Scripted(**{phase+'_failures':1},failure=failure)
    value=run('What actions addressed the failure?',llm)
    assert value['answer_status']=='answered' and value['verification_status']=='verified'
    assert value['verification_attempts'][phase]==2 and value['verification_retries']==1
    assert value['citations'][0][0]['quote']==ACTION
    for _,_,payload,_ in llm.calls:assert [row['id'] for row in payload['records']]==['1']
    assert 'Private provider' not in str(value)


@pytest.mark.parametrize('phase',['draft','grounding'])
def test_repeated_bad_output_terminates_with_honest_unavailable_message_not_claim_of_missing_records(phase):
    llm=Scripted(**{phase+'_failures':99})
    value=run('Why did the failure happen?',llm)
    assert value['verification_status']=='unavailable' and value['answer_status']=='not_established'
    assert value['verification_attempts'][phase]==3 and len(llm.calls)<=4
    assert not value['citations'] and 'records lack an answer' in value['lines'][1]['text']
    assert 'Private' not in str(value) and 'Investigation' not in str(value['lines'])


def test_authentication_configuration_failure_does_not_waste_retries_or_claim_evidence_is_missing():
    llm=Scripted(draft_failures=99,failure='permanent')
    value=run('What was done?',llm)
    assert value['verification_status']=='unavailable' and value['verification_reason']=='request_rejected'
    assert len(llm.calls)==1 and value['verification_retries']==0


def test_valid_finding_of_insufficient_evidence_is_not_retried_until_model_agrees():
    llm=AnswerLLM();llm.no_support=True
    value=run('What actions resolved the issue?',llm)
    assert value['verification_status']=='insufficient_evidence' and value['answer_status']=='not_established'
    assert len(llm.calls)==2 and value['verification_retries']==0 and not value['citations']


def test_missing_punctuation_and_duplicate_inline_citations_are_formatting_not_provider_errors():
    lines=[{'text':'The analyst corrected the settings [1]','references':['1']},
           {'text':'The source reported a setting error.','references':['1']}]
    value=draft_lines({'lines':lines},['1'])
    assert value[0]['text']=='The analyst corrected the settings.' and lines[0]['text'].endswith('[1]')


def test_inline_citation_outside_verified_reference_list_requires_repair():
    from capa_match.overview_resilience import InvalidModelOutput
    lines=[{'text':'The analyst corrected the settings [1, 2].','references':['1']},
           {'text':'The source reported a setting error.','references':['1']}]
    with pytest.raises(InvalidModelOutput,match='inline_citation_mismatch'):draft_lines({'lines':lines},['1','2'])


def test_invented_supporting_quote_is_retried_once_and_never_published_as_an_answer():
    llm=AnswerLLM();llm.bad_quote=True
    value=run('What actions were taken?',llm)
    assert value['verification_status']=='unavailable' and value['verification_attempts']=={'draft':1,'grounding':3}
    assert not value['citations'] and 'corrected the needle-wash' not in str(value['lines'])


def test_time_budget_stops_before_another_provider_call_and_does_not_escape_request():
    session=OverviewSession(AnswerLLM());session.deadline=0
    with pytest.raises(OverviewUnavailable,match='time_budget'):
        session.invoke('draft','match_overview','',{}, {},20,lambda value:value)
    assert not session.llm.calls and _deadline.get() is None
    assert provider_timeout(30)==30


def test_concurrent_callers_share_the_same_repair_and_verified_result():
    from test_watchlist import History,record
    service=History([record('1',SOURCE)]);service.source_ids={}
    llm=Scripted(draft_failures=1);helper=SearchIntelligence(llm)
    # A known source record is enough to seed the same protected overview entry.
    helper.authority=lambda _:('fictional-authority',)
    entry={'authority':helper.authority(service),'result':{'query':'What actions were taken?','site':'LAB','results':[{'pr_id':'1','site':'LAB'}]},'lock':__import__('threading').Lock(),'summary':None}
    helper.searches.put('a'*32,entry)
    with ThreadPoolExecutor(max_workers=2) as pool:values=list(pool.map(lambda _:helper.overview(service,'a'*32),range(2)))
    assert len(llm.calls)==3 and sum(value['cached'] for value in values)==1
    assert all(value['answer_status']=='answered' for value in values)


def test_provider_failure_cache_has_short_cooldown_and_recovers_without_changing_query():
    llm=Scripted(draft_failures=2,failure='transport');helper=SearchIntelligence(llm)
    from test_watchlist import History,record
    service=History([record('1',SOURCE)]);helper.authority=lambda _:('same',)
    entry={'authority':helper.authority(service),'result':{'query':'What actions were taken?','site':'LAB','results':[{'pr_id':'1','site':'LAB'}]},'lock':__import__('threading').Lock(),'summary':None}
    helper.searches.put('a'*32,entry)
    assert helper.overview(service,'a'*32)['verification_status']=='unavailable'
    assert helper.overview(service,'a'*32)['cached'] and len(llm.calls)==2
    entry['unavailable_until']=0
    recovered=helper.overview(service,'a'*32)
    assert recovered['verification_status']=='verified' and len(llm.calls)==4


@pytest.mark.parametrize('status,retryable',[(429,True),(503,True),(401,False),(403,False),(400,False)])
def test_provider_failures_are_classified_without_returning_sensitive_upstream_body(monkeypatch,status,retryable):
    monkeypatch.setenv('OPENAI_API_KEY','fictional-test-key');monkeypatch.setenv('MATCH_ASSIST_ENABLED','1')
    monkeypatch.setenv('OPENAI_BASE_URL','https://api.openai.com/v1')
    def fail(*args,**kwargs):raise HTTPError('https://api.openai.com/v1/responses',status,'private provider text',{},BytesIO(b'private body'))
    monkeypatch.setattr('capa_match.intelligence.urlopen',fail)
    with pytest.raises(AssistanceUnavailable) as caught:StructuredLLM().invoke('match_overview','',{}, {},50)
    assert caught.value.retryable is retryable and 'private' not in str(caught.value)


def test_provider_socket_timeout_is_clamped_to_overview_budget(monkeypatch):
    from time import monotonic
    monkeypatch.setenv('OPENAI_API_KEY','fictional-test-key');monkeypatch.setenv('MATCH_ASSIST_ENABLED','1')
    monkeypatch.setenv('OPENAI_BASE_URL','https://api.openai.com/v1');monkeypatch.setenv('MATCH_ASSIST_TIMEOUT_SECONDS','30')
    seen=[]
    def respond(req,timeout):
        seen.append(timeout);return BytesIO(json.dumps({'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':'{}'}]}]}).encode())
    monkeypatch.setattr('capa_match.intelligence.urlopen',respond)
    token=_deadline.set(monotonic()+1)
    try:StructuredLLM().invoke('match_overview','',{}, {},50)
    finally:_deadline.reset(token)
    assert 0<seen[0]<=1


def test_service_hiccup_does_not_consume_draft_or_grounding_repair_allowances():
    class Combined(AnswerLLM):
        def __init__(self):super().__init__();self.number=0
        def invoke(self,name,instructions,data,schema,max_tokens):
            self.number+=1
            if self.number==1:
                self.calls.append((name,instructions,data,schema));raise AssistanceUnavailable('Temporary test outage')
            if self.number in {2,4}:
                self.calls.append((name,instructions,data,schema));return {},{'input_tokens':1,'output_tokens':1}
            return super().invoke(name,instructions,data,schema,max_tokens)
    llm=Combined();value=run('What actions were taken?',llm)
    assert value['answer_status']=='answered' and value['verification_status']=='verified'
    assert value['verification_attempts']=={'draft':3,'grounding':2}
    assert value['verification_provider_retries']==1 and value['verification_retries']==3 and len(llm.calls)==5


def test_provider_retry_allowance_is_shared_across_both_stages():
    llm=Scripted(draft_failures=1,grounding_failures=1,failure='transport')
    value=run('What actions were taken?',llm)
    assert value['verification_status']=='unavailable' and value['verification_provider_retries']==1
    assert value['verification_attempts']=={'draft':2,'grounding':1} and len(llm.calls)==3


def test_unused_transport_recovery_can_repair_a_second_format_failure_without_exceeding_five_calls():
    llm=Scripted(draft_failures=2,grounding_failures=1)
    value=run('What actions were taken?',llm)
    assert value['verification_status']=='verified' and value['answer_status']=='answered'
    assert value['verification_attempts']=={'draft':3,'grounding':2} and len(llm.calls)==5
