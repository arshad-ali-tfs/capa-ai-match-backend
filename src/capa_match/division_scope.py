"""Resolve explicitly named divisions without allowing rewrites to expand scope."""
import re


def requested_divisions(service, query, selected):
    if selected:
        return selected
    allowed=sorted({record.get('division','').strip() for record in service.records.values() if record['site'] in service.allowed_sites}-{''})
    names='(?:'+'|'.join(re.escape(value) for value in allowed)+')'
    negative=re.findall(r'\b(?:not|except|excluding|exclude|without)\s+((?:'+names+r'(?![\w-])\s*(?:(?:,|and|or)\s*)?)+)',query,re.I)
    mentioned=[];excluded=[]
    for value in allowed:
        pattern=r'(?<![\w-])'+re.escape(value)+r'(?![\w-])'
        if value.casefold()=='global' and not re.search(r'\bdivision\b',query,re.I):
            continue  # "global calibration issues" usually means all locations.
        if re.search(pattern,query,re.I):
            if any(re.search(pattern,part,re.I) for part in negative):excluded.append(value)
            else:mentioned.append(value)
    return mentioned or [value for value in allowed if value not in excluded] if excluded else mentioned


def strip_divisions(query):
    return re.sub(r'(?<![\w-])(?:DPD|DSD|CTD|Global)(?![\w-])|\bdivisions?\b',' ',query,flags=re.I)
