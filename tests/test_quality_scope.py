from datetime import date
import pytest
from capa_match.scope import resolve_scope, requested_years, requested_dates, filter_only
from match_v1.contracts import SearchRequest
from match_v1.core import clean_for_index
from test_explorer import explorer

def scope(query, **values):
    return resolve_scope(explorer(),SearchRequest(query=query,all_sites=True,assist=True,**values),query,date(2026,11,20))

@pytest.mark.parametrize('ending',['','.','!','?',';'])
@pytest.mark.parametrize('years',['2025','2025 and 2026','2024, 2025 and 2026'])
def test_year_punctuation_does_not_change_requested_scope(ending,years):
    import re
    assert requested_years('Show all critical deviations in '+years+ending,2026)==[int(value) for value in re.findall(r'20\d{2}',years)]

@pytest.mark.parametrize('negative',['no major or critical','not major or critical','excluding major and critical','without major and critical','except major and critical'])
def test_coordinated_negative_criticality(negative):
    assert scope('Only minor HPLC deviations in Toronto in 2025, '+negative+'.')['filters']['criticalities']==['Minor']

def test_site_exclusion_and_multi_site_union_are_hard_filters():
    value=scope('Do not include Toronto cases; show other sites in 2025.')
    assert value['filters']['exclude_sites']==['TRO (Toronto)']
    service=explorer()
    service.allowed_sites.add('GRC (Greenville, NC)')
    query='Show HPLC deviations from Toronto and Greenville only.'
    value=resolve_scope(service,SearchRequest(query=query,all_sites=True),query,date(2026,11,20))
    assert value['site'] is None and len(value['filters']['sites'])==2
    result=service.search('',all_sites=True,filters=value['filters'])
    assert all(row['site'] in value['filters']['sites'] for row in result['results'])

def test_open_request_returns_no_closed_records_and_explains_corpus_limit():
    value=scope('Show me currently open critical deviations.')
    assert value['filters']['status']=='open' and value['notices']
    assert explorer().search('',all_sites=True,filters=value['filters'])['results']==[]

@pytest.mark.parametrize('text,start,end',[
    ('last month','2026-10-01','2026-10-31'),('this month','2026-11-01','2026-11-20'),
    ('past 7 days','2026-11-14','2026-11-20'),('yesterday','2026-11-19','2026-11-19'),
    ('last quarter','2026-07-01','2026-09-30'),('October 2025','2025-10-01','2025-10-31'),
    ('from 2025-01-01 to 2025-12-31','2025-01-01','2025-12-31')])
def test_calendar_date_scope(text,start,end):
    assert requested_dates(text,date(2026,11,20))==(date.fromisoformat(start),date.fromisoformat(end))

def test_month_boundary_and_leap_year():
    assert requested_dates('last month',date(2024,3,1))==(date(2024,2,1),date(2024,2,29))
    assert requested_dates('last month',date(2026,1,1))==(date(2025,12,1),date(2025,12,31))

def test_selected_scope_cannot_be_relaxed_by_search_prose():
    value=scope('Critical deviations in 2026',filters={'years':[2021],'criticalities':['Minor']})
    assert value['filters']['years']==[2021] and value['filters']['criticalities']==['Minor']

@pytest.mark.parametrize('text',['Spec <0.10%)\n--> conforms','Humidity <65% and temperature <18 or >30 degrees.',
 'Result <0.5000 EU/mL; Root Cause 1 - Procedure -> action required.',
 '<2-JUN-2021: Received notification with important investigation text>'])
def test_scientific_comparisons_and_angle_wrapped_text_survive(text):
    assert clean_for_index(text)==text

def test_actual_html_and_entities_are_normalized_without_removing_scientific_values():
    value=clean_for_index('<p>Limit &lt;0.10%</p><br/>Result &gt;0.20%')
    assert '<0.10%' in value and '>0.20%' in value and '<p>' not in value and '\n' in value

@pytest.mark.parametrize('text',['Show all Critical deviations in 2025.','Show deviations opened last month in Toronto.','Show me currently open critical deviations.'])
def test_metadata_only_requests_browse(text):
    assert filter_only(text,explorer())

def test_capa_closure_is_not_deviation_status_constraint():
    assert scope('Have there been deviations involving MFG859 after its CAPA was closed?')['filters']['status']=='any'

def test_current_quarter_uses_calendar_boundaries():
    assert requested_dates('this quarter',date(2026,11,20))==(date(2026,10,1),date(2026,11,20))

def test_unknown_member_of_explicit_site_union_does_not_silently_disappear():
    with pytest.raises(ValueError,match='site in the requested list'):
        scope('HPLC deviations from Toronto and Atlantis only.')

def test_excluding_every_severity_never_turns_into_an_unfiltered_search():
    value=scope('Show deviations, no minor or major or critical.')
    assert set(value['filters']['exclude_criticalities'])=={'Minor','Major','Critical'}
    assert explorer().search('',all_sites=True,filters=value['filters'])['results']==[]

def test_negated_year_and_status_are_not_reintroduced_by_positive_scope():
    value=scope('Show deviations in 2024 and 2025, not 2024; not closed deviations.')
    assert value['filters']['years']==[2025] and value['filters']['exclude_years']==[2024]
    assert value['filters']['status']=='open'

def test_year_exclusion_works_without_a_positive_year_clause():
    value=scope('Show HPLC deviations, exclude 2024.')
    assert value['filters']['years']==[] and value['filters']['exclude_years']==[2024]

def test_conflicting_selected_scope_is_explained():
    value=scope('Critical deviations in 2026',filters={'years':[2021],'criticalities':['Minor']})
    assert len(value['notices'])==2
