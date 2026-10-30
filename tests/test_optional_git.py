import os,subprocess,time,unittest
from pathlib import Path
from unittest.mock import patch

from code_agent_platform.engine import DemoEngine
from code_agent_platform.git_support import git_environment,repository_info
from code_agent_platform.models import Budget,Mode,PlatformError
import test_platform as fixtures


class OptionalGitTests(unittest.TestCase):
    setUp=fixtures.PlatformTests.setUp
    task=fixtures.PlatformTests.task
    approve=fixtures.PlatformTests.approve

    def command(self,root,*args):
        return subprocess.run(['git','-C',str(root),*args],capture_output=True,check=True,env=git_environment()).stdout.decode().strip()

    def test_empty_folder_git_queries_are_informative_and_audited_without_initializing_git(self):
        root=Path(self.project['root']);task=self.task()
        for name in ('git_status','git_diff'):
            result=self.platform.gateway.invoke(self.actor,task['id'],name,{})
            self.assertFalse(result['available'])
            self.assertEqual(result['state'],'not_repository')
            self.assertEqual(self.platform.store.list('tool_call',task_id=task['id'])[-1]['status'],'succeeded')
        self.assertFalse((root/'.git').exists())

    def test_agent_can_call_git_then_finish_plan_test_review_and_apply_with_file_hashes(self):
        gateway=self.platform.gateway
        class QueryGit(DemoEngine):
            def plan(engine,actor,task,context):
                engine.git=gateway.invoke(actor,task['id'],'git_status',{})
                return super().plan(actor,task,context)
        engine=QueryGit();self.platform.engine_override=engine
        task=self.task(Mode.WRITE)
        self.assertEqual(self.platform.run(self.actor,task['id'])['status'],'awaiting_plan')
        plan=self.platform.store.get('plan',self.platform.store.get('task',task['id'])['plan_id'])
        self.assertIsNone(plan['repo_revision'])
        self.assertFalse(engine.git['available'])
        self.assertEqual(self.approve(task['id'])['status'],'awaiting_diff')
        self.assertEqual(self.platform.store.list('sandbox_run',task_id=task['id'])[-1]['workspace_kind'],'snapshot')
        self.assertEqual(self.approve(task['id'])['status'],'completed')
        self.assertEqual(self.platform.apply_patch(self.actor,task['id'])['status'],'applied')
        self.assertFalse((Path(self.project['root'])/'.git').exists())

    def test_unborn_repository_and_committed_repository_are_distinguished(self):
        root=Path(self.project['root']);self.command(root,'init')
        self.assertEqual(repository_info(self.project)['state'],'no_commits')
        result=self.platform.gateway.invoke(self.actor,self.task()['id'],'git_status',{})
        self.assertIn('calculator.py',result['status'])
        self.command(root,'add','calculator.py')
        self.command(root,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-m','Initial fixture')
        self.assertEqual(repository_info(self.project)['state'],'ready')
        task=self.task(Mode.WRITE);self.platform.run(self.actor,task['id'])
        self.assertEqual(self.approve(task['id'])['verification']['workspace_kind'],'git_worktree')

    def test_parent_repository_is_not_adopted_or_cloned_for_selected_child(self):
        root=Path(self.project['root']);self.command(self.root,'init')
        private=self.root/'outside.txt';private.write_text('outside selected folder')
        self.command(self.root,'add','outside.txt')
        self.command(self.root,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-m','Parent fixture')
        self.assertEqual(repository_info(self.project)['state'],'outside_project')
        task=self.task();workspace=self.platform.workspace.prepare(task,self.project,[])
        self.addCleanup(lambda:self.platform.workspace.cleanup(task['id']))
        self.assertEqual(workspace['kind'],'snapshot')
        self.assertFalse((Path(workspace['path'])/'outside.txt').exists())
        self.assertFalse((self.platform.workspace.directory(task['id'])/'repository.git').exists())

    def test_inherited_git_directory_does_not_redirect_the_project(self):
        foreign=self.root/'other';foreign.mkdir();self.command(foreign,'init')
        with patch.dict(os.environ,{'GIT_DIR':str(foreign/'.git'),'GIT_WORK_TREE':str(foreign)}):
            self.assertEqual(repository_info(self.project)['state'],'not_repository')

    def test_git_binary_unavailable_is_optional_but_explicit_commit_review_requires_git(self):
        with patch('code_agent_platform.git_support.subprocess.run',side_effect=FileNotFoundError):
            self.assertEqual(self.platform.gateway.invoke(self.actor,self.task()['id'],'git_status',{})['state'],'unavailable')
        task=self.task(Mode.REVIEW)
        with self.assertRaises(PlatformError) as missing:self.platform.run(self.actor,task['id'])
        self.assertEqual(missing.exception.code,'git_baseline_required')
        with self.assertRaises(PlatformError):self.platform.gateway.invoke(self.actor,self.task()['id'],'git_diff',{'base_ref':'HEAD'})

    def failed_plan(self):
        class Failing(DemoEngine):
            def plan(engine,*args):raise PlatformError('fixture_retry','Retry fixture')
        self.platform.engine_override=Failing()
        task=self.task(Mode.WRITE,budget=Budget(max_seconds=60))
        with self.assertRaises(PlatformError):self.platform.run(self.actor,task['id'])
        return task

    def test_resume_excludes_human_waiting_time_and_preserves_usage_counters(self):
        task=self.failed_plan();now=time.time()
        before=self.platform.store.get('task',task['id'])['usage']
        self.platform._set(task['id'],started_epoch=now-1005,failure_waiting_since=now-1000)
        self.platform.engine_override=DemoEngine()
        result=self.platform.run(self.actor,task['id'],resume=True)
        self.assertEqual(result['status'],'awaiting_plan')
        self.assertGreaterEqual(result['failure_wait_seconds'],1000)
        self.assertEqual(result['usage'],before)

    def test_resume_does_not_reset_already_spent_execution_budget(self):
        task=self.failed_plan();now=time.time()
        self.platform._set(task['id'],started_epoch=now-1100,failure_waiting_since=now-1000)
        self.platform.engine_override=DemoEngine()
        with self.assertRaises(PlatformError) as exceeded:self.platform.run(self.actor,task['id'],resume=True)
        self.assertEqual(exceeded.exception.code,'time_budget')


if __name__=='__main__':unittest.main()
