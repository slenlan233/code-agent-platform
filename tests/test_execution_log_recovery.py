import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from code_agent_platform.engine import DemoEngine
from code_agent_platform.models import Mode, PlatformError
import test_platform as fixtures


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


class ExecutionLogTests(unittest.TestCase):
    setUp = fixtures.PlatformTests.setUp
    task = fixtures.PlatformTests.task
    approve = fixtures.PlatformTests.approve

    def report(self, task=None):
        task = task or self.task(Mode.WRITE)
        stdout = self.platform.store.artifact(task, 'stdout.log', 'header\n'*10000+'IMPORTANT_FAILURE_AT_END\n')
        stderr = self.platform.store.artifact(task, 'stderr.log', 'test stderr')
        patch = self.platform.store.put('patch', {'task_id': task['id'], 'project_id': task['project_id'], 'hash': 'proposal', 'files': []})
        run = self.platform.store.put('sandbox_run', {'task_id': task['id'], 'project_id': task['project_id'], 'patch_id': patch['id'],
            'status': 'failed', 'stdout_id': stdout['id'], 'stderr_id': stderr['id']})
        task = self.platform._set(task['id'], patch_id=patch['id'], verification=run)
        return task, stdout, stderr

    def read(self, task, artifact, **kwargs):
        return self.platform.gateway.invoke(self.actor, task['id'], 'read_tool_result', {'artifact_id': artifact['id'], **kwargs})

    def test_current_large_test_log_is_readable_without_marking_every_artifact_public(self):
        task, stdout, stderr = self.report()
        result = self.read(task, stdout, offset=60000, length=16000)
        self.assertIn('IMPORTANT_FAILURE_AT_END', result['content'])
        self.assertEqual(result['execution_log']['patch_id'], task['patch_id'])
        self.assertEqual(self.read(task, stderr)['content'], 'test stderr')
        internal = self.platform.store.artifact(task, 'stdout.log', 'not a registered execution receipt')
        with self.assertRaises(PlatformError) as error:
            self.read(task, internal)
        self.assertEqual(error.exception.code, 'artifact_denied')

    def test_other_task_and_previous_proposal_logs_are_denied(self):
        task, stdout, _ = self.report()
        other = self.task(Mode.WRITE)
        with self.assertRaises(PlatformError):
            self.read(other, stdout)
        other_patch = self.platform.store.put('patch', {'task_id': task['id'], 'project_id': task['project_id'], 'hash': 'new-proposal', 'files': []})
        self.platform._set(task['id'], patch_id=other_patch['id'])
        with self.assertRaises(PlatformError):
            self.read(task, stdout)

    def test_log_policy_and_hash_checks_still_apply(self):
        task, stdout, _ = self.report()
        self.platform.update_project(self.actor, self.project['id'], policy_rules=[{
            'rule_id': 'deny-diagnostics', 'effect': 'deny', 'tools': ['read_execution_log']}])
        with self.assertRaises(PlatformError):
            self.read(task, stdout)
        self.platform.update_project(self.actor, self.project['id'], policy_rules=[])
        path = self.platform.store.artifact_dir/stdout['relative_path']
        path.write_text('tampered', encoding='utf-8')
        with self.assertRaises(PlatformError) as error:
            self.read(task, stdout)
        self.assertEqual(error.exception.code, 'artifact_changed')

    def test_command_logs_require_current_approved_plan_binding(self):
        task = self.task(Mode.WRITE)
        plan = self.platform.store.put('plan', {'task_id': task['id'], 'project_id': task['project_id'], 'hash': 'current-plan', 'goal': 'fixture', 'files': [], 'steps': []})
        task = self.platform._set(task['id'], plan_id=plan['id'])
        log = self.platform.store.artifact(task, 'powershell-stdout.log', 'command output')
        run = self.platform.store.put('command_run', {'task_id': task['id'], 'project_id': task['project_id'],
            'plan_hash': plan['hash'], 'stdout_id': log['id']})
        self.assertEqual(self.read(task, log)['content'], 'command output')
        self.platform.store.mutate('command_run', run['id'], lambda row: {**row, 'plan_hash': 'old-plan'})
        with self.assertRaises(PlatformError):
            self.read(task, log)

    def test_agent_repair_receives_failure_tail_and_can_page_full_current_log(self):
        platform = self.platform
        real_runner = platform.runner
        class Runner:
            attempts = 0
            def verify(runner, task, project, workspace):
                runner.attempts += 1
                if runner.attempts == 1:
                    return {'status': 'failed', 'exit_code': 1, 'stdout': 'startup noise\n'*6000+'IMPORTANT_FAILURE_AT_END',
                            'stderr': '', 'backend': 'test_fixture', 'test_count': 3}
                return real_runner.verify(task, project, workspace)
        runner = Runner()
        platform.runner = runner
        class Repair(DemoEngine):
            saw_tail = False
            def patch(engine, actor, task, context):
                if context.get('test_failures'):
                    failure = context['test_failures']
                    assert 'IMPORTANT_FAILURE_AT_END' in failure['stdout']
                    output = platform.gateway.invoke(actor, task['id'], 'read_tool_result', {
                        'artifact_id': failure['stdout_id'], 'offset': failure['stdout_characters']-100, 'length': 100})
                    assert 'IMPORTANT_FAILURE_AT_END' in output['content']
                    engine.saw_tail = True
                return super().patch(actor, task, context)
        engine = Repair()
        platform.engine_override = engine
        task = self.task(Mode.WRITE)
        platform.run(self.actor, task['id'])
        result = self.approve(task['id'])
        self.assertEqual(result['status'], 'awaiting_diff')
        self.assertTrue(engine.saw_tail)
        self.assertEqual(runner.attempts, 2)
        self.assertFalse((Path(self.project['root'])/'tests/test_calculator.py').exists())


