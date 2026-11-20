"""Read original narrative evidence for indexed, authorised source identities."""
from __future__ import annotations
import json

NARRATIVES = {'DETAILED_DESCRIPTION':'detailed_description', 'INVESTIGATION':'investigation',
              'CONCLUSION':'conclusion', 'CORRECTIONS_AND_CAPA_PLAN':'corrections_and_capa_plan'}


def original_fields(record):
    return {**(record.get('original_field_texts') or record.get('indexed_field_texts') or
               {'DETAILED_DESCRIPTION':record.get('text','')}), **record.get('literal_field_texts',{})}


def enriched(service, record):
    """The caller must first authorise the exact source ID through service.record."""
    mapper=getattr(service,'identifiers',None)
    if mapper is None:
        return record
    if mapper.snapshot_id!=service.manifest.get('source_snapshot_id'):
        raise ValueError('The source does not match the indexed snapshot.')
    source=mapper.repository.get_record(record['pr_id'])
    if source is None or source.site!=record['site'] or source.record_type.strip().casefold()!='deviation':
        raise ValueError('The indexed deviation does not resolve to its retained source.')
    fields={name:str(source.fields.get(column) or '') for name,column in NARRATIVES.items()}
    return {**record, 'original_field_texts':{**record.get('original_field_texts',{}),**fields}}


def site_records(service, site):
    """Stream the full eligible site history; never expand beyond indexed IDs."""
    core=getattr(service,'service',service)
    core._authorized_site(site)
    selected={str(record['pr_id']):record for record in core.records.values() if record['site']==site}
    mapper=getattr(service,'identifiers',None)
    if mapper is None:
        yield from (dict(record) for record in selected.values())
        return
    if mapper.snapshot_id!=service.manifest.get('source_snapshot_id'):
        raise ValueError('The source does not match the indexed snapshot.')
    external={mapper.identity(key,site=site,record_type='deviation').pr_id:key for key in selected}
    keys=list(external)
    columns=','.join("'$."+name+"'" for name in NARRATIVES.values())
    found=set()
    with mapper.repository._connect() as database:
        for offset in range(0,len(keys),200):
            batch=keys[offset:offset+200]
            query="SELECT pr_id,json_extract(fields,"+columns+") FROM records WHERE pr_id IN ("+','.join('?' for _ in batch)+')'
            for row in database.execute(query,batch):
                identifier,encoded=tuple(row)
                record=selected[external[identifier]]
                found.add(identifier)
                yield {**record,'pr_id':identifier,'original_field_texts':dict(zip(NARRATIVES,(str(value or '') for value in json.loads(encoded))))}
    if found!=set(keys):
        raise ValueError('Some indexed records are missing from the retained snapshot.')
