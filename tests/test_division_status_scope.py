"""Division/workflow scope, historical precedence and provisional source evidence."""
import csv
from copy import deepcopy
from datetime import date
import pytest
from match_v1.scope import workflow_state, status_counts
from match_v1.ingest import load_gtw
from match_v1.contracts import SearchFilters,SearchRequest
from capa_match.watchlist import Watchlist
from capa_match.intelligence import SearchIntelligence
from capa_match.division_scope import requested_divisions
from test_explorer import explorer
from test_watchlist import history

@pytest.mark.parametrize('value,expected',[
    ('Closed - Done','completed'),(' CLOSED — DONE ','completed'),('Closed - Cancelled','cancelled'),
    ('Closed - Canceled','cancelled'),('Open - Investigation','ongoing'),('In Progress','ongoing'),
    ('Pending QA Review','ongoing'),('Closed - Other','other'),('','other'),('Unknown','other')])
def test_source_workflow_categories_preserve_cancellation_and_unknowns(value,expected):
    assert workflow_state(value)==expected

def mixed():
    service=explorer()
    for key,division,status in [('101','DPD','Closed - Done'),('102','DSD','Open - Investigation'),
            ('103','CTD','Closed - Cancelled'),('104','Global','Closed - Done'),
            ('107','DPD','Closed - Done'),('108','DSD','Closed - Done')]:
        service.records[key].update(division=division,status=status)
    return service

def ids(result):return {row['pr_id'] for row in result['results']}

def test_defaults_are_completed_all_divisions_and_cancelled_requires_opt_in():
    service=mixed()
    assert ids(service.search('',all_sites=True))=={'101','104','107','108'}
    assert ids(service.search('',all_sites=True,filters={'record_scope':'all'}))=={'101','102','104','107','108'}
    assert ids(service.search('',all_sites=True,filters={'record_scope':'ongoing'}))=={'102'}
    result=service.search('',all_sites=True,filters={'record_scope':'all','include_cancelled':True})
    assert ids(result)=={'101','102','103','104','107','108'}
    assert ids(service.search('',all_sites=True,filters={'record_scope':'all','include_cancelled':True,'statuses':['Closed - Cancelled']}))=={'103'}
    assert not service.search('',all_sites=True,filters={'include_cancelled':True,'statuses':['Closed - Cancelled']})['results']

@pytest.mark.parametrize('mode,query',[('auto',''),('keyword','calibration'),('semantic','equipment calibration failed'),('auto','EQ001')])
def test_division_filters_constrain_every_retrieval_path_before_ranking(mode,query):
    service=mixed()
    result=service.search(query,all_sites=True,mode=mode,filters={'divisions':[' dpd ','Global']})
    assert result['results'] and all(row['division'] in {'DPD','Global'} and row['status']=='Closed - Done' for row in result['results'])
    assert ids(service.search('',all_sites=True,filters={'divisions':['DSD'],'record_scope':'all'}))=={'102','108'}

def test_disjunctive_division_facets_and_status_counts_describe_authorized_filtered_population():
    service=mixed();before=deepcopy(service.records)
    value=service.facets(all_sites=True,filters={'divisions':['DSD']})
    assert value['eligible_record_count']==1
    assert value['status_counts']=={'completed':1,'ongoing':1,'cancelled':0,'other':0}
    assert {row['value']:row['count'] for row in value['facets']['divisions']}=={'DPD':2,'DSD':1,'Global':1}
    assert service.records==before

def test_legacy_author_retrieval_excludes_ongoing_and_cancelled_cases():
    result=mixed().search_legacy('HPLC equipment calibration system suitability failed',all_sites=True,top_k=50)
    assert result['results'] and all(row['status']=='Closed - Done' for row in result['results'])
    assert not {'102','103'} & ids(result)

def test_repeated_filtered_queries_reuse_their_population_and_never_leak_between_divisions(monkeypatch):
    from match_v1 import search as module
    original=module.BM25;calls=[]
    def count_builds(docs):calls.append(1);return original(docs)
    monkeypatch.setattr(module,'BM25',count_builds)
    service=mixed()
    query='HPLC equipment calibration failed'
    for _ in range(2):
        value=service.search(query,all_sites=True,mode='semantic',filters={'divisions':['DPD']})
        assert all(row['division']=='DPD' and row['status']=='Closed - Done' for row in value['results'])
    assert len(calls)==1
    value=service.search(query,all_sites=True,mode='semantic',filters={'divisions':['DSD'],'record_scope':'all'})
    assert value['results'] and all(row['division']=='DSD' for row in value['results'])
    assert len(calls)==2

def test_explicit_division_text_is_resolved_without_overriding_selected_filters_or_asset_codes():
    service=mixed()
    assert requested_divisions(service,'show DPD and DSD deviations',[])==['DPD','DSD']
    assert requested_divisions(service,'show all except DPD',[])==['CTD','DSD','Global']
    assert requested_divisions(service,'all divisions except DPD and DSD',[])==['CTD','Global']
    assert requested_divisions(service,'all divisions except Global',[])==['CTD','DPD','DSD']
    assert requested_divisions(service,'DPD equipment issues',['CTD'])==['CTD']
    assert requested_divisions(service,'equipment DPD-001 failed',[])==[]
    assert requested_divisions(service,'global calibration issues',[])==[]
    assert requested_divisions(service,'Global division calibration issues',[])==['Global']
    service.records={key:row for key,row in service.records.items() if row['division']=='DPD'}
    assert requested_divisions(service,'show DSD deviations',[])==['DSD']