class ApprovalWaitAccountingTests(unittest.TestCase):
    setUp = fixtures.PlatformTests.setUp
    task = fixtures.PlatformTests.task

    def test_reusing_approval_id_does_not_skip_a_new_pending_window(self):
        task = self.task(Mode.WRITE)
        now = time.time()
        plan = self.platform.store.put('plan', {'task_id': task['id'], 'project_id': task['project_id'], 'hash': 'scope', 'goal': 'fixture', 'files': [], 'steps': []})
        approval = self.platform.store.put('approval', {'task_id': task['id'], 'project_id': task['project_id'], 'kind': 'plan',
            'subject_id': plan['id'], 'subject_hash': 'scope', 'status': 'pending', 'waiting_since': now-3600, 'expires_epoch': now-1})
        task = self.platform._set(task['id'], plan_id=plan['id'], counted_approvals=[approval['id']])
        self.platform._renew_approval_record(task, approval)
        current = self.platform.store.get('task', task['id'])
        self.assertGreaterEqual(current['approval_wait_seconds'], 3600)
        self.assertEqual(current['budget']['max_seconds'], task['budget']['max_seconds'])

    def test_legacy_wait_is_reconciled_once_and_failure_overlap_is_not_double_counted(self):
        task = self.task(Mode.WRITE)
        now = time.time()
        task = self.platform._set(task['id'], started_epoch=now-5000, approval_wait_seconds=300)
        approval = self.platform.store.put('approval', {'task_id': task['id'], 'project_id': task['project_id'],
            'kind': 'plan', 'status': 'approved', 'waiting_since': now-1000})
        old = {**approval, 'status': 'pending', 'waiting_since': now-4000, 'decided_at': iso(now-4500)}
        self.platform.store.mutate('approval', approval['id'], lambda row: {**row, 'decision_history': [old]})
        for kind, epoch in [('TaskFailed', now-2500), ('TaskResumed', now-2000)]:
            self.platform.store.put('event', {'task_id': task['id'], 'project_id': task['project_id'], 'kind': kind, 'created_at': iso(epoch)})
        repaired = self.platform._reconcile_legacy_approval_wait(task)
        self.assertAlmostEqual(repaired['approval_wait_seconds'], 2800, delta=1)
        again = self.platform._reconcile_legacy_approval_wait(repaired)
        self.assertEqual(again['approval_wait_seconds'], repaired['approval_wait_seconds'])
        self.assertEqual(again['usage'], task['usage'])
        self.assertEqual(again['budget'], task['budget'])


if __name__ == '__main__':
    unittest.main()
