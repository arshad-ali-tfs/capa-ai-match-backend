"""Deterministic historical signals over the authorised indexed site corpus."""
from collections import Counter, OrderedDict
from copy import deepcopy
from datetime import date, datetime, timezone
import re
from threading import Lock
from .retained import enriched, original_fields, site_records
from match_v1.contracts import SearchFilters
from match_v1.scope import accepts_status, status_counts

VERSION='match-watchlist-division-status-v2'
ASSET=re.compile(r'(?<![A-Za-z0-9])(?:UHPLC|HPLC|UPLC|LC|GC|EQP?|INS|INST|ASSET|BAL|FRZ|FZR|MFG|MIX|PT|TT|AUT|OVEN|SCALE|INC)[-_ ]{0,2}(?:[A-Z]{1,4}[-_])?\d{2,8}(?:[-_][A-Z0-9]{1,8})?(?![A-Za-z0-9])',re.I)
NAMED_ASSET=re.compile(r'\b(?:equipment|asset|instrument)(?:\s+(?:id|number|no\.?))?\s*[:#]\s*([A-Z]{1,8}[-_]\d{2,8}(?:[-_][A-Z0-9]{1,8})?)(?![A-Za-z0-9])',re.I)
LOCATION=re.compile(r'\b(?:room|suite|line|building)\s*(?:#|no\.?)?\s*[A-Z]?[-_]?\d{1,5}[A-Z]?(?![A-Za-z0-9])',re.I)
DOCUMENT_CONTEXT=re.compile(r'\b(?:sop|method|procedure|batch|lot|report|rsid|ssid)\s*(?:id|no\.?|number)?\s*[:#-]?\s*$',re.I)
FAILURE=re.compile(r'\bfail(?:ed|ure|ures|ing|s)?\b|\bmalfunction\w*|\b(?:not|did not|does not) (?:meet|pass|comply)|\bout[ -]of[ -](?:specification|tolerance)|\bexcursion\w*|\b(?:above|below|exceed\w*)\b.{0,35}\b(?:limit|specification|range)',re.I)
NEGATED=re.compile(r'\b(?:no|not|without|never|neither)\b(?:[\s\w-]{0,35})$|\b(?:ruled out|did not|didn.t|does not)\s*$',re.I)
REFERENCE=re.compile(r'\b(?:related (?:documents|procedures)|reference (?:list|documents)|previous deviation|historical review|trend report|prior (?:event|deviation))\b',re.I)
RULES=[
 ('system_suitability','System suitability failure',r'\bsystem suitability\b|\b(?:sst|ssf|bracket standard|check standard)\b',r'\bfail\w*|not (?:meet|met)|out[ -]of[ -](?:specification|tolerance)|exceed'),
 ('calibration','Calibration / verification failure',r'\bcalibrat\w*|\bverification\b',r'\bfail\w*|out[ -]of[ -]tolerance|drift|not (?:meet|met|within)'),
 ('temperature','Temperature excursion',r'\btemperatur\w*|\bcold chain\b|\bfreezer\b',r'excursion|out[ -]of[ -](?:range|specification)|exceed|above.{0,30}limit|below.{0,30}limit'),
 ('contamination','Contamination / microbial finding',r'\bcontaminat\w*|\bbioburden\b|\bmicrobial\b|\bendotoxin\b',r'detect|observ|exceed|fail|growth|positive'),
 ('packaging','Packaging / labelling discrepancy',r'\bpackag\w*|\blabell?\w*|\breconcil\w*',r'incorrect|wrong|missing|mismatch|discrepanc|incomplete|fail|damage'),
 ('documentation','Documentation / recording discrepancy',r'\bdocument\w*|\brecord\w*|\blogbook\b|\btranscri\w*',r'missing|incorrect|incomplete|error|not record|not document|omitt'),
 ('oos','Out-of-specification result',r'\bout[ -]of[ -]specification\b|\boos\b',r'result|observ|obtain|report|fail'),
 ('breakdown','Equipment malfunction',r'\bequipment\b|\binstrument\b|\bmalfunction\w*|\bbreakdown\b',r'malfunction|breakdown|stopp|fail|not function'),
]