def test_division_only_assisted_browse_uses_filters_without_embedding_filler():
    from test_intelligence import FakeLLM
    service=mixed();service.embedder.encode=lambda _:pytest.fail('Division-only browsing must not embed filler')
    result=SearchIntelligence(FakeLLM()).search(service,SearchRequest(query='show DPD deviations',all_sites=True,assist=True))
    assert result['search_mode']=='browse' and ids(result)=={'101','107'} and result['filters']['divisions']==['DPD']

def test_watchlist_respects_submitted_division_and_status_scope_and_reports_separate_counts():
    service=history()
    for key,division,status in [('1','DPD','Closed - Done'),('2','DSD','Closed - Done'),('3','DPD','Open - Investigation'),('4','DPD','Closed - Cancelled')]:service.records[key].update(division=division,status=status)
    watch=Watchlist()
    value=watch.inspect(service,'1','LAB',date(2026,11,21),filters={'divisions':['DPD'],'record_scope':'all'})
    assert value['scope_record_count']==2 and value['other_record_count']==1 and value['selected_in_scope']
    assert value['status_counts']=={'completed':1,'ongoing':1,'cancelled':0,'other':0}
    pattern=next(row for row in value['signals'] if row['kind']=='pattern')
    assert pattern['status_counts']=={'completed':0,'ongoing':1,'cancelled':0,'other':0}
    assert pattern['other_record_count']==1 and all(ref['pr_id']=='3' for ref in pattern['evidence'])
    cancelled=watch.inspect(service,'1','LAB',filters={'divisions':['DPD'],'record_scope':'all','include_cancelled':True})
    assert cancelled['status_counts']['cancelled']==1
    different=watch.inspect(service,'1','LAB',filters={'divisions':['DSD']})
    assert different['scope_record_count']==different['other_record_count']==1 and not different['selected_in_scope']

def test_broader_ingestion_retains_future_active_cases_and_links_without_treating_capas_as_deviations(tmp_path):
    source=tmp_path/'fictional.csv'
    columns=['PR_ID','RECORD_TYPE','STATUS','SITE','PSG_Division','PARENT_ID','ROOT_PARENT_RECORD_TYPE','DETAILED_DESCRIPTION']
    with source.open('w',newline='',encoding='utf-8') as stream:
        writer=csv.DictWriter(stream,fieldnames=columns);writer.writeheader()
        for key,kind,division,status,parent,text in [('1','Deviation','DPD','Closed - Done','','HPLC failure.'),('2','Deviation','DSD','Open - Investigation','','Preliminary HPLC failure.'),('3','Deviation','CTD','Closed - Cancelled','','Cancelled report.'),('4','Deviation','Global','Closed - Done','',''),('5','CAPA','DSD','Open','2','Planned action.')]:
            writer.writerow(dict(PR_ID=key,RECORD_TYPE=kind,PSG_Division=division,STATUS=status,PARENT_ID=parent,SITE='LAB',DETAILED_DESCRIPTION=text))
    records,capas,stats=load_gtw(source,division='',limit=0,status_scope='all')
    assert {row['pr_id'] for row in records}=={'1','2','3'}
    assert capas['2'][0]['pr_id']=='5' and capas['2'][0]['status']=='Open'
    assert stats['status_scope']=='all' and stats['scope_policy_version']
    assert [row['pr_id'] for row in load_gtw(source,division='',limit=0)[0]]==['1']
    from capa_match.infrastructure.data.importer import import_snapshot
    from capa_match.infrastructure.data.repository import SqliteRecordRepository
    imported=SqliteRecordRepository(import_snapshot(source,tmp_path/'sources'))
    assert imported.get_record('2').status=='Open - Investigation'

@pytest.mark.parametrize('filters',[{'record_scope':'made up'},{'divisions':['']},{'statuses':['x'*201]}])
def test_invalid_scope_is_rejected_instead_of_widened(filters):
    with pytest.raises(ValueError):SearchFilters.model_validate(filters)

def test_watchlist_http_scope_and_invalid_query_contract(monkeypatch):
    from fastapi.testclient import TestClient
    from capa_match import main
    service=history()
    for key,division,status in [('1','DPD','Closed - Done'),('2','DSD','Closed - Done'),('3','DPD','Open - Investigation'),('4','DPD','Closed - Cancelled')]:service.records[key].update(division=division,status=status)
    monkeypatch.setattr(main,'engine',lambda:service)
    with TestClient(main.app) as client:
        endpoint='/api/v1/match/matched-records/1/watchlist'
        result=client.get(endpoint,params={'site':'LAB','divisions':'DPD','record_scope':'all','as_of':'2026-11-21'})
        assert result.status_code==200 and result.json()['scope_record_count']==2
        assert result.json()['status_counts']['ongoing']==1
        assert client.get(endpoint,params={'site':'DENIED','divisions':'DPD'}).status_code==403
        assert client.get(endpoint,params={'site':'LAB','record_scope':'invalid'}).status_code==422
        assert client.get(endpoint,params={'site':'LAB','divisions':''}).status_code==422

def test_answer_context_preserves_workflow_state_and_original_source_status():
    from test_direct_answers import AnswerLLM,SOURCE
    from test_watchlist import History,record
    from capa_match.answering import build_summary
    row=record('1',SOURCE);row.update(status='Open - Investigation',division='DSD')
    service=History([row]);llm=AnswerLLM()
    result=build_summary(service,{'query':'What actions were documented?','site':'LAB','results':[{'pr_id':'1','site':'LAB'}]},llm,'a'*32)
    context=llm.calls[0][2]['records'][0]
    assert context['status']=='Open - Investigation' and context['workflow_state']=='ongoing' and context['division']=='DSD'
    assert result['reviewed_status_counts']=={'completed':0,'ongoing':1,'cancelled':0,'other':0}
    assert 'provisional' in llm.calls[0][1] and 'Ongoing' in llm.calls[1][1]
