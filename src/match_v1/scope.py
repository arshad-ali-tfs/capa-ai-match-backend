"""Source workflow categories used consistently by ingestion, search and evidence."""
import re

POLICY_VERSION = 'division-status-scope-v1'


def workflow_state(status):
    value = re.sub(r'[\s\u2010-\u2015_-]+', ' ', str(status or '').strip()).casefold()
    if value == 'closed done':
        return 'completed'
    if re.search(r'\bcancell?ed\b', value):
        return 'cancelled'
    if not value or value.startswith('closed') or value in {'unknown', 'n/a', 'none'}:
        return 'other'
    return 'ongoing'


def status_counts(records):
    counts = dict.fromkeys(('completed', 'ongoing', 'cancelled', 'other'), 0)
    for record in records:
        counts[workflow_state(record.get('status'))] += 1
    return counts


def accepts_status(status, scope='completed', include_cancelled=False):
    state = workflow_state(status)
    if state == 'cancelled':
        return scope == 'all' and include_cancelled
    return state in ({'completed', 'ongoing', 'other'} if scope == 'all' else {scope})
