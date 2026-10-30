"""Request output profiles and conservative, size-aware patch batches."""
from pathlib import PurePosixPath

from langchain.agents.middleware import AgentMiddleware
from pydantic import BaseModel, Field, model_validator

from .context_management import estimated_tokens
from .models import PlatformError, digest


class GenerationSettings(BaseModel):
    code_output_tokens: int = Field(default=131072, ge=256, le=393216)
    text_output_tokens: int = Field(default=32768, ge=256, le=393216)
    text_retry_output_tokens: int = Field(default=65536, ge=256, le=393216)
    code_timeout_seconds: int = Field(default=600, ge=1, le=1800)
    text_timeout_seconds: int = Field(default=180, ge=1, le=1800)
    text_retry_timeout_seconds: int = Field(default=300, ge=1, le=1800)
    max_batch_files: int = Field(default=8, ge=1, le=16)
    batch_output_fraction: float = Field(default=0.5, ge=0.1, le=0.8)

    @model_validator(mode='after')
    def retry_not_smaller(self):
        if self.text_retry_output_tokens < self.text_output_tokens or self.text_retry_timeout_seconds < self.text_timeout_seconds:
            raise ValueError('普通阶段重试额度和超时不能小于初次请求。')
        return self


def request_limits(settings, *, coding=False, retry=False):
    profile = getattr(settings, 'generation', None)
    if profile is not None:
        return {'max_tokens': profile.code_output_tokens if coding else profile.text_retry_output_tokens if retry else profile.text_output_tokens,
                'timeout': profile.code_timeout_seconds if coding else profile.text_retry_timeout_seconds if retry else profile.text_timeout_seconds}
    return {'max_tokens': getattr(settings, 'model_max_output_tokens', 8192),
            'timeout': getattr(settings, 'model_timeout', 60)}


def generation_hash(settings):
    profile = getattr(settings, 'generation', None)
    return digest(profile.model_dump() if profile is not None else request_limits(settings))


def output_truncated(messages):
    for message in reversed(messages):
        if getattr(message, 'type', None) == 'ai':
            return getattr(message, 'response_metadata', {}).get('finish_reason') in {'length', 'max_tokens'}
    return False


class RequestLimitsMiddleware(AgentMiddleware):
    def __init__(self, limits, retry_limits=None, on_retry=None):
        self.limits = limits
        self.retry_limits, self.on_retry = retry_limits, on_retry
        self.promoted = False

    def wrap_model_call(self, request, handler):
        response = handler(request.override(model_settings={**request.model_settings, **self.limits}))
        if not output_truncated(response.result):
            return response
        if self.retry_limits and not self.promoted and self.retry_limits['max_tokens'] > self.limits['max_tokens']:
            if self.on_retry:
                self.on_retry(self.limits, self.retry_limits)
            self.limits, self.promoted = self.retry_limits, True
            # Replay the request view, not the truncated answer. The inner
            # context/usage middleware rechecks the window and task budget.
            response = handler(request.override(model_settings={**request.model_settings, **self.limits}))
            if not output_truncated(response.result):
                return response
        raise PlatformError('model_output_truncated', '模型输出达到本阶段上限并被截断，已保留原记录；编码会尝试拆小批次。')


def patch_weights(paths, context):
    known = {}
    for item in [*context.get('evidence', []), *context.get('previous_patch', {}).get('changes', [])]:
        body = item.get('content', item.get('snippet'))
        if isinstance(body, str) and item.get('path'):
            known[item['path']] = max(known.get(item['path'], 0), estimated_tokens({'path': item['path'], 'content': body}))
    weights = {}
    for path in paths:
        name = PurePosixPath(path)
        floor = 8192 if 'test' in name.stem.lower() else 1024 if name.suffix.lower() in {'.yml', '.yaml', '.json', '.xml', '.toml', '.properties'} else 4096
        # Allow growth and JSON encoding; the other half of the default output
        # budget remains available for reasoning and structured-output overhead.
        weights[path] = int(max(floor, known.get(path, 0))*1.5)+256
    return weights


def patch_batches(paths, context, settings):
    weights = patch_weights(paths, context)
    profile = getattr(settings, 'generation', None)
    if profile is None:
        return [paths[i:i+3] for i in range(0, len(paths), 3)], weights
    budget = int(profile.code_output_tokens*profile.batch_output_fraction)
    batches, current, size = [], [], 0
    for path in paths:
        if current and (len(current) >= profile.max_batch_files or size+weights[path] > budget):
            batches.append(current)
            current, size = [], 0
        current.append(path)
        size += weights[path]
    if current:
        batches.append(current)
    return batches, weights


def split_batch(batch, weights):
    total = sum(weights[path] for path in batch)
    prefix, candidates = 0, []
    for i, path in enumerate(batch[:-1], 1):
        prefix += weights[path]
        candidates.append((abs(prefix-total/2), i))
    middle = min(candidates)[1]
    return [batch[:middle], batch[middle:]]
