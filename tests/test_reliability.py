"""Causal memory, real cancellation boundaries and optional RAG degradation."""
import asyncio
import json
import subprocess
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from code_agent_platform.cancellation import CancellableChatOpenAI
from code_agent_platform.models import LOCAL_ACTOR, PlatformError, TaskInput
from code_agent_platform.runtime import DockerRunner
import test_platform as fixtures


class ReliabilityTests(unittest.TestCase):
    setUp = fixtures.PlatformTests.setUp

    def turn(self, prompt='解释代码', session=None):
        return self.platform.create_task(LOCAL_ACTOR, TaskInput(project_id=self.project['id'],
            session_id=session, user_input=prompt, engine='demo'))

    def history(self, count=10):
        session = self.platform.conversations.create(LOCAL_ACTOR, self.project['id'])
        tasks = []
        for i in range(count):
            task = self.turn('使用 MySQL 永久保存，不要使用内存代替。' if i == 0 else '第 '+str(i+1)+' 轮', session['id'])
            tasks.append(self.platform._set(task['id'], status='completed'))
        return tasks

    def test_retry_old_round_never_sees_later_rounds_and_snapshot_is_stable(self):
        tasks = self.history()
        old = tasks[2]
        context = self.platform.conversations.context(old)
        self.assertEqual(context['prior_task_ids'], [t['id'] for t in tasks[:2]])
        self.platform._set(tasks[1]['id'], answer={'answer': 'later changed result'})
        self.assertEqual(context, self.platform.conversations.context(old))
        self.assertNotIn('later changed result', json.dumps(context))

    def test_legacy_round_uses_time_boundary_not_current_session_summary(self):
        tasks = self.history(3)
        for i, task in enumerate(tasks):
            task.pop('history_task_ids')
            task['created_at'] = f'2026-01-01T00:00:0{i}+00:00'
            self.platform.store.put('task', task)
        context = self.platform.conversations.context(tasks[1])
        self.assertEqual(context['prior_task_ids'], [tasks[0]['id']])

    def test_full_retrieval_never_injects_future_session_summary(self):
        tasks = self.history(3)
        task = tasks[0]
        self.platform.store.mutate('session', task['session_id'], lambda row: {**row, 'summary': {'future': 'FUTURE_ONLY_SENTINEL'}})
        state = self.platform._context({'task_id': task['id']})
        context = self.platform._read_context(state)
        self.assertNotIn('FUTURE_ONLY_SENTINEL', json.dumps(context))

    def test_old_user_constraint_has_current_task_evidence_but_future_citation_is_denied(self):
        from code_agent_platform.models import digest
        tasks = self.history()
        state = self.platform._context({'task_id': tasks[-1]['id']})
        context = self.platform._read_context(state)
        old = next(e for e in context['evidence'] if e.get('metadata', {}).get('request_task_id') == tasks[0]['id'])
        self.platform._validate_sources(tasks[-1], [old['id']])
        forged = self.platform.store.put('evidence', {'source_type': 'request', 'task_id': tasks[0]['id'],
            'project_id': self.project['id'], 'content_hash': digest(tasks[-1]['user_input'].encode()),
            'metadata': {'request_task_id': tasks[-1]['id']}})
        with self.assertRaises(PlatformError):
            self.platform._validate_sources(tasks[0], [forged['id']])

    def test_history_archive_allows_inert_old_reference_but_not_unpinned_archive(self):
        tasks = self.history(2)
        source = self.platform.store.put('evidence', {'task_id': tasks[0]['id'], 'project_id': self.project['id'], 'source_type': 'code'})
        self.platform._set(tasks[0]['id'], answer={'answer': '历史回答引用 '+source['id']})
        context = self.platform.conversations.context(tasks[-1])
        artifact_id = context['memory']['history_archive']['artifact_id']
        result = self.platform.gateway.invoke(LOCAL_ACTOR, tasks[-1]['id'], 'read_tool_result', {'artifact_id': artifact_id})
        self.assertTrue(result['historical_quotes_only'])
        self.assertFalse(result.get('evidence'))
        forged = self.platform.gateway.archive_context_material(tasks[-1], 'not pinned', {'kind': 'conversation_history'})
        with self.assertRaises(PlatformError):
            self.platform.gateway.invoke(LOCAL_ACTOR, tasks[-1]['id'], 'read_tool_result', {'artifact_id': forged['id']})

    def test_restart_stopping_task_reaps_only_owned_labelled_resources(self):
        task = self.turn()
        self.platform._set(task['id'], status='cancelling', cancel_requested=True, cancelled_node='SANDBOX_VERIFY')
        replies = iter([
            SimpleNamespace(returncode=0, stdout=b'container-1'),
            SimpleNamespace(returncode=0, stdout=json.dumps([{'Id': 'container-1', 'Config': {'Labels': {
                'com.ai-agent.owner': 'task-runtime', 'com.ai-agent.task': task['id']}}}]).encode()),
            SimpleNamespace(returncode=0, stdout=b''),
            SimpleNamespace(returncode=0, stdout=b'')])
        self.platform.runner = DockerRunner(self.settings)
        with patch('code_agent_platform.runtime.subprocess.run', side_effect=lambda *a, **k: next(replies)) as cli:
            result = self.platform.finish_cancellation(task['id'])
        self.assertEqual(result['status'], 'cancelled')
        self.assertTrue(result['cancellation_cleanup']['verified'])
        self.assertEqual(cli.call_args_list[2].args[0], ['docker', 'rm', '-f', 'container-1'])

    def test_cleanup_does_not_remove_foreign_resources(self):
        task = self.turn()
        replies = iter([SimpleNamespace(returncode=0, stdout=b'foreign'),
            SimpleNamespace(returncode=0, stdout=json.dumps([{'Id': 'foreign', 'Config': {'Labels': {
                'com.ai-agent.owner': 'other-service', 'com.ai-agent.task': task['id']}}}]).encode())])
        with patch('code_agent_platform.runtime.subprocess.run', side_effect=lambda *a, **k: next(replies)) as cli:
            result = DockerRunner(self.settings).cleanup_task(task['id'])
        self.assertFalse(result['verified'])
        self.assertEqual(cli.call_count, 2)

    def test_mysql_cancel_propagates_after_owned_resource_cleanup(self):
        from code_agent_platform.database_runtime import verify_with_mysql
        task = self.turn()
        runner = DockerRunner(self.settings)
        with patch.object(runner, 'health', return_value={'available': True}), \
             patch('code_agent_platform.database_runtime.MySQLTestEnvironment') as factory:
            env = factory.return_value
            env.start.side_effect = PlatformError('task_cancelled', 'Stopped', 409)
            with self.assertRaises(PlatformError) as error:
                verify_with_mysql(runner, task, self.project, {'path': str(self.root)})
            self.assertEqual(error.exception.code, 'task_cancelled')
            env.close.assert_called_once()

    def test_long_chat_keeps_old_constraints_and_task_scoped_full_archive(self):
        tasks = self.history()
        context = self.platform.conversations.context(tasks[-1])
        self.assertEqual(len(context['prior_task_ids']), 4)
        self.assertTrue(any('MySQL' in item['quote'] for item in context['memory']['user_constraints']))
        artifact_id = context['memory']['history_archive']['artifact_id']
        text = self.platform.gateway.invoke(LOCAL_ACTOR, tasks[-1]['id'], 'read_tool_result', {'artifact_id': artifact_id})
        self.assertIn(tasks[0]['id'], text['content'])
        other = self.turn()
        with self.assertRaises(PlatformError) as error:
            self.platform.gateway.invoke(LOCAL_ACTOR, other['id'], 'read_tool_result', {'artifact_id': artifact_id})
        self.assertEqual(error.exception.code, 'artifact_denied')

    def test_cancel_queued_task_does_not_call_engine(self):
        task = self.turn()
        self.platform.cancel(LOCAL_ACTOR, task['id'])
        self.assertEqual(self.platform.run(LOCAL_ACTOR, task['id'])['status'], 'cancelled')
        self.assertFalse(self.platform.store.list('model_call', task_id=task['id']))

    def test_cancel_interrupts_provider_coroutine_before_any_tool_or_reply(self):
        started, cleaned = threading.Event(), threading.Event()
        async def slow_provider(model, *args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        task = self.platform.create_task(LOCAL_ACTOR, TaskInput(project_id=self.project['id'], engine='model', user_input='解释代码'))
        results, errors = [], []
        def run():
            try:
                results.append(self.platform.run(LOCAL_ACTOR, task['id']))
            except Exception as error:
                errors.append(error)
        with patch.object(CancellableChatOpenAI, '_agenerate', slow_provider):
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(started.wait(10), errors)
                self.assertEqual(self.platform.cancel(LOCAL_ACTOR, task['id'])['status'], 'cancelling')
                with self.assertRaises(PlatformError) as error:
                    self.turn(session=task['session_id'])
                self.assertEqual(error.exception.code, 'conversation_busy')
                worker.join(10)
                self.assertFalse(worker.is_alive())
                self.assertTrue(cleaned.is_set())
            finally:
                self.platform.cancel(LOCAL_ACTOR, task['id'])
                worker.join(10)
        self.assertFalse(errors)
        current = self.platform.store.get('task', task['id'])
        self.assertEqual(current['status'], 'cancelled')
        self.assertEqual(current['usage']['interrupted_calls'], 1)
        self.assertFalse(current.get('answer'))
        call = self.platform.store.list('model_call', task_id=task['id'])[0]
        self.assertEqual((call['status'], call['usage_source']), ('cancelled', 'interrupted_unknown'))
        self.assertEqual(self.platform._set(task['id'], status='completed')['status'], 'cancelled')

    def test_cancelling_container_removes_it_and_stops_cli_process(self):
        task = self.turn()
        checks = []
        def check(_):
            checks.append(True)
            if len(checks) > 1:
                raise PlatformError('task_cancelled', 'Stopped', 409)
        runner = DockerRunner(self.settings, check_cancelled=check)
        process = SimpleNamespace(returncode=None)
        process.poll = lambda: process.returncode
        process.kill = lambda: setattr(process, 'returncode', -9)
        process.wait = lambda **kwargs: process.returncode
        with patch.object(runner, 'health', return_value={'available': True}), \
             patch('code_agent_platform.runtime.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=b'sha256:fixture')) as cli, \
             patch('code_agent_platform.runtime.subprocess.Popen', return_value=process):
            with self.assertRaises(PlatformError) as error:
                runner._verify_container(task, self.project, {'path': str(self.root), 'container_name': 'owned-fixture'})
            self.assertEqual(error.exception.code, 'task_cancelled')
            self.assertIn(['docker', 'rm', '-f', 'owned-fixture'], [c.args[0] for c in cli.call_args_list])
            self.assertEqual(process.returncode, -9)

    def rag_fixture(self, *, required=False):
        self.settings.mcp_servers['bailian'].enabled = True
        self.settings.mcp_servers['bailian'].required = required
        self.platform.update_project(LOCAL_ACTOR, self.project['id'], mcp_servers=['project_files', 'bailian'])

    def run_rag_error(self, code, status=400):
        task = self.turn('根据 Java 开发规范解释代码')
        original = self.platform.gateway.invoke
        def invoke(actor, task_id, name, arguments):
            if name == 'mcp__bailian__query_rag':
                raise PlatformError(code, 'fixture cloud failure', status)
            return original(actor, task_id, name, arguments)
        with patch.object(self.platform.mcp, 'prepare', return_value=[{'server': 'bailian', 'status': 'connected'}]), \
             patch.object(self.platform.gateway, 'invoke', side_effect=invoke):
            return self.platform.run(LOCAL_ACTOR, task['id'])

    def test_optional_query_outage_degrades_with_visible_warning(self):
        self.rag_fixture()
        result = self.run_rag_error('mcp_call_failed')
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(result['knowledge_warnings'])
        messages = self.platform.store.list('message', task_id=result['id'])
        self.assertTrue(any(m['kind'] == 'notice' for m in messages))

    def test_required_source_outage_and_permission_denial_never_degrade(self):
        self.rag_fixture(required=True)
        with self.assertRaises(PlatformError):
            self.run_rag_error('mcp_call_failed')
        self.settings.mcp_servers['bailian'].required = False
        with self.assertRaises(PlatformError) as error:
            self.run_rag_error('policy_denied', 403)
        self.assertEqual(error.exception.status, 403)

    def test_startup_question_skips_optional_remote_prefetch(self):
        self.rag_fixture()
        task = self.turn('怎么运行')
        with patch.object(self.platform.mcp, 'prepare') as connect:
            self.assertEqual(self.platform.run(LOCAL_ACTOR, task['id'])['status'], 'completed')
            connect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
