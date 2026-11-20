from copy import deepcopy
from datetime import date
import csv
import pytest
from fastapi.testclient import TestClient
from match_v1.search import SiteAccessError
from capa_match import main
from capa_match.watchlist import Watchlist,entities,events


def record(key,text,site='LAB',opened='2026-03-01',title='Recorded laboratory event'):
    return {'pr_id':key,'site':site,'record_name':title,'date_opened':opened,'criticality':'Minor','status':'Closed - Done',
            'indexed_field_texts':{'DETAILED_DESCRIPTION':text},'text':text,'division':'DS'}


class History:
    def __init__(self,rows):
        self.records={row['pr_id']:row for row in rows};self.allowed_sites={'LAB'};self.capas={}
        self.manifest={'source_snapshot_id':'fictional-source','storage_directory':'fictional-generation','site_counts':dict((site,sum(row['site']==site for row in rows)) for site in {'LAB','DENIED'})}
    def _authorized_site(self,site):
        if site not in self.allowed_sites:raise SiteAccessError('Denied')
        return site
    def record(self,key,site):
        self._authorized_site(site)
        if key not in self.records or self.records[key]['site']!=site:raise KeyError(key)
        return {**self.records[key],'related_capas':[],'source_snapshot_id':self.manifest['source_snapshot_id']}


def history():return History([
    record('1','UHPLC327 system suitability failed. Bracket standard was out of specification.','LAB','2020-01-01'),
    record('2','UHPLC327 system suitability failed during assay.','LAB','2021-02-01'),
    record('3','UHPLC3270 system suitability failed during assay.','LAB','2026-05-01'),
    record('4','UHPLC327 was listed in related procedures. All tests passed.','LAB','2026-06-01'),
    record('5','UHPLC327 system suitability failed during assay.','DENIED','2026-07-01'),
])


def test_counts_scan_full_same_site_index_excluding_selected_and_not_prefixes_or_duplicate_mentions():
    service=history();service.records['2']['indexed_field_texts']['DETAILED_DESCRIPTION']+=' UHPLC327 UHPLC327.'
    before=deepcopy(service.records)
    result=Watchlist().inspect(service,'1','LAB',date(2026,11,21))
    asset=next(row for row in result['signals'] if row['kind']=='equipment')
    assert result['scope_record_count']==4 and result['other_record_count']==3
    assert asset['other_record_count']==2 and asset['failure_indicator_count']==1
    assert asset['last_12_months']==1 and asset['last_opened']=='2026-06-01'
    assert {row['pr_id'] for row in asset['evidence']}=={'2','4'}
    assert service.records==before


def test_matching_event_indicators_are_separate_from_shared_equipment_mentions():
    result=Watchlist().inspect(history(),'1','LAB',date(2026,11,21))
    pattern=next(row for row in result['signals'] if row['key']=='pattern:system_suitability')
    assert pattern['other_record_count']==2
    assert {row['pr_id'] for row in pattern['evidence']}=={'2','3'}


@pytest.mark.parametrize('text',[
    'No system suitability failures were observed.',
    'System suitability failure was not observed.',
    'Calibration did not fail and verification passed.',
    'Historical review of previous deviations identified system suitability failures.',
])
def test_negated_or_explicit_historical_reference_text_is_not_an_event_indicator(text):
    assert not events(record('1',text,title='Laboratory record'))


def test_known_equipment_variants_preserve_zeroes_and_exclude_batch_method_and_product_ids():
    result=entities(record('1','HPLC LC-029 failed. UHPLC327 failed. Equipment ID: EQ-001. GSK2140944 Batch CFCCT, 3800010730; Method CTMLP-4934. SSID 34235 RSID 47036.'))
    labels={row['label'] for row in result.values()}
    assert {'LC-029','UHPLC327','EQ-001'}<=labels
    assert not labels.intersection({'GSK2140944','CFCCT','3800010730','CTMLP-4934','34235','47036'})
    assert not entities(record('1','Batch EQ-001. Method HPLC-029.'))


def test_punctuation_equivalence_and_leading_zeroes_are_explicit():
    service=History([record('1','EQ-001 calibration failed.'),record('2','EQ 001 calibration failed.'),record('3','EQ0010 calibration failed.'),record('4','EQ01 calibration failed.')])
    asset=next(row for row in Watchlist().inspect(service,'1','LAB')['signals'] if row['kind']=='equipment')
    assert asset['other_record_count']==1 and asset['evidence'][0]['pr_id']=='2'


