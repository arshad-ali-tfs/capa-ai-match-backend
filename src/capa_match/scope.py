"""Deterministic, auditable scope resolution; a rewrite cannot relax it."""
from __future__ import annotations

import calendar
from datetime import date, timedelta
import re

YEAR = r'(?<![A-Za-z0-9_./-])(?:19|20|21)\d{2}(?![A-Za-z0-9_]|[./-][A-Za-z0-9])'
NEGATIVE = re.compile(r'\b(?:no|not(?!\s+only)|exclude|excluding|except|without|non)[ -]+(.+?)(?=[,;!?]|\.(?:\s|$)|\bbut\b|$)', re.I)


def requested_years(query, current_year):
    year = r'((?:19|20|21)\d{2})'
    interval = re.search(r'\b(?:from\s+)?' + year + r'\s*(?:to|through|until|[–-])\s*' + year + r'\b|\bbetween\s+' + year + r'\s+and\s+' + year, query, re.I)
    if interval:
        values = [int(value) for value in interval.groups() if value]
        if values[0] <= values[1] and values[1] - values[0] < 100:
            return list(range(values[0], values[1] + 1))
    if re.search(r'\b(?:this|current)\s+year\b', query, re.I):
        return [current_year]
    if re.search(r'\b(?:last|previous)\s+year\b', query, re.I):
        return [current_year - 1]
    recent = re.search(r'\b(?:last|past)\s+(\d{1,2})\s+years\b', query, re.I)
    if recent and 1 <= int(recent[1]) <= 50:
        return list(range(current_year - int(recent[1]) + 1, current_year + 1))
    explicit = re.findall(YEAR, query)
    if explicit and (re.search(r'\b(?:in|during|year|years|opened|raised|since|from|for|between)\b', query, re.I) or re.match(r'^\s*\d{4}\b', query)):
        values = sorted({int(value) for value in explicit})
        return list(range(values[0], current_year + 1))[-100:] if re.search(r'\bsince\b', query, re.I) and values[0] <= current_year else values
    return None