def day(value):
    try:return date.fromisoformat(str(value)[:10])
    except ValueError:return None


def snippet(field,text,start,end):
    left=max(0,start-100);right=min(len(text),max(end+140,start+220))
    return {'field':field,'start':left,'end':right,'quote':text[left:right]}


def affirmative(text,start,end):
    before=text[max(0,start-45):start]
    after=text[end:end+50]
    if NEGATED.search(before) or re.match(r'\s+(?:was |were |is |are )?(?:not (?:observed|detected|confirmed)|ruled out)',after,re.I):return False
    return True


def events(record):
    fields={'RECORD_NAME':record.get('record_name',''),'DETAILED_DESCRIPTION':original_fields(record).get('DETAILED_DESCRIPTION','')}
    found={}
    for key,label,subject,outcome in RULES:
        if key=='breakdown' and found:continue
        for field,text in fields.items():
            for subject_match in re.finditer(subject,text,re.I):
                start=max(0,subject_match.start()-100);end=min(len(text),subject_match.end()+160)
                window=text[start:end]
                failures=[match for match in re.finditer(outcome,window,re.I) if affirmative(text,start+match.start(),start+match.end())]
                if failures and affirmative(text,subject_match.start(),subject_match.end()) and not REFERENCE.search(window):
                    found[key]={'label':label,'proof':snippet(field,text,subject_match.start(),subject_match.end())}
                    break
            if key in found:break
    return found


def entities(record):
    found={}
    for field,text in {'RECORD_NAME':record.get('record_name',''),**original_fields(record)}.items():
        if not isinstance(text,str):continue
        matches=[('equipment',match,0) for match in ASSET.finditer(text)]
        matches += [('equipment',match,1) for match in NAMED_ASSET.finditer(text)]
        matches += [('location',match,0) for match in LOCATION.finditer(text)]
        for kind,match,group in matches:
            start,end=match.span(group);value=match[group]
            family_prefix=re.match(r'^(?:UHPLC|HPLC|UPLC)\s+((?:LC|EQP?|INS|ASSET)[-_]\d{2,8}(?:[-_][A-Z0-9]{1,8})?)$',value,re.I)
            if kind=='equipment' and family_prefix:
                start+=family_prefix.start(1);value=family_prefix[1]
            if DOCUMENT_CONTEXT.search(text[max(0,start-30):start]):continue
            key=kind+':'+re.sub(r'[^a-z0-9]','',value.casefold())
            if key in found:continue
            window=text[max(0,start-140):min(len(text),end+160)]
            failure=any(affirmative(window,m.start(),m.end()) for m in FAILURE.finditer(window))
            found[key]={'kind':kind,'label':value,'proof':snippet(field,text,start,end),
                        'failure_nearby':field in {'RECORD_NAME','DETAILED_DESCRIPTION'} and failure and not REFERENCE.search(window)}
    return found


def project(record):
    return {key:record.get(key,'') for key in ['pr_id','site','division','record_name','date_opened','criticality','status']} | {'entities':entities(record),'events':events(record)}


def evidence(record,proof):
    return {key:record[key] for key in ['pr_id','site','division','record_name','date_opened','criticality','status']} | proof


