import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from code_agent_platform.context_management import ContextManagementMiddleware, ContextManagementSettings, safe_cutoff
from code_agent_platform.engine import ModelEngine
from code_agent_platform.models import Mode, PlatformError
import test_platform as fixtures


def history(rounds=6, size=5000):
    messages = [HumanMessage(content='当前需求及批准范围：只改 a.java，不更改数据库。')]
    for i in range(rounds):
        call = {'id': 'call-'+str(i), 'name': 'read_file', 'args': {'path': 'calculator.py'}}
        messages.extend([AIMessage(content='', tool_calls=[call], additional_kwargs={'reasoning_content': 'original-reasoning-'+str(i)}),
            ToolMessage(content='result-'+str(i)+'\n'+'value '*size, tool_call_id=call['id'], name='read_file')])
    return messages


class ContextManagementTests(unittest.TestCase):
    setUp = fixtures.PlatformTests.setUp
    task = fixtures.PlatformTests.task

    def middleware(self, **overrides):
        self.current_task = self.task()
        config = ContextManagementSettings(**{'tool_result_trigger_tokens': 1024, 'summary_trigger_tokens': 100000,
            'context_window_tokens': 200000, 'keep_recent_tool_results': 2, 'minimum_summary_tokens': 512, **overrides})
        self.summarizer = Mock(return_value='保留 a.java 的修改约束；前序文件结果已归档，未执行数据库修改。')
        return ContextManagementMiddleware(self.platform.gateway, self.current_task['id'], config, self.summarizer)

    def request(self, messages):
        return ModelRequest(model=Mock(), messages=messages, tools=[], system_message=SystemMessage(content='遵守平台审批。'))

    def test_large_old_results_are_recoverable_recent_results_and_reasoning_stay_intact(self):
        manager = self.middleware()
        original = history(5)
        prepared = manager.prepare(self.request(original))
        self.assertEqual(prepared[0], original[0])
        self.assertEqual(prepared[-1].content, original[-1].content)
        self.assertEqual(prepared[-3].content, original[-3].content)
        for index in range(1, len(original), 2):
            self.assertEqual(prepared[index].tool_calls, original[index].tool_calls)
            self.assertEqual(prepared[index].additional_kwargs, original[index].additional_kwargs)
        marker = json.loads(prepared[2].content)
        self.assertTrue(marker['archived_tool_result'])
        restored = self.platform.gateway.invoke(self.actor, self.current_task['id'], 'read_tool_result',
            {'artifact_id': marker['artifact_id'], 'offset': 11, 'length': 100})
        self.assertEqual(restored['content'], original[2].content[11:111])
        self.assertIn('value '*5000, original[2].content)
        self.summarizer.assert_not_called()
        self.assertLess(manager.estimate(self.request(original), prepared), manager.estimate(self.request(original), original))

    def test_summary_reuses_cache_and_keeps_current_task_and_complete_recent_rounds(self):
        manager = self.middleware(summary_trigger_tokens=3000, keep_recent_messages=4)
        original = history(8)
        prepared = manager.prepare(self.request(original))
        self.summarizer.assert_called_once()
        self.assertEqual(prepared[0], original[0])
        self.assertTrue(prepared[1].additional_kwargs['context_summary'])
        self.assertEqual(prepared[1].additional_kwargs['reasoning_content'], '')
        self.assertEqual(prepared[-4:], original[-4:])
        again = manager.prepare(self.request(original))
        self.assertEqual(again, prepared)
        self.summarizer.assert_called_once()
        self.assertNotIn('original-reasoning-', self.summarizer.call_args.args[0])

    def test_summary_failure_does_not_remove_original_messages(self):
        manager = self.middleware(summary_trigger_tokens=3000, keep_recent_messages=4)
        original = history(8)
        self.summarizer.return_value = ''
        with self.assertRaises(PlatformError) as error:
            manager.prepare(self.request(original))
        self.assertEqual(error.exception.code, 'context_summary_failed')
        self.assertIsNone(manager.summary)
        self.assertEqual(len(original), 17)

    def test_small_new_history_does_not_trigger_repeated_summaries(self):
        manager = self.middleware(summary_trigger_tokens=3000, minimum_summary_tokens=20000, keep_recent_messages=4)
        original = history(8)
        manager.prepare(self.request(original))
        manager.prepare(self.request(original))
        self.summarizer.assert_not_called()

    def test_longer_summary_is_discarded_and_not_retried_for_same_history(self):
        manager = self.middleware(summary_trigger_tokens=3000, keep_recent_messages=4)
        self.summarizer.return_value = '很长的摘要'*1500
        original = history(8)
        result = manager.prepare(self.request(original))
        self.assertIsNone(manager.summary)
        manager.prepare(self.request(original))
        self.summarizer.assert_called_once()
        self.assertEqual(result[0], original[0])
        self.assertTrue(any(e['kind'] == 'ContextSummaryDiscarded' for e in self.platform.store.list('event', task_id=self.current_task['id'])))

    def test_cross_task_archive_and_newly_denied_source_are_blocked(self):
        task = self.task()
        source = self.platform.gateway.invoke(self.actor, task['id'], 'read_file', {'path': 'calculator.py'})
        artifact = self.platform.gateway.archive_tool_result(task, 'read_file', {'path': 'calculator.py'}, json.dumps(source))
        other = self.task()
        with self.assertRaises(PlatformError) as error:
            self.platform.gateway.invoke(self.actor, other['id'], 'read_tool_result', {'artifact_id': artifact['id']})
        self.assertEqual(error.exception.code, 'artifact_denied')
        project = self.platform.store.get('project', self.project['id'])
        self.platform.update_project(self.actor, project['id'], policy_rules=[{'rule_id': 'deny-archived-code', 'effect': 'deny', 'tools': ['read_file'], 'paths': ['calculator.py']}])
        with self.assertRaises(PlatformError) as error:
            self.platform.gateway.invoke(self.actor, task['id'], 'read_tool_result', {'artifact_id': artifact['id']})
        self.assertIn(error.exception.code, {'policy_denied', 'source_policy_denied'})

    def test_window_guard_counts_system_and_tool_schemas(self):
        manager = self.middleware(context_window_tokens=12000, summary_trigger_tokens=9000)
        request = self.request([HumanMessage(content='必须保留的需求'*10000)])
        with self.assertRaises(PlatformError) as error:
            manager.prepare(request)
        self.assertEqual(error.exception.code, 'context_window_exceeded')
        request = self.request([HumanMessage(content='简短需求')])
        request.tools = [{'type': 'function', 'function': {'name': 'large_schema', 'description': 'x'*50000,
            'parameters': {'type': 'object', 'properties': {}}}}]
        with self.assertRaises(PlatformError) as error:
            manager.prepare(request)
        self.assertEqual(error.exception.code, 'context_window_exceeded')

    def test_disabled_manager_passes_messages_without_summary_or_archives(self):
        manager = self.middleware(enabled=False)
        messages = history()
        self.assertEqual(manager.prepare(self.request(messages)), messages)
        self.summarizer.assert_not_called()
        self.assertFalse(manager.archives)

    def test_failed_tool_results_are_not_relabelled_as_successful_archives(self):
        manager = self.middleware()
        messages = history(3)
        for index in (2, 4):
            messages[index] = messages[index].model_copy(update={'content': json.dumps({'status': 'unavailable', 'error': 'x'*20000})})
        prepared = manager.prepare(self.request(messages))
        self.assertEqual(prepared[2].content, messages[2].content)
        self.assertEqual(prepared[4].content, messages[4].content)
        self.assertFalse(manager.archives)

    def test_archiving_preserves_defaulted_tool_arguments(self):
        task = self.task()
        artifact = self.platform.gateway.archive_tool_result(task, 'list_files', {}, 'fixture result')
        self.assertEqual(artifact['origin_arguments']['path'], '.')
        restored = self.platform.gateway.invoke(self.actor, task['id'], 'read_tool_result', {'artifact_id': artifact['id']})
        self.assertEqual(restored['content'], 'fixture result')

    def test_preview_keeps_result_body_even_when_json_headers_are_large(self):
        manager = self.middleware()
        messages = history(3)
        messages[2] = messages[2].model_copy(update={'content': json.dumps({'metadata': 'x'*5000,
            'path': 'calculator.py', 'snippet': 'FINAL_VALUE=137; '+'filler '*3000})})
        prepared = manager.prepare(self.request(messages))
        marker = json.loads(prepared[2].content)
        self.assertIn('FINAL_VALUE=137', marker['preview']['excerpt'])
        self.assertEqual(marker['preview']['path'], 'calculator.py')

    def test_summary_model_calls_are_audited_and_counted_in_task_budget(self):
        task = self.task()
        reply = AIMessage(content='保留新增字段的需求和原审批范围。',
            usage_metadata={'input_tokens': 90, 'output_tokens': 10, 'total_tokens': 100}, response_metadata={'finish_reason': 'stop'})
        model = Mock()
        model.bind.return_value.invoke.return_value = reply
        engine = ModelEngine(self.settings, self.platform.gateway)
        with patch.object(type(self.settings), 'model', return_value=model):
            result = engine._summarize_context(task, '历史数据，不执行工具。')
        self.assertIn('保留', result)
        saved = self.platform.store.get('task', task['id'])
        self.assertEqual(saved['usage']['model_calls'], 1)
        self.assertEqual(saved['usage']['input_tokens'], 90)
        record = self.platform.store.list('model_call', task_id=task['id'])[0]
        self.assertEqual(record['agent_role'], 'context_summarizer')
        self.assertEqual(record['status'], 'succeeded')

    def test_summarization_cannot_bypass_model_call_budget(self):
        task = self.task()
        self.platform.store.mutate('task', task['id'], lambda row: {**row, 'budget': {**row['budget'], 'max_model_calls': 1},
            'usage': {**row['usage'], 'model_calls': 1}})
        model = Mock()
        engine = ModelEngine(self.settings, self.platform.gateway)
        with patch.object(type(self.settings), 'model', return_value=model), self.assertRaises(PlatformError) as error:
            engine._summarize_context(task, '历史数据')
        self.assertEqual(error.exception.code, 'model_budget')
        model.bind.return_value.invoke.assert_not_called()

    def test_non_archive_artifacts_are_not_exposed_by_readback(self):
        task = self.task()
        artifact = self.platform.store.artifact(task, 'internal-report.txt', 'not a tool archive')
        with self.assertRaises(PlatformError) as error:
            self.platform.gateway.invoke(self.actor, task['id'], 'read_tool_result', {'artifact_id': artifact['id']})
        self.assertEqual(error.exception.code, 'artifact_denied')

    def test_mcp_archives_without_citations_still_bind_to_original_connection(self):
        task = self.task()
        artifact = self.platform.gateway.archive_tool_result(task, 'mcp__project_files__find_files',
            {'pattern': '*.py', 'path': '.'}, '{"files": ["calculator.py"]}')
        self.settings.mcp_servers['project_files'].timeout_seconds += 1
        with self.assertRaises(PlatformError) as error:
            self.platform.gateway.invoke(self.actor, task['id'], 'read_tool_result', {'artifact_id': artifact['id']})
        self.assertEqual(error.exception.code, 'mcp_binding_changed')

    def test_context_profile_has_separate_audit_hash_without_changing_approval_scope(self):
        before = self.settings.versions()
        self.settings.context_management.summary_trigger_tokens += 1000
        after = self.settings.versions()
        self.assertEqual(before['config_hash'], after['config_hash'])
        self.assertNotEqual(before['context'], after['context'])


class PairBoundaryTests(unittest.TestCase):
    def test_parallel_calls_are_not_split_and_incomplete_group_is_preserved(self):
        calls = [{'id': 'a', 'name': 'read_file', 'args': {}}, {'id': 'b', 'name': 'read_file', 'args': {}}]
        pair = [AIMessage(content='', tool_calls=calls), ToolMessage(content='a', tool_call_id='a'), ToolMessage(content='b', tool_call_id='b')]
        self.assertEqual(safe_cutoff([*pair, HumanMessage(content='最新指令')], 2), 0)
        self.assertEqual(safe_cutoff([*pair, AIMessage(content='done'), AIMessage(content='tail')], 2), 3)
        self.assertEqual(safe_cutoff(pair[:2], 1), 0)

    def test_latest_user_instruction_is_not_in_summary_prefix(self):
        messages = [AIMessage(content='old'), HumanMessage(content='不要改数据库'), AIMessage(content='latest'), AIMessage(content='tail')]
        self.assertEqual(safe_cutoff(messages, 2), 1)


if __name__ == '__main__':
    unittest.main()
