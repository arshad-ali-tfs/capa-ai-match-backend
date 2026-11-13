from pathlib import Path

from match_v1.ingest import load_gtw

DEMO = Path(__file__).parent / 'fixtures' / 'demo_gtw.csv'


def test_demo_ingest_and_capa_linkage():
    records, capas, stats = load_gtw(DEMO, division='DPD', limit=0)
    assert len(records) == 8
    assert stats['linked_capas'] == 4
    assert {c['pr_id'] for c in capas['101']} == {'901'}
    assert {c['pr_id'] for c in capas['104']} == {'902'}
    assert {r['pr_id'] for r in records}.isdisjoint({'999'})


def test_site_filtering_and_limit():
    records, capas, _ = load_gtw(DEMO, division='DPD', site='GRC (Greenville, NC)', limit=1)
    assert len(records) == 1 and records[0]['site'] == 'GRC (Greenville, NC)'
    assert records[0]['pr_id'] == '105'
    assert len(capas['105']) == 1


def test_prepared_text_field_consistency():
    records, _, stats = load_gtw(DEMO, division='DPD', text_fields=('RECORD_NAME', 'DETAILED_DESCRIPTION'), limit=1)
    assert 'HPLC system suitability failure' in records[0]['text']
    assert stats['indexed_fields'] == ['RECORD_NAME', 'DETAILED_DESCRIPTION']
    assert set(records[0]['indexed_field_texts']) == {'RECORD_NAME', 'DETAILED_DESCRIPTION'}
