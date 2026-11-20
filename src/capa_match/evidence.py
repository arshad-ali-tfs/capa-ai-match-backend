"""Bounded original-source passages and explicit evidence coverage."""
from __future__ import annotations
import re

INTENT_WORDS = {
    'actions': r'corrective|preventive|action|capa|retrain|reassess|revis|updat|implement|prohibit|follow.up|monitor|effective',
    'chronology': r'after|before|follow|repeat|again|recurr|mainten|repair|replac|capa|closed|completed|training',
    'evidence': r'stability|kinetic|assessment|impact|evidence|sample|test|result|specification|conclusion',
    'precedents': r'root cause|contribut|conclusion|investigation|probable|suspect|confirmed|not determined',
}


def original_fields(record):
    return {**(record.get('original_field_texts') or record.get('indexed_field_texts') or
               {'DETAILED_DESCRIPTION': record.get('text','')}), **record.get('literal_field_texts',{})}


def passages(record, query='', intent='precedents', budget=2400):
    words = {word for word in re.findall(r'[a-z]{4,}',query.lower()) if word not in {'deviation','deviations','historical','cases','records','there','have','been','what','were','similar','show','find'}}
    candidates = []
    for field, text in original_fields(record).items():
        if not isinstance(text,str) or not text.strip():
            continue
        # Windows are contiguous original text; offsets address Unicode code
        # points in the immutable field, not a model paraphrase or cleaned copy.
        for start in range(0,len(text),650):
            end = min(start+800,len(text))
            chunk = text[start:end]
            score = sum(word in chunk.lower() for word in words)
            score += 4*len(re.findall(INTENT_WORDS.get(intent,INTENT_WORDS['precedents']),chunk,re.I))
            score += 2 if field=='CONCLUSION' else 0
            score += 1 if start==0 else 0
            candidates.append((score,field,start,end,chunk))
    selected=[]
    used=0
    for _,field,start,end,chunk in sorted(candidates,key=lambda item:(-item[0],item[1],item[2])):
        if used+min(len(chunk),budget)>budget or any(p['field']==field and start<p['end'] and end>p['start'] for p in selected):
            continue
        selected.append({'field':field,'start':start,'end':end,'text':chunk})
        used+=len(chunk)
        if budget-used<200:
            break
    if not selected and candidates:
        _,field,start,end,chunk=candidates[0]
        selected=[{'field':field,'start':start,'end':start+min(len(chunk),budget),'text':chunk[:budget]}]
    return selected


def locate_quote(record, quote, field=None):
    if not isinstance(quote,str) or len(quote.strip())<12:
        return None
    for name,text in {'RECORD_NAME':record.get('record_name',''),**original_fields(record)}.items():
        if field and name!=field:
            continue
        start=text.find(quote)
        if start>=0:
            return {'field':name,'start':start,'end':start+len(quote),'quote':quote}
        # Permit formatting-only whitespace differences, but return the exact
        # original span. Never substitute a title or a paraphrase as evidence.
        pattern=r'\s+'.join(re.escape(part) for part in quote.strip().split())
        match=re.search(pattern,text) if pattern else None
        if match:
            return {'field':name,'start':match.start(),'end':match.end(),'quote':match[0]}
    return None


def coverage(record):
    fields=original_fields(record)
    attachment=any(re.search(r'\b(?:see|refer|attached|attachment)\b.{0,90}\b(?:attach|investigation|report|capa)',text,re.I|re.S) for text in fields.values())
    narrative_ids=sorted(set(re.findall(r'\bCAPA\s*(?:#|QR|PR|No\.?|number)?\s*[:#-]?\s*(\d{3,10})\b','\n'.join(fields.values()),re.I)))
    linked={str(item.get('pr_id')) for item in record.get('related_capas',[])}
    return {'original_available':bool(record.get('original_field_texts')),
            'available_fields':list(fields),'attachments_included':False,'attachment_references':attachment,
            'narrative_capa_ids':narrative_ids[:20], 'unresolved_capa_mentions':[key for key in narrative_ids if key not in linked][:20],
            'effectiveness':'Not established by retained link status; inspect documented follow-up evidence.'}


def mentions(record):
    result=[]
    seen=set()
    for field,text in original_fields(record).items():
        patterns=[('Equipment',r'\b(?:MFG|EQP?|HPLC|UPLC|QC|INS|PT)[-_ ]?\d{2,8}(?:[-_][A-Za-z0-9]+)*\b'),
                  ('Location',r'\b(?:room|suite|line|building)\s+[A-Za-z]?[-_]?\d+[A-Za-z0-9-]*\b'),
                  ('Batch',r'\b(?:batch|lot)\s*(?:#|no\.?|number|:)?\s*([A-Z0-9][A-Z0-9-]{3,20})\b')]
        for kind,pattern in patterns:
            for match in re.finditer(pattern,text,re.I):
                value=match[0]
                if kind=='Batch' and not re.search(r'\d',value):
                    continue
                key=(kind,value.casefold())
                if key in seen:
                    continue
                seen.add(key)
                result.append({'kind':kind,'value':value,'field':field,'start':match.start(),'end':match.end(),
                               'role':'Source mention; event involvement requires review'})
                if len(result)>=30:
                    return result
    return result
