from threading import Event
from time import sleep
from capa_match import main,warmup
from test_explorer import explorer


def test_full_warmup_preserves_ranking_and_prepares_titles_and_scoped_keyword_statistics():
    original=explorer().search('HPLC calibration failed',all_sites=True,mode='semantic')
    service=explorer();report=warmup.prepare_search(service)
    actual=service.search('HPLC calibration failed',all_sites=True,mode='semantic')
    assert actual['results']==original['results'] and report['titles']>0
    assert report['keyword_populations']>0 and service._lexical_index is not None
    assert service._explorer.prepared_titles.loaded


def test_repeated_readiness_checks_start_only_one_complete_warmup(monkeypatch):
    service=explorer();service.manifest['embedding_backend']='MPNet'
    started,release=Event(),Event();calls=[]
    def prepare(value):calls.append(value);started.set();assert release.wait(5)
    monkeypatch.setattr(warmup,'prepare_search',prepare)
    assert main.query_model_state(service)=='warming' and started.wait(5)
    assert main.query_model_state(service)=='warming' and len(calls)==1
    release.set()
    for _ in range(100):
        if main.query_model_state(service)=='ready':break
        sleep(.01)
    assert main.query_model_state(service)=='ready' and len(calls)==1


def test_warmup_failure_is_reported_without_claiming_search_is_prepared(monkeypatch):
    service=explorer();service.manifest['embedding_backend']='MPNet'
    finished=Event()
    def fail(value):finished.set();raise OSError('fictional inaccessible cache')
    monkeypatch.setattr(warmup,'prepare_search',fail)
    main.query_model_state(service);assert finished.wait(5)
    for _ in range(100):
        if main.query_model_state(service)=='unavailable':break
        sleep(.01)
    assert main.query_model_state(service)=='unavailable'


def test_startup_initiates_full_warmup_before_the_first_search(monkeypatch):
    from fastapi.testclient import TestClient
    service=explorer();service.manifest['embedding_backend']='MPNet';started=Event()
    monkeypatch.setenv('MATCH_WARM_SEARCH','1');monkeypatch.setattr(main,'engine',lambda:service)
    monkeypatch.setattr(warmup,'prepare_search',lambda value:started.set())
    with TestClient(main.app) as client:
        assert client.get('/api/v1/match/health').status_code==200 and started.wait(5)


def test_warmup_prepares_retained_literal_fields_before_the_first_equipment_lookup():
    service=explorer();calls=[]
    def retained():calls.append(1);return {'101':{'INVESTIGATION':'Fictional ASSET-009 investigation evidence.'}}
    service.additional_documents_loader=retained
    warmup.prepare_search(service)
    assert calls==[1] and service._explorer._additional_loaded
    result=service.search('ASSET-009',all_sites=True)
    assert [row['pr_id'] for row in result['results']]==['101'] and calls==[1]
