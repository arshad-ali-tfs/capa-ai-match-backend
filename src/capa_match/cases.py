"""Durable, source-versioned investigation bundles from server-owned searches."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from uuid import uuid4
from filelock import FileLock
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from match_v1.search import SiteAccessError
from .provenance import index_generation


class Decision(BaseModel):
    model_config=ConfigDict(extra='forbid')
    pr_id:str=Field(min_length=1,max_length=200)
    disposition:Literal['include','exclude']
    rationale:str=Field(min_length=3,max_length=2000)


class CaseRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    search_id:str=Field(pattern=r'^[a-f0-9]{32}$')
    title:str=Field(min_length=3,max_length=200)
    reviewer:str=Field(min_length=1,max_length=200)
    notes:str=Field(default='',max_length=10000)
    decisions:list[Decision]=Field(default_factory=list,max_length=6)


def directory():
    root=Path(os.environ.get('CAPA_DATA_DIR','data'))/'match-cases'
    root.mkdir(parents=True,exist_ok=True)
    return root.resolve()


def authorized(case,service):
    if not set(case['authorized_sites']).issubset(service.allowed_sites):
        raise SiteAccessError('This saved investigation includes a site outside the current allowed scope.')


def path(case_id):
    if not re.fullmatch('[a-f0-9]{32}',case_id):
        raise ValueError('Invalid investigation identifier.')
    return directory()/(case_id+'.json')


def save(service,assistant,request):
    if len(request.title.strip())<3 or not request.reviewer.strip():
        raise ValueError('Enter an investigation title and reviewer name.')
    entry=assistant.searches.get(request.search_id)
    if entry is None or entry['authority']!=assistant.authority(service):
        raise KeyError('The search expired or its source changed. Search again before saving.')
    with entry['lock']:
        result=deepcopy(entry['result'])
        rows={row['pr_id']:row for row in result['results']}
        decisions=[item.model_dump() for item in request.decisions]
        if len({item['pr_id'] for item in decisions})!=len(decisions) or any(item['pr_id'] not in rows for item in decisions):
            raise ValueError('Decisions must identify distinct candidates from this search.')
        included=[item for item in decisions if item['disposition']=='include']
        if len(included)>6:
            raise ValueError('An evidence bundle can retain up to six included source records.')
        from .retained import enriched
        sources=[enriched(service,service.record(item['pr_id'],rows[item['pr_id']]['site'])) for item in decisions]
        summary=deepcopy(entry.get('summary'))
        if summary:
            summary['search_id']=request.search_id
        result.update(search_id=request.search_id,summary_id=request.search_id if summary else None)
        stamp=datetime.now(timezone.utc).isoformat()
        case={'id':uuid4().hex,'schema_version':'investigation-v1','title':request.title.strip(),'reviewer':request.reviewer.strip(),
              'notes':request.notes.strip(),'created_at':stamp,'revision':1,'decisions':decisions,'result':result,
              'sources':sources,'overview':summary,'criteria':result['submitted_criteria'],
              'authorized_sites':sorted({row['site'] for row in rows.values()}|({result['seed']['site']} if result.get('seed') else set())),
              'provenance':{'source_snapshot_id':service.manifest.get('source_snapshot_id'),
                            'index_generation':index_generation(service.manifest),
                            'model_name':service.manifest.get('model_name'),'assistant_version':result.get('assistant_version','match-assist-v2'),
                            'text_normalization_version':service.manifest.get('source_statistics',{}).get('text_normalization_version'),
                            'assistant_recipe':deepcopy(result.get('assistant_recipe'))},
              'scope':'Saved investigator evidence and rationale; no source/QMS record is modified.'}
        encoded=json.dumps(case,ensure_ascii=False,indent=2).encode()
        if len(encoded)>8_000_000:
            raise ValueError('This evidence bundle is too large. Include fewer source records.')
    target=path(case['id'])
    with FileLock(str(directory()/'.cases.lock'),timeout=10):
        pending=target.with_suffix('.tmp')
        pending.write_bytes(encoded)
        pending.replace(target)
    return case


def get(case_id,service):
    target=path(case_id)
    with FileLock(str(directory()/'.cases.lock'),timeout=10):
        case=json.loads(target.read_text(encoding='utf-8'))
    authorized(case,service)
    return {**case,'source_changed':case['provenance']['source_snapshot_id']!=service.manifest.get('source_snapshot_id'),
            'index_changed':case['provenance']['index_generation']!=index_generation(service.manifest)}


def list_cases(service):
    items=[]
    with FileLock(str(directory()/'.cases.lock'),timeout=10):
        for target in directory().glob('*.json'):
            case=json.loads(target.read_text(encoding='utf-8'))
            try:
                authorized(case,service)
            except SiteAccessError:
                continue
            items.append({key:case[key] for key in ['id','title','reviewer','created_at','revision']})
    return sorted(items,key=lambda case:case['created_at'],reverse=True)