def test_evidence_offsets_address_exact_original_unicode_text_and_are_bounded():
    source='Fictional 🌡️ evidence. '+('unrelated words '*150)+'UHPLC327 system suitability failed. '+('retained text '*100)
    service=History([record('1','UHPLC327 system suitability failed.'),record('2',source)])
    result=Watchlist().inspect(service,'1','LAB')
    for signal in result['signals']:
        for proof in signal['evidence']:
            text=service.records[proof['pr_id']]['indexed_field_texts'][proof['field']]
            assert text[proof['start']:proof['end']]==proof['quote'] and len(proof['quote'])<=500


def test_missing_dates_zero_matches_and_no_recognised_signals_remain_distinct():
    service=History([record('1','EQ-009 calibration failed.',opened=''),record('2','Packaging reconciliation was incomplete.',opened='bad-date')])
    result=Watchlist().inspect(service,'1','LAB')
    assert result['undated_count']==2 and result['opened_from'] is None
    assert all(row['other_record_count']==0 for row in result['signals'])
    empty=Watchlist().inspect(History([record('1','General review completed.')]),'1','LAB')
    assert not empty['signals'] and empty['scope_record_count']==1


def test_cache_is_authority_bound_and_access_is_rechecked_before_serving_counts():
    service=history();watch=Watchlist()
    first=watch.inspect(service,'1','LAB')
    service.allowed_sites=set()
    with pytest.raises(SiteAccessError):watch.inspect(service,'1','LAB')
    service.allowed_sites={'LAB'};service.manifest['storage_directory']='new-generation'
    service.records['2']['indexed_field_texts']['DETAILED_DESCRIPTION']='Unrelated event.'
    second=watch.inspect(service,'1','LAB')
    assert first['signals']!=second['signals']


def test_http_scope_and_date_validation_happen_without_ai_or_embedding(monkeypatch):
    service=history();monkeypatch.setattr(main,'engine',lambda:service);main.watchlist_service.cache_clear()
    client=TestClient(main.app)
    data=client.get('/api/v1/match/matched-records/1/watchlist?site=LAB&as_of=2026-11-21')
    assert data.status_code==200 and data.json()['method']=='deterministic'
    assert client.get('/api/v1/match/matched-records/5/watchlist?site=DENIED').status_code==403
    assert client.get('/api/v1/match/matched-records/5/watchlist?site=LAB').status_code==404
    assert client.get('/api/v1/match/matched-records/1/watchlist?site=LAB&as_of=2026-02-30').status_code==422


def test_original_snapshot_projection_keeps_only_eligible_ids_and_exact_source_identifiers(tmp_path):
    from capa_match.infrastructure.data.importer import import_snapshot
    from capa_match.infrastructure.data.repository import SqliteRecordRepository
    from capa_match.identifiers import SnapshotMatchService,SourceIdentifiers
    source=tmp_path/'source.csv'
    with source.open('w',newline='',encoding='utf-8') as handle:
        writer=csv.DictWriter(handle,fieldnames=['PR_ID','SITE','RECORD_TYPE','DETAILED_DESCRIPTION'])
        writer.writeheader()
        for key in ['001','002','003']:
            writer.writerow({'PR_ID':key,'SITE':'LAB','RECORD_TYPE':'Deviation','DETAILED_DESCRIPTION':'Original UHPLC327 system suitability failed.'})
    repo=SqliteRecordRepository(import_snapshot(source,tmp_path/'sources'));mapper=SourceIdentifiers(repo)
    core=History([record('1','CLEANED TEXT'),record('2','CLEANED TEXT')]);core.manifest['source_snapshot_id']=mapper.snapshot_id
    adapter=SnapshotMatchService(core,mapper)
    result=Watchlist().inspect(adapter,'001','LAB')
    assert result['scope_record_count']==2
    asset=next(row for row in result['signals'] if row['kind']=='equipment')
    assert asset['evidence'][0]['pr_id']=='002' and 'Original UHPLC327' in asset['evidence'][0]['quote']
    with pytest.raises(KeyError):Watchlist().inspect(adapter,'1','LAB')