class Watchlist:
    def __init__(self):
        self.sites=OrderedDict();self.lock=Lock()

    def inspect(self,service,pr_id,site,as_of=None,filters=None):
        selected=service.record(pr_id,site)  # Exact identity and site access precede cache/scans.
        selected=enriched(service,selected)
        as_of=as_of or datetime.now(timezone.utc).date()
        core=getattr(service,'service',service)
        authority=(id(core),service.manifest.get('source_snapshot_id'),service.manifest.get('storage_directory'),len(core.records),tuple(sorted(service.allowed_sites)),site)
        with self.lock:
            if authority not in self.sites:
                self.sites[authority]=(core,tuple(project(record) for record in site_records(service,site)))
                while len(self.sites)>3:self.sites.popitem(last=False)
            self.sites.move_to_end(authority);records=self.sites[authority][1]
        criteria=SearchFilters.model_validate(filters or {'record_scope':'all'})
        divisions={value.strip().casefold() for value in criteria.divisions}
        statuses={value.strip().casefold() for value in criteria.statuses}
        def accepts(record):
            return (not divisions or record['division'].casefold() in divisions) and (not statuses or record['status'].casefold() in statuses) and accepts_status(record['status'],criteria.record_scope,criteria.include_cancelled)
        available=records
        records=[record for record in records if accepts(record)]
        selected_in_scope=any(record['pr_id']==pr_id for record in records)
        seed=project(selected)
        others=[record for record in records if record['pr_id']!=pr_id]
        try:start=as_of.replace(year=as_of.year-1)
        except ValueError:start=as_of.replace(year=as_of.year-1,day=28)
        dates=[opened for record in records if (opened:=day(record['date_opened'])) and opened<=as_of]
        def newest(record):return (day(record['date_opened']) or date.min,record['pr_id'])
        signals=[]
        def signal(key,kind,label,matches,proof_fn,nearby_count=0):
            matches=sorted(matches,key=newest,reverse=True)
            years=Counter(str(opened.year) for record in matches if (opened:=day(record['date_opened'])) and opened<=as_of)
            recent=sum(1 for record in matches if (opened:=day(record['date_opened'])) and start<=opened<=as_of)
            opened=[opened for record in matches if (opened:=day(record['date_opened'])) and opened<=as_of]
            signals.append({'key':key,'kind':kind,'label':label,'other_record_count':len(matches),
                'failure_indicator_count':nearby_count,'last_12_months':recent,'last_opened':max(opened).isoformat() if opened else None,
                'status_counts':status_counts(matches),
                'year_counts':[{'year':int(year),'count':count} for year,count in sorted(years.items())],
                'evidence':[evidence(record,proof_fn(record)) for record in matches[:5]],
                'seed_evidence':proof_fn(seed),'search_query':label if kind=='equipment' else None})
        for key,entity in seed['entities'].items():
            matches=[record for record in others if key in record['entities']]
            nearby=sum(1 for record in matches if record['entities'][key]['failure_nearby'] and bool(set(record['events'])&set(seed['events'])))
            signal(key,entity['kind'],entity['label'],matches,lambda record,k=key:record['entities'][k]['proof'],nearby)
        for key,event in seed['events'].items():
            matches=[record for record in others if key in record['events']]
            signal('pattern:'+key,'pattern',event['label'],matches,lambda record,k=key:record['events'][k]['proof'])
        signals.sort(key=lambda row:(row['kind']=='location',-row['other_record_count'],row['kind']!='equipment',row['label'].casefold()))
        return {'version':VERSION,'pr_id':pr_id,'site':site,'source_snapshot_id':service.manifest.get('source_snapshot_id'),
            'method':'deterministic','as_of':as_of.isoformat(),'recent_from':start.isoformat(),'scope':'same_site_indexed_history',
            'scope_record_count':len(records),'other_record_count':len(others),
            'selected_in_scope':selected_in_scope,'status_counts':status_counts(records),'available_status_counts':status_counts(available),
            'filters':{key:criteria.model_dump(mode='json')[key] for key in ['divisions','statuses','record_scope','include_cancelled']},
            'opened_from':min(dates).isoformat() if dates else None,'opened_to':max(dates).isoformat() if dates else None,
            'undated_count':sum(day(record['date_opened']) is None for record in records),
            'future_dated_count':sum(bool((opened:=day(record['date_opened'])) and opened>as_of) for record in records),
            'available_signals':len(signals),'signals':signals[:8],
            'interpretation':'Counts are distinct other indexed deviations at this site, across all dates, within the submitted division and workflow status scope. Other search filters do not constrain this history. Equipment/location counts are literal mentions; event indicators are rule-based title/description matches. Ongoing records are provisional and cancelled records are not completed investigation precedents. Counts do not establish repeated root causes, equipment causation, a recurrence rate or CAPA effectiveness.'}
