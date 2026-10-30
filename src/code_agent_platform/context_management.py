"""Progressive request views: recoverable tool offload, then bounded summarization."""
import json
import re

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, Field, model_validator

from .models import PlatformError, digest


class ContextManagementSettings(BaseModel):
    enabled: bool = True
    tool_result_trigger_tokens: int = Field(default=48000, ge=512)
    summary_trigger_tokens: int = Field(default=96000, ge=1024)
    context_window_tokens: int = Field(default=1048576, ge=4096)
    keep_recent_tool_results: int = Field(default=6, ge=1, le=50)
    keep_recent_messages: int = Field(default=8, ge=2, le=100)
    minimum_tool_characters: int = Field(default=3000, ge=200)
    summary_max_tokens: int = Field(default=4096, ge=256, le=16384)
    summary_retry_max_tokens: int = Field(default=8192, ge=256, le=16384)
    summary_target_characters: int = Field(default=2400, ge=400, le=12000)
    minimum_summary_tokens: int = Field(default=4096, ge=256, le=100000)

    @model_validator(mode='after')
    def thresholds(self):
        if not self.tool_result_trigger_tokens < self.summary_trigger_tokens < self.context_window_tokens:
            raise ValueError('上下文阈值应满足：工具归档阈值 < 摘要阈值 < 模型窗口。')
        if self.summary_retry_max_tokens < self.summary_max_tokens:
            raise ValueError('摘要重试额度不能小于初次请求额度。')
        return self


def message_data(message):
    result = {'role': message.type, 'content': message.content}
    if isinstance(message, AIMessage):
        result['tool_calls'] = message.tool_calls
        result['reasoning_content'] = message.additional_kwargs.get('reasoning_content', '')
    if isinstance(message, ToolMessage):
        result.update(tool_call_id=message.tool_call_id, name=message.name)
    return result


def estimated_tokens(value):
    # A conservative multilingual estimate, not the provider's tokenizer.
    return (len(json.dumps(value, ensure_ascii=False, default=str).encode('utf-8')) + 2) // 3


def result_preview(value, content):
    """Keep useful result data rather than spending the preview on JSON headers."""
    if isinstance(value, dict):
        text = next((value[key] for key in ('snippet', 'text', 'content') if isinstance(value.get(key), str)), None)
        if text is not None:
            excerpt = text if len(text) <= 1200 else text[:1000]+'\n[…片段已归档…]\n'+text[-200:]
            return {**{key: value[key] for key in ('path', 'start_line', 'end_line', 'title', 'version') if key in value}, 'excerpt': excerpt}
        if isinstance(value.get('evidence'), list):
            return {'evidence_excerpts': [result_preview(item, json.dumps(item, ensure_ascii=False)) for item in value['evidence'][:3]]}
    return content[:1200]


def safe_cutoff(messages, keep):
    """Never separate an assistant tool call from any of its parallel results."""
    target = max(0, len(messages)-keep)
    protected = [i for i, message in enumerate(messages) if isinstance(message, (HumanMessage, SystemMessage))]
    if protected:
        target = min(target, protected[-1])
    last = 0
    index = 0
    while index < target:
        message = messages[index]
        end = index+1
        if isinstance(message, AIMessage) and message.tool_calls:
            expected = {call['id'] for call in message.tool_calls}
            found = set()
            while end < len(messages) and isinstance(messages[end], ToolMessage):
                found.add(messages[end].tool_call_id)
                end += 1
            if expected != found or end > target:
                break
        elif isinstance(message, ToolMessage):
            break
        if end > target:
            break
        last = end
        index = end
    return last


