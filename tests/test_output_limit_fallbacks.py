import unittest
from unittest.mock import Mock, patch

from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from code_agent_platform.context_management import ContextManagementMiddleware, ContextManagementSettings
from code_agent_platform.engine import ModelEngine, scoped_patch_type
from code_agent_platform.generation import GenerationSettings
from code_agent_platform.models import AnswerDraft, Mode, PlatformError
from test_generation_profiles import CaptureModel
import test_platform as fixtures


class TruncatingModel(CaptureModel):
    truncate_count: int = 1
    seen: list[list[str]] = Field(default_factory=list)

    def _generate(self, messages, **kwargs):
        self.seen.append([str(m.content) for m in messages])
        if len(self.calls) < self.truncate_count:
            self.calls.append(kwargs)
            message = AIMessage(content='TRUNCATED_UNSAFE_TAIL', response_metadata={'finish_reason': 'length'},
                usage_metadata={'input_tokens': 12, 'output_tokens': 8, 'total_tokens': 20})
            return ChatResult(generations=[ChatGeneration(message=message)])
        return super()._generate(messages, **kwargs)


class ToolHistoryModel(CaptureModel):
    truncate_tool_call: bool = False
    seen: list[list[str]] = Field(default_factory=list)

    def _generate(self, messages, **kwargs):
        self.seen.append([str(m.content) for m in messages])
        index = len(self.calls)
        if index == 0 or index == 1 and not self.truncate_tool_call:
            self.calls.append(kwargs)
            first = index == 0
            message = AIMessage(content='' if first else 'TRUNCATED_UNSAFE_TAIL',
                tool_calls=[{'id': 'read', 'name': 'read_file', 'args': {'path': 'calculator.py'}}] if first else [],
                response_metadata={'finish_reason': 'length' if self.truncate_tool_call or not first else 'tool_calls'},
                usage_metadata={'input_tokens': 12, 'output_tokens': 8, 'total_tokens': 20})
            return ChatResult(generations=[ChatGeneration(message=message)])
        return super()._generate(messages, **kwargs)