def month_start(today, offset=0):
    value = today.year * 12 + today.month - 1 + offset
    return date(value // 12, value % 12 + 1, 1)


def requested_dates(query, today):
    if re.search(r'\b(?:last|previous)\s+month\b', query, re.I):
        return month_start(today, -1), month_start(today) - timedelta(days=1)
    if re.search(r'\b(?:this|current)\s+month\b', query, re.I):
        return month_start(today), today
    if re.search(r'\b(?:last|previous)\s+week\b', query, re.I):
        first=today-timedelta(days=today.weekday())
        return first-timedelta(days=7), first-timedelta(days=1)
    if re.search(r'\b(?:this|current)\s+week\b', query, re.I):
        return today-timedelta(days=today.weekday()), today
    if re.search(r'\byesterday\b', query, re.I):
        return today - timedelta(days=1), today - timedelta(days=1)
    if re.search(r'\btoday\b', query, re.I):
        return today, today
    recent = re.search(r'\b(?:last|past)\s+(\d{1,3})\s+(days?|weeks?|months?)\b', query, re.I)
    if recent:
        count = int(recent[1])
        if count:
            start = month_start(today, -count) if recent[2].lower().startswith('month') else today - timedelta(days=count * (7 if recent[2].lower().startswith('week') else 1) - 1)
            return start, today
    if re.search(r'\b(?:last|previous)\s+quarter\b', query, re.I):
        first = date(today.year, 3*((today.month-1)//3)+1, 1)
        return month_start(first, -3), first-timedelta(days=1)
    if re.search(r'\b(?:this|current)\s+quarter\b', query, re.I):
        return date(today.year, 3*((today.month-1)//3)+1, 1), today
    values = re.findall(r'\b\d{4}-\d{2}-\d{2}\b', query)
    if values:
        try:
            dates = [date.fromisoformat(value) for value in values]
        except ValueError:
            raise ValueError('The search contains an invalid date. Use YYYY-MM-DD.') from None
        if len(dates) == 2 and re.search(r'\b(?:from|between)\b', query, re.I):
            if dates[0] > dates[1]:
                raise ValueError('The requested opened-date range is reversed.')
            return dates[0], dates[1]
        if len(dates) == 1:
            if re.search(r'\bafter\b', query, re.I):
                return dates[0]+timedelta(days=1), None
            if re.search(r'\b(?:since|from)\b', query, re.I):
                return dates[0], None
            if re.search(r'\bbefore\b', query, re.I):
                return None, dates[0]-timedelta(days=1)
            return dates[0], dates[0]
    for number, name in enumerate(calendar.month_name[1:], 1):
        match = re.search(r'\b' + name + r'\s+(20\d{2})\b', query, re.I)
        if match:
            year = int(match[1])
            return date(year, number, 1), date(year, number, calendar.monthrange(year,number)[1])
    return None


def site_aliases(name):
    aliases = [name, name.split(' (')[0]]
    label = re.search(r'\(([^)]+)\)', name)
    if label:
        aliases.append(label[1])
        aliases.append(label[1].split(',')[0])
    return list(dict.fromkeys(aliases))


def mentions(text, value):
    return bool(re.search(r'(?<!\w)' + re.escape(value) + r'(?!\w)', text, re.I))


def resolve_scope(service, request, query, today=None):
    today = today or date.today()
    # Authorization occurs before the query or any source excerpts reach a model.
    service.facets(site=request.site, all_sites=request.all_sites, filters=request.filters.model_dump(mode='json'))
    filters = request.filters.model_dump(mode='json')
    notices = []
    negative = [re.split(r'\b(?:in|at|from|during|with|opened|raised)\b',re.sub(r'^\s*(?:in|at|from)\s+','',match[1],flags=re.I),maxsplit=1,flags=re.I)[0] for match in NEGATIVE.finditer(query)]
    classes = sorted({'Minor', 'Major', 'Critical', *[row.get('criticality','') for row in service.records.values() if row['site'] in service.allowed_sites]} - {''})
    excluded = {value for value in classes if any(mentions(part,value) for part in negative)}
    included = [value for value in classes if mentions(query,value) and value not in excluded]
    if not filters['criticalities'] and (included or excluded):
        filters['criticalities'] = included
        filters['exclude_criticalities']=sorted(set([*filters['exclude_criticalities'],*excluded]))
    elif filters['criticalities'] and (excluded.intersection(filters['criticalities']) or included and set(included)!=set(filters['criticalities'])):
        notices.append('The selected criticality filters take precedence over the different criticality scope in the search text.')
    years = requested_years(query, today.year)
    negative_years={int(value) for part in negative for value in re.findall(YEAR,part)}
    if not filters['years'] and (years is not None or negative_years):
        filters['years'] = [year for year in years or [] if year not in negative_years]
        filters['exclude_years']=sorted(set([*filters['exclude_years'],*negative_years]))
    elif filters['years'] and (negative_years.intersection(filters['years']) or years is not None and set(years)!=set(filters['years'])):
        notices.append('The selected year filters take precedence over the different year scope in the search text.')
    dates = requested_dates(query, today)
    if dates and not (filters['date_from'] or filters['date_to']):
        filters['date_from'], filters['date_to'] = [value.isoformat() if value else None for value in dates]
    elif re.search(r'\b(?:last|past|previous|this|current)\s+(?:\d+\s+)?(?:month|week|quarter)', query, re.I) and not dates:
        raise ValueError('The relative date phrase could not be resolved. Set an explicit opened-date range.')
    names = [row['site'] for row in service.sites()]
    # An explicit site list must never silently lose an unrecognised member.
    # Restrict this check to deliberate "... sites ... only" expressions so
    # ordinary event descriptions are not mistaken for site names.
    for clause in re.finditer(r'\b(?:from|in|at|sites?\s*:?)\s+([^;.!?]+?)\s+only\b', query, re.I):
        parts = re.split(r'\s+(?:and|or)\s+|,\s*', clause[1])
        if len(parts)>1 and any(any(mentions(part,alias) for name in names for alias in site_aliases(name)) for part in parts):
            unknown=[part.strip() for part in parts if not any(mentions(part,alias) for name in names for alias in site_aliases(name))]
            if unknown:
                raise ValueError('A site in the requested list is unavailable or unrecognised. Choose the intended sites in Filters.')
    excluded_sites = [name for name in names if any(any(mentions(part, alias) for alias in site_aliases(name)) for part in negative)]
    included_sites = [name for name in names if name not in excluded_sites and any(mentions(query,alias) for alias in site_aliases(name))]
    if not request.site:
        if not filters['sites'] and included_sites:
            filters['sites'] = included_sites
        filters['exclude_sites'] = sorted(set([*filters['exclude_sites'], *excluded_sites]))
    elif included_sites and request.site not in included_sites or request.site in excluded_sites:
        notices.append('The selected site takes precedence over the different site scope in the search text.')
    if filters['status'] == 'any':
        status_query = re.search(r'\b(currently\s+open|(?:open|closed)(?:\s*[-/]\s*done)?\s+(?:(?:major|minor|critical|historical)\s+)*(?:deviations?|records?|cases?))\b|\bstatus\s*(?::|is)?\s*(open|closed)\b',query,re.I)
        if status_query:
            filters['status'] = 'open' if mentions(status_query[0],'open') else 'closed'
            if any(mentions(part,'open') for part in negative):
                filters['status']='closed'
            elif any(mentions(part,'closed') for part in negative):
                filters['status']='open'
    if filters['status'] == 'open' and not any(not re.match(r'closed\b',row.get('status',''),re.I) for row in service.records.values() if row['site'] in service.allowed_sites):
        notices.append('This index contains historical closed deviations only. It cannot supply currently open deviations; no closed cases will be substituted.')
    # A single mentioned site retains the familiar site label; a union/exclusion
    # stays explicit inside the filters and is applied before candidate retrieval.
    site = request.site
    if not site and len(filters['sites']) == 1 and not filters['exclude_sites']:
        site = filters['sites'][0]
        filters['sites'] = []
    return {'filters': filters, 'site': site, 'all_sites': site is None, 'notices': notices, 'as_of': today.isoformat()}


def filter_only(query, service):
    text = query
    for row in service.sites():
        for alias in site_aliases(row['site']):
            text = re.sub(r'(?<!\w)' + re.escape(alias) + r'(?!\w)', ' ', text, flags=re.I)
    text = re.sub(r'\b\d{4}-\d{2}-\d{2}\b', ' ', text)
    stop = set(('please show find list browse all any the me us for from in at of on and or historical history deviation deviations record records case cases site sites year years during opened raised this last past previous current major minor critical with between to through until give get return which what are were have has been only where that occurred reported classification classified as not no include including exclude excluding except without other currently open closed done month months week weeks day days quarter since after before today yesterday').split())
    stop.update(name.lower() for name in calendar.month_name if name)
    stop.update({'how','many','number','count','counts','compared','compare','versus','vs'})
    return not [word for word in re.findall(r'[a-z0-9]+',text.lower()) if word not in stop and not word.isdigit()]


def question_intent(query):
    if re.search(r'\b(?:how many|number of|counts?|rate|percentage)\b|\b(?:compare|trend)\b.*\b(?:20\d{2}|by year|over time|versus)',query,re.I):
        return 'aggregate'
    if re.search(r'\b(?:actions?|handled|corrected|resolved|measures|changed|prevent)\b',query,re.I):
        return 'actions'
    if re.search(r'\bafter\b|\bpassed\b.*\blater\b|\bfollowing\s+(?:maintenance|retraining|CAPA)|\b(?:repeat|again|recurr)',query,re.I):
        return 'chronology'
    if re.search(r'\b(?:impact|evidence|assessment)\b',query,re.I):
        return 'evidence'
    return 'precedents'
