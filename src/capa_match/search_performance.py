"""Bounded, authority-scoped result reuse and server-owned relevance reviews."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from threading import Lock
from time import monotonic
from uuid import uuid4
from match_v1.search import SiteAccessError


class SearchPerformance:
    def __init__(self, owner, cache):
        self.owner = owner
        self.completed = cache(limit=128, ttl=900)
        self.prepared = cache(limit=64, ttl=900)
        self.reviews = cache(limit=64, ttl=900)
        self.locks = [Lock() for _ in range(32)]

    @staticmethod
    def authorize(service, request):
        if request.all_sites:
            if not service.sites():
                raise SiteAccessError('No authorised indexed sites.')
        else:
            service._authorized_site(request.site)

    def key(self, service, request):
        criteria = request.model_dump(mode='json', exclude={'progressive'})
        for name, value in criteria['filters'].items():
            if isinstance(value, list):
                criteria['filters'][name] = sorted(set(value))
        model = [getattr(self.owner.llm, name, None) for name in ['model', 'review_model', 'base', 'configured']]
        data = ['match-search-performance-v1', self.owner.authority(service),
                datetime.now(timezone.utc).date().isoformat(), model, criteria]
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    def bind(self, service, result, request, cached=False):
        value = deepcopy(result)
        value['submitted_criteria'] = request.model_dump(mode='json')
        value['cache_hit'] = cached
        if cached:
            value['assistant_usage'] = None
            value['relevance_review_usage'] = None
        return self.owner.retain_result(service, value)

    def remember(self, key, result):
        value = deepcopy(result)
        value.pop('summary_id', None)
        degraded = value.get('review_state') in {'unavailable', 'local_ranking'} or (value.get('interpretation') or {}).get('origin') == 'local'
        self.completed.put(key, {'result': value, 'until': monotonic() + (15 if degraded else 900)})

    def run(self, service, request, retrieve):
        self.authorize(service, request)
        key = self.key(service, request)
        with self.locks[int(key[:8], 16) % len(self.locks)]:
            cached = self.completed.get(key)
            if cached is not None and monotonic() < cached['until']:
                return self.bind(service, cached['result'], request, True)
            pending = self.prepared.get(key)
            if pending is not None and pending['final'] is None:
                if request.progressive:
                    return {**deepcopy(pending['initial']), 'submitted_criteria': request.model_dump(mode='json')}
                return self.review(service, pending['id'], request)
            result = retrieve(service, request)
            if result.get('review_state') != 'pending':
                self.remember(key, result)
            return result

    def prepare(self, service, request, result, candidates, review_query=None):
        token = uuid4().hex
        key = self.key(service, request)
        initial = {**deepcopy(result), 'review_id': token, 'review_state': 'pending', 'summary_id': None, 'cache_hit': False}
        if 'question_intent' in result:
            initial['search_id'] = None
        entry = {'id': token, 'key': key, 'authority': self.owner.authority(service),
                 'request': request.model_copy(deep=True), 'initial': initial,
                 'candidates': deepcopy(candidates), 'review_query': review_query or request.query.strip(), 'lock': Lock(), 'final': None}
        self.reviews.put(token, entry)
        self.prepared.put(key, entry)
        return initial

    def review(self, service, token, request=None):
        entry = self.reviews.get(token)
        if entry is None or entry['authority'] != self.owner.authority(service):
            raise KeyError('This relevance review expired. Search again.')
        self.authorize(service, entry['request'])
        with entry['lock']:
            if entry['authority'] != self.owner.authority(service):
                raise KeyError('The search authority changed.')
            reused = entry['final'] is not None
            if not reused:
                selected, usage, state = self.owner.review_candidates(service, entry['review_query'], entry['candidates'], entry['request'].top_k)
                result = {**deepcopy(entry['initial']), 'results': selected, 'result_count': len(selected),
                          'relevance_review_usage': usage, 'review_state': state}
                if hasattr(self.owner, 'finalize_review_result'):
                    result = self.owner.finalize_review_result(result)
                if entry['authority'] != self.owner.authority(service):
                    raise KeyError('The search authority changed.')
                entry['final'] = result
                self.remember(entry['key'], result)
            return self.bind(service, entry['final'], request or entry['request'], reused)