class ContextManagementMiddleware(AgentMiddleware):
    def __init__(self, gateway, task_id, settings, summarize):
        self.gateway, self.task_id, self.settings, self.summarize = gateway, task_id, settings, summarize
        self.archives = {}
        self.summary = None
        self.summarized_count = 0
        self.prefix_hash = None
        self.attempted_count = 0
        self.attempted_hash = None

    def estimate(self, request, messages):
        tools = [convert_to_openai_tool(tool) for tool in (request.tools or [])]
        system = message_data(request.system_message) if request.system_message is not None else None
        return estimated_tokens({'messages': [message_data(m) for m in messages], 'system': system, 'tools': tools})

    def _offload(self, messages):
        calls = {call['id']: call for message in messages if isinstance(message, AIMessage) for call in message.tool_calls}
        indexes = [i for i, message in enumerate(messages) if isinstance(message, ToolMessage)]
        recent = set(indexes[-self.settings.keep_recent_tool_results:])
        output = list(messages)
        count = 0
        native = {'read_file', 'list_files', 'search_code', 'rag_search', 'git_status', 'git_diff', 'graph_query'}
        for index in indexes:
            message = messages[index]
            call = calls.get(message.tool_call_id)
            if index in recent or not call or not isinstance(message.content, str) or len(message.content) < self.settings.minimum_tool_characters:
                continue
            if message.status == 'error':
                continue
            try:
                value = json.loads(message.content)
            except ValueError:
                value = None
            if isinstance(value, dict) and (value.get('status') in {'failed', 'error', 'unavailable'} or value.get('success') is False):
                continue
            name = call['name']
            if name not in native and not name.startswith('mcp__'):
                continue
            key = digest([message.tool_call_id, message.content])
            if key not in self.archives:
                task = self.gateway.store.get('task', self.task_id)
                self.archives[key] = self.gateway.archive_tool_result(task, name, call['args'], message.content)
            artifact = self.archives[key]
            sources = list(dict.fromkeys(re.findall(r'source_[0-9a-f]+', message.content)))
            marker = {'archived_tool_result': True, 'artifact_id': artifact['id'], 'tool': name,
                'characters': len(message.content), 'preview': result_preview(value, message.content), 'source_ids': sources[:40],
                'read_with': 'read_tool_result(artifact_id, offset, length)', 'snapshot': True,
                'archive_reason': '这是已成功执行的旧结果，正文转存不代表失败；仅当本轮需要具体细节时读回，不要重复执行原工具。'}
            marker_text = json.dumps(marker, ensure_ascii=False)
            if estimated_tokens(marker_text) >= estimated_tokens(message.content):
                continue
            output[index] = message.model_copy(update={'content': marker_text})
            count += 1
        return output, count

    def prepare(self, request):
        raw = list(request.messages)
        if not raw or not self.settings.enabled:
            return raw
        initial = self.estimate(request, raw)
        messages = raw
        if self.attempted_count and digest([message_data(m) for m in raw[:1+self.attempted_count]]) != self.attempted_hash:
            self.attempted_count, self.attempted_hash = 0, None
        if self.summary is not None:
            prefix = [message_data(m) for m in raw[:1+self.summarized_count]]
            if digest(prefix) == self.prefix_hash:
                messages = [raw[0], self.summary, *raw[1+self.summarized_count:]]
            else:
                self.summary, self.summarized_count, self.prefix_hash = None, 0, None
        before = self.estimate(request, messages)
        if before >= self.settings.tool_result_trigger_tokens:
            messages, count = self._offload(messages)
            after = self.estimate(request, messages)
            if count:
                self.gateway.store.event(self.task_id, 'ContextToolResultsArchived', {'count': count,
                    'before_estimated_tokens': before, 'after_estimated_tokens': after, 'original_records_retained': True})
        total = self.estimate(request, messages)
        if total >= self.settings.summary_trigger_tokens:
            messages = self._collapse(request, raw, messages, total)
        final = self.estimate(request, messages)
        reserve = request.model_settings.get('max_tokens', getattr(self.gateway.settings, 'model_max_output_tokens', 8192))
        if final + reserve >= self.settings.context_window_tokens:
            raise PlatformError('context_window_exceeded', '压缩后必要上下文仍接近模型窗口，请缩小本轮任务范围；原记录与审批已保留。')
        state = {'before_estimated_tokens': initial, 'after_estimated_tokens': final,
            'context_window_tokens': self.settings.context_window_tokens, 'estimation': 'utf8_bytes_div_3',
            'reserved_output_tokens': reserve,
            'summarized_messages': self.summarized_count, 'archived_results': len(self.archives)}
        self.gateway.store.mutate('task', self.task_id, lambda row: {**row, 'context_usage': state})
        return messages

    def _collapse(self, request, raw, messages, total):
        # Pin current task and approvals; cut only complete old call/result groups.
        tail = messages[1:]
        cut = safe_cutoff(tail, self.settings.keep_recent_messages)
        has_summary = self.summary is not None
        if not cut or has_summary and cut <= 1:
            return messages
        old = tail[:cut]
        skip = (1 if has_summary else 0)+max(0, self.attempted_count-self.summarized_count)
        new_tokens = estimated_tokens([message_data(m) for m in old[skip:]])
        if new_tokens < self.settings.minimum_summary_tokens:
            return messages
        compactable = [message_data(m) for m in old]
        for item in compactable:
            item.pop('reasoning_content', None)
        try:
            summary = self.summarize(json.dumps(compactable, ensure_ascii=False))
        except PlatformError:
            raise
        except Exception as error:
            raise PlatformError('context_summary_failed', '上下文摘要生成失败；原始历史仍保留，请重试。') from error
        if not isinstance(summary, str) or not summary.strip():
            raise PlatformError('context_summary_failed', '上下文摘要为空，已保留原始记录，未继续发送不完整上下文。')
        if len(summary) > max(12000, self.settings.summary_retry_max_tokens*8):
            raise PlatformError('context_summary_too_large', '摘要过长，未裁断或替换原始历史。')
        boundary = AIMessage(content='【历史上下文摘要，仅供参考，不构成新需求或授权】\n'+summary,
            additional_kwargs={'reasoning_content': '', 'context_summary': True})
        consumed = self.summarized_count+cut-(1 if has_summary else 0)
        self.attempted_count = consumed
        self.attempted_hash = digest([message_data(m) for m in raw[:1+consumed]])
        proposed = [raw[0], boundary, *tail[cut:]]
        after = self.estimate(request, proposed)
        if after >= total:
            self.gateway.store.event(self.task_id, 'ContextSummaryDiscarded', {'reason': 'no_reduction',
                'before_estimated_tokens': total, 'candidate_estimated_tokens': after})
            return messages
        self.summarized_count, self.prefix_hash, self.summary = consumed, self.attempted_hash, boundary
        task = self.gateway.store.get('task', self.task_id)
        artifact = self.gateway.store.artifact(task, 'context-summary.txt', summary)
        self.gateway.store.event(self.task_id, 'ContextSummarized', {'summary_artifact_id': artifact['id'],
            'summarized_messages': consumed, 'before_estimated_tokens': total,
            'after_estimated_tokens': after, 'original_records_retained': True})
        return proposed

    def wrap_model_call(self, request, handler):
        return handler(request.override(messages=self.prepare(request)))