class OutputFallbackTests(unittest.TestCase):
    setUp = fixtures.PlatformTests.setUp
    task = fixtures.PlatformTests.task

    def enable(self):
        self.settings.generation = GenerationSettings()

    def test_text_truncation_promotes_once_without_replaying_incomplete_answer(self):
        self.enable()
        task = self.task()
        model = TruncatingModel()
        with patch.object(type(self.settings), 'model', return_value=model):
            answer = ModelEngine(self.settings, self.platform.gateway).run(self.actor, task, {}, AnswerDraft, 'Return fixture')
        self.assertEqual(answer.answer, 'fixture answer')
        self.assertEqual([call['max_tokens'] for call in model.calls], [32768, 65536])
        self.assertEqual([call['timeout'] for call in model.calls], [180, 300])
        self.assertEqual(model.seen[0], model.seen[1])
        self.assertNotIn('TRUNCATED_UNSAFE_TAIL', '\n'.join(model.seen[1]))
        current = self.platform.store.get('task', task['id'])
        self.assertEqual(current['usage']['model_calls'], 2)
        self.assertEqual(current['context_usage']['reserved_output_tokens'], 65536)
        records = sorted(self.platform.store.list('model_call', task_id=task['id']), key=lambda row: row['created_at'])
        self.assertEqual([row['output_limit_tokens'] for row in records], [32768, 65536])
        self.assertEqual([row['output_truncated'] for row in records], [True, False])

    def test_two_truncations_stop_and_coding_does_not_use_text_fallback(self):
        self.enable()
        for coding in (False, True):
            task = self.task(Mode.WRITE if coding else Mode.READONLY)
            model = TruncatingModel(coding=coding, truncate_count=100)
            with patch.object(type(self.settings), 'model', return_value=model), self.assertRaises(PlatformError) as error:
                ModelEngine(self.settings, self.platform.gateway).run(self.actor, task, {}, scoped_patch_type(['new.java']) if coding else AnswerDraft, 'Return fixture')
            self.assertEqual(error.exception.code, 'model_output_truncated')
            self.assertEqual([call['max_tokens'] for call in model.calls], [131072] if coding else [32768, 65536])

    def test_tools_in_truncated_response_are_not_executed(self):
        self.enable()
        task = self.task()
        model = ToolHistoryModel(truncate_tool_call=True)
        with patch.object(type(self.settings), 'model', return_value=model), patch.object(self.platform.gateway, 'invoke', wraps=self.platform.gateway.invoke) as invoke:
            ModelEngine(self.settings, self.platform.gateway).run(self.actor, task, {}, AnswerDraft, 'Return fixture')
        invoke.assert_not_called()
        self.assertEqual([call['max_tokens'] for call in model.calls], [32768, 65536])

    def test_completed_tool_result_is_reused_without_reexecuting_tool(self):
        self.enable()
        task = self.task()
        model = ToolHistoryModel()
        with patch.object(type(self.settings), 'model', return_value=model), patch.object(self.platform.gateway, 'invoke', wraps=self.platform.gateway.invoke) as invoke:
            ModelEngine(self.settings, self.platform.gateway).run(self.actor, task, {}, AnswerDraft, 'Return fixture')
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual([call['max_tokens'] for call in model.calls], [32768, 32768, 65536])
        self.assertEqual(model.seen[1], model.seen[2])

    def test_text_retry_cannot_exceed_task_call_budget(self):
        self.enable()
        task = self.task()
        self.platform.store.mutate('task', task['id'], lambda row: {**row, 'budget': {**row['budget'], 'max_model_calls': 1}})
        model = TruncatingModel()
        with patch.object(type(self.settings), 'model', return_value=model), self.assertRaises(PlatformError) as error:
            ModelEngine(self.settings, self.platform.gateway).run(self.actor, task, {}, AnswerDraft, 'Return fixture')
        self.assertEqual(error.exception.code, 'model_budget')
        self.assertEqual(len(model.calls), 1)

    def summary_reply(self, *, truncated=False):
        return AIMessage(content='TRUNCATED_UNSAFE_TAIL' if truncated else '保留原始用户约束与未完成测试，文件内容按归档 ID 读回。',
            response_metadata={'finish_reason': 'length' if truncated else 'stop'},
            usage_metadata={'input_tokens': 80, 'output_tokens': 20, 'total_tokens': 100})

    def test_summary_retry_uses_same_input_and_audits_both_calls(self):
        self.enable()
        task = self.task()
        model = Mock()
        model.bind.return_value.invoke.side_effect = [self.summary_reply(truncated=True), self.summary_reply()]
        with patch.object(type(self.settings), 'model', return_value=model):
            result = ModelEngine(self.settings, self.platform.gateway)._summarize_context(task, '历史记录')
        self.assertIn('用户约束', result)
        self.assertEqual([call.kwargs['max_tokens'] for call in model.bind.call_args_list], [4096, 8192])
        requests = model.bind.return_value.invoke.call_args_list
        self.assertEqual(requests[0].args[0], requests[1].args[0])
        prompt = requests[1].args[0][0].content
        self.assertIn('2400', prompt)
        self.assertNotIn('800个', prompt)
        self.assertEqual(self.platform.store.get('task', task['id'])['usage']['model_calls'], 2)

    def test_summary_double_truncation_does_not_publish_partial_summary(self):
        self.enable()
        task = self.task()
        model = Mock()
        model.bind.return_value.invoke.return_value = self.summary_reply(truncated=True)
        with patch.object(type(self.settings), 'model', return_value=model), self.assertRaises(PlatformError) as error:
            ModelEngine(self.settings, self.platform.gateway)._summarize_context(task, '历史记录')
        self.assertEqual(error.exception.code, 'context_summary_truncated')
        self.assertEqual(model.bind.return_value.invoke.call_count, 2)
        self.assertFalse(self.platform.store.list('artifact', task_id=task['id']))

    def test_summary_retry_checks_budget_again(self):
        self.enable()
        task = self.task()
        self.platform.store.mutate('task', task['id'], lambda row: {**row, 'budget': {**row['budget'], 'max_model_calls': 1}})
        model = Mock()
        model.bind.return_value.invoke.return_value = self.summary_reply(truncated=True)
        with patch.object(type(self.settings), 'model', return_value=model), self.assertRaises(PlatformError) as error:
            ModelEngine(self.settings, self.platform.gateway)._summarize_context(task, '历史记录')
        self.assertEqual(error.exception.code, 'model_budget')
        self.assertEqual(model.bind.return_value.invoke.call_count, 1)

    def test_larger_summary_is_not_rejected_by_old_character_guard(self):
        task = self.task()
        config = ContextManagementSettings(tool_result_trigger_tokens=1000, summary_trigger_tokens=3000,
            context_window_tokens=200000, keep_recent_messages=2)
        manager = ContextManagementMiddleware(self.platform.gateway, task['id'], config, lambda payload: 'facts '*2500)
        original = [HumanMessage(content='只读需求'), AIMessage(content='old '*30000), AIMessage(content='recent'), AIMessage(content='latest')]
        prepared = manager.prepare(ModelRequest(model=Mock(), messages=original, tools=[], system_message=SystemMessage(content='rules')))
        self.assertTrue(prepared[1].additional_kwargs['context_summary'])
        self.assertGreater(len(prepared[1].content), 12000)
        self.assertEqual(original[1].content, 'old '*30000)


if __name__ == '__main__':
    unittest.main()
