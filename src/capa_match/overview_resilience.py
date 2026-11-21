"""Bounded overview repair with a request-local provider time budget."""
from contextvars import ContextVar
from time import monotonic
import logging

LOG = logging.getLogger(__name__)
_deadline = ContextVar('match_overview_deadline', default=None)
MAX_ATTEMPTS = 2
TIME_BUDGET_SECONDS = 75


class InvalidModelOutput(ValueError):
    """A safe reason code, never the draft, source text or upstream response."""


class OverviewUnavailable(RuntimeError):
    pass


def provider_timeout(configured):
    if not 0 < configured <= 300:
        raise ValueError('Invalid provider timeout')
    deadline = _deadline.get()
    if deadline is None:
        return configured
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError('Overview time budget exhausted')
    return min(configured, remaining)


class OverviewSession:
    def __init__(self, llm):
        self.llm = llm
        self.deadline = monotonic() + TIME_BUDGET_SECONDS
        self.attempts = {'draft': 0, 'grounding': 0}
        self.provider_retries = 0
        self.extra_retry_used = False
        self.usage = {'input_tokens': 0, 'output_tokens': 0}
        self.last_reason = None

    def invoke(self, stage, name, prompt, data, schema, max_tokens, validate):
        from .intelligence import AssistanceUnavailable
        reason = None
        draft = None
        validation_failures = 0
        while validation_failures < MAX_ATTEMPTS:
            if monotonic() >= self.deadline:
                self.last_reason = 'time_budget'
                raise OverviewUnavailable('time_budget')
            if sum(self.attempts.values()) >= 5 or self.attempts[stage] >= 3:
                raise OverviewUnavailable(reason or 'verification_failed')
            self.attempts[stage] += 1
            request = data
            instructions = prompt
            if validation_failures:
                instructions += ('\nRepair the previous response using exactly the same evidence and constraints. '
                                 'Do not invent support or turn a proposed action into an implemented one. '
                                 'Use only the references array for deviation citations. '
                                 'For summary text, use two sentences and at most 40 words TOTAL.')
                request = {**data, 'repair_reason': reason}
                if stage == 'draft' and isinstance(draft, list):
                    request['draft_to_check'] = [
                        {'text': str(line.get('text', ''))[:500],
                         'references': line.get('references', [])[:3] if isinstance(line.get('references'), list) else []}
                        for line in draft[:4] if isinstance(line, dict)]
            token = _deadline.set(self.deadline)
            try:
                generated, usage = self.llm.invoke(name, instructions, request, schema, max_tokens)
                for key in ('input_tokens', 'output_tokens'):
                    self.usage[key] += usage.get(key, 0)
                if usage.get('model'):
                    self.usage['model'] = usage['model']
                draft = generated.get('lines') if isinstance(generated, dict) else None
                return validate(generated)
            except InvalidModelOutput as error:
                reason = str(error)
                validation_failures += 1
            except AssistanceUnavailable as error:
                reason = getattr(error, 'reason', 'provider_unavailable')
                if not getattr(error, 'retryable', True):
                    self.last_reason = reason
                    raise OverviewUnavailable(reason) from None
                if reason == 'invalid_model_response':
                    validation_failures += 1
                elif self.provider_retries < 1 and not self.extra_retry_used:
                    self.provider_retries += 1
                    self.extra_retry_used = True
                else:
                    self.last_reason = reason
                    raise OverviewUnavailable(reason) from None
            finally:
                _deadline.reset(token)
            self.last_reason = reason
            LOG.info('MATCH overview retry stage=%s attempt=%s reason=%s', stage, self.attempts[stage], reason)
            if validation_failures >= MAX_ATTEMPTS and not self.extra_retry_used:
                self.extra_retry_used = True
                validation_failures -= 1
        raise OverviewUnavailable(reason or 'verification_failed')

    def metadata(self, outcome, reason=None):
        attempts = dict(self.attempts)
        return {'verification_status': outcome,
                'verification_attempts': attempts,
                'verification_retries': sum(max(0, count - 1) for count in attempts.values()),
                'verification_provider_retries': self.provider_retries,
                'verification_reason': reason,
                'usage': {**self.usage, 'grounding_checked': attempts['grounding'] > 0,
                          **({'revision_pass': True} if attempts['draft'] > 1 else {})}}
