from copy import deepcopy
import pytest
from capa_match.answering import build_summary,question_kind
from capa_match.intelligence import AssistanceUnavailable
from test_watchlist import History,record

SOURCE='The investigation attributed the failure to an incorrect needle-wash setting. The analyst updated the wash settings and repeated the system suitability test, which passed.'
ACTION='The analyst updated the wash settings and repeated the system suitability test, which passed.'
CAUSE='The investigation attributed the failure to an incorrect needle-wash setting.'


class AnswerLLM:
    def __init__(self):self.calls=[];self.answer_line=0;self.bad_quote=False;self.no_support=False;self.bad_refs=False
    def invoke(self,name,instructions,data,schema,max_tokens):
        self.calls.append((name,instructions,data,schema))
        if name=='match_overview':
            lines=[{'text':'The analyst corrected the needle-wash settings and repeated system suitability successfully.','references':['1']},
                   {'text':'Investigators attributed the failure to an incorrect needle-wash setting.','references':['1']},
                   {'text':'No retained linked CAPA evidence was supplied.','references':['1']}]
            if self.bad_refs:lines[0]['references']=['INVENTED']
            return {'lines':lines},{'input_tokens':20,'output_tokens':20}
        return {'supported_lines':[] if self.no_support else [0,1],'answer_line':None if self.no_support else self.answer_line,
                'evidence':[{'line':0,'id':'1','field':'DETAILED_DESCRIPTION','quote':'An invented improvement was implemented.' if self.bad_quote else ACTION},
                            {'line':1,'id':'1','field':'DETAILED_DESCRIPTION','quote':CAUSE}]},{'input_tokens':20,'output_tokens':20}


def run(query,llm=None):
    service=History([record('1',SOURCE)])
    result={'query':query,'site':'LAB','source_snapshot_id':'fictional-source','filters':{},'results':[{'pr_id':'1','site':'LAB'}]}
    return build_summary(service,result,llm or AnswerLLM(),'f'*32)


def test_question_leads_with_direct_documented_action_and_verified_original_quotes():
    llm=AnswerLLM();summary=run('How were the HPLC system suitability failures addressed?',llm)
    assert summary['is_question'] and summary['answer_status']=='answered'
    assert summary['question_intent']=='actions' and 'corrected the needle-wash' in summary['lines'][0]['text']
    assert summary['citations'][0][0]['quote']==ACTION
    assert summary['citations'][0][0]['start']==SOURCE.index(ACTION)
    assert [call[0] for call in llm.calls]==['match_overview','match_grounding']
    assert llm.calls[1][2]['is_question'] and 'DIRECTLY ANSWER' in llm.calls[0][1]


def test_verified_answer_is_promoted_over_generic_first_sentence():
    llm=AnswerLLM();llm.answer_line=1
    summary=run('Why did the system suitability check fail?',llm)
    assert summary['answer_status']=='answered' and summary['lines'][0]['text'].startswith('Investigators attributed')
    assert summary['citations'][0][0]['quote']==CAUSE


def test_unsupported_answer_is_an_explicit_question_specific_gap_not_a_fabrication():
    llm=AnswerLLM();llm.no_support=True
    summary=run('What actions addressed the deviation?',llm)
    assert summary['answer_status']=='not_established' and 'which actions' in summary['lines'][0]['text']
    assert not summary['citations'] and 'corrected' not in str(summary['lines'])


def test_invented_quote_cannot_turn_a_plausible_answer_into_verified_evidence():
    llm=AnswerLLM();llm.bad_quote=True
    summary=run('What actions fixed the issue?',llm)
    assert summary['answer_status']=='not_established'
    assert 'corrected the needle-wash' not in str(summary['lines'])
    assert summary['support_check']['verified_sentences']==1


def test_unknown_ids_are_rejected_before_publishing_an_answer():
    llm=AnswerLLM();llm.bad_refs=True
    with pytest.raises(AssistanceUnavailable):run('What was done?',llm)


def test_keyword_search_retains_overview_mode_and_a_three_line_contract():
    summary=run('HPLC system suitability failures')
    assert not summary['is_question'] and summary['answer_status']=='overview' and len(summary['lines'])==3
    assert summary['reviewed_ids']==['1']


@pytest.mark.parametrize('query,expected',[
    ('Why did EQ-001 fail?',(True,'cause')),
    ('How was this corrected?',(True,'actions')),
    ('Did the issue recur after maintenance?',(True,'chronology')),
    ('What was the product impact?',(True,'evidence')),
    ('How many failures occurred?',(True,'aggregate')),
    ('HPLC system suitability failures',(False,'precedents')),
])
def test_question_and_intent_classification(query,expected):assert question_kind(query)==expected
