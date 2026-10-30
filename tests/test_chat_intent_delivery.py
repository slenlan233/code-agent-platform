import unittest
from pathlib import Path
from code_agent_platform.models import ApprovalDecision,Mode,PlatformError,TaskInput
from fastapi.testclient import TestClient
from code_agent_platform.api import create_app
import test_platform as fixtures


class ChatIntentDeliveryTests(unittest.TestCase):
    setUp=fixtures.PlatformTests.setUp
    task=fixtures.PlatformTests.task
    approve=fixtures.PlatformTests.approve
    completed_write=fixtures.PlatformTests.completed_write

    def test_auto_question_returns_answer_without_plan_or_patch(self):
        task=self.platform.create_task(self.actor,TaskInput(project_id=self.project['id'],user_input='怎么运行',mode=Mode.AUTO,engine='demo'))
        result=self.platform.run(self.actor,task['id'])
        self.assertEqual(result['mode'],'readonly')
        self.assertEqual(result['status'],'completed')
        self.assertTrue(result['answer'])
        self.assertFalse(self.platform.store.list('plan',task_id=task['id']))
        self.assertFalse(self.platform.store.list('patch',task_id=task['id']))

    def test_auto_retry_regenerates_an_invalid_answer_instead_of_replaying_it(self):
        from code_agent_platform.engine import DemoEngine
        from code_agent_platform.models import AnswerDraft,Finding,TurnDraft
        class Retry(DemoEngine):
            calls=0
            def respond(engine,actor,task,context):
                engine.calls+=1
                if engine.calls==1:return TurnDraft(action='answer',answer=AnswerDraft(answer='invalid source',findings=[Finding(claim='fixture',source_ids=['missing_source'])]))
                return TurnDraft(action='answer',answer=engine.answer(actor,task,context))
        engine=Retry();self.platform.engine_override=engine
        task=self.platform.create_task(self.actor,TaskInput(project_id=self.project['id'],user_input='怎么运行',mode=Mode.AUTO,engine='demo'))
        with self.assertRaises(PlatformError):self.platform.run(self.actor,task['id'])
        result=self.platform.run(self.actor,task['id'],resume=True)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(engine.calls,2)

    def test_auto_reading_keeps_existing_readonly_policy_restrictions(self):
        self.platform.update_project(self.actor,self.project['id'],policy_rules=[{'rule_id':'ro-private','effect':'deny','tools':['read_file'],'paths':['calculator.py'],'modes':['readonly']}])
        task=self.task(Mode.AUTO)
        with self.assertRaises(PlatformError) as denied:self.platform.gateway.invoke(self.actor,task['id'],'read_file',{'path':'calculator.py'})
        self.assertEqual(denied.exception.code,'policy_denied')

    def test_auto_modification_still_requires_both_approvals(self):
        task=self.platform.create_task(self.actor,TaskInput(project_id=self.project['id'],user_input='新增测试',mode=Mode.AUTO,engine='demo'))
        result=self.platform.run(self.actor,task['id'])
        self.assertEqual(result['mode'],'approval_write')
        self.assertEqual(result['status'],'awaiting_plan')
        result=self.approve(task['id'])
        self.assertEqual(result['status'],'awaiting_diff')
        self.assertFalse((Path(self.project['root'])/'tests/test_calculator.py').exists())

    def test_deferred_implementation_is_not_ended_as_plan_only_when_model_misclassifies(self):
        from code_agent_platform.engine import DemoEngine
        from code_agent_platform.models import TurnDraft
        class PlanMisclassification(DemoEngine):
            def respond(engine,actor,task,context):
                return TurnDraft(action='plan',plan=engine.plan(actor,task,context))
        self.platform.engine_override=PlanMisclassification()
        task=self.platform.create_task(self.actor,TaskInput(project_id=self.project['id'],user_input='从零开发 Todo 项目。先分析并给出实施计划，等待我确认后执行。',mode=Mode.AUTO,engine='demo'))
        pending=self.platform.run(self.actor,task['id'])
        self.assertEqual(pending['status'],'awaiting_plan')
        self.assertEqual(pending['mode'],'approval_write')
        self.assertFalse(self.platform.store.list('patch',task_id=task['id']))
        detail=self.platform.conversations.detail(self.actor,task['session_id'])
        self.assertTrue(detail['tasks'][0]['plan']['steps'])
        self.assertEqual(detail['tasks'][0]['plan']['files'],['tests/test_calculator.py'])
        headers={'Authorization':'Bearer '+self.platform.store.local_token()}
        with TestClient(create_app(platform=self.platform)) as client:
            response=client.post('/api/approvals/'+pending['approval_id'],json={'decision':'approved'},headers=headers)
            self.assertEqual(response.status_code,202)
        continued=self.platform.store.get('task',task['id'])
        self.assertEqual(continued['status'],'awaiting_diff')
        self.assertTrue(continued['patch_id'])
        self.assertFalse((Path(self.project['root'])/'tests/test_calculator.py').exists())

    def test_explicit_plan_mode_still_stops_after_planning(self):
        task=self.platform.create_task(self.actor,TaskInput(project_id=self.project['id'],user_input='给出计划，确认后实施。',mode=Mode.PLAN,engine='demo'))
        result=self.platform.run(self.actor,task['id'])
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['mode'],'plan_only')
        self.assertFalse(self.platform.store.list('approval',task_id=task['id']))
        self.assertTrue(self.platform.conversations.detail(self.actor,task['session_id'])['tasks'][0]['plan']['steps'])

    def test_deferred_execution_does_not_override_readonly_project(self):
        from code_agent_platform.engine import DemoEngine
        from code_agent_platform.models import TurnDraft
        class PlanMisclassification(DemoEngine):
            def respond(engine,actor,task,context):return TurnDraft(action='plan',plan=engine.plan(actor,task,context))
        self.platform.engine_override=PlanMisclassification()
        self.platform.update_project(self.actor,self.project['id'],allow_write=False)
        task=self.platform.create_task(self.actor,TaskInput(project_id=self.project['id'],user_input='给出计划，等我确认后执行。',mode=Mode.AUTO,engine='demo'))
        result=self.platform.run(self.actor,task['id'])
        self.assertEqual(result['mode'],'plan_only')
        self.assertFalse(self.platform.store.list('approval',task_id=task['id']))

    def test_only_design_request_does_not_offer_execution(self):
        from code_agent_platform.intent import requests_implementation_after_approval
        self.assertFalse(requests_implementation_after_approval('只讨论确认后执行的流程，暂不开发。'))
        self.assertTrue(requests_implementation_after_approval('Create a Todo project. After I approve the plan, implement it.'))

    def test_readonly_project_auto_never_elevates_itself_to_writing(self):
        self.platform.update_project(self.actor,self.project['id'],allow_write=False)
        task=self.platform.create_task(self.actor,TaskInput(project_id=self.project['id'],user_input='新增测试',mode=Mode.AUTO,engine='demo'))
        result=self.platform.run(self.actor,task['id'])
        self.assertEqual(result['mode'],'plan_only')
        self.assertEqual(result['status'],'completed')
        self.assertFalse(self.platform.store.list('patch',task_id=task['id']))

    def accepted_task(self):
        task=self.task(Mode.WRITE);self.platform.run(self.actor,task['id']);pending=self.approve(task['id'])
        self.platform.decide(self.actor,pending['approval_id'],ApprovalDecision(decision='approved',apply_to_project=True))
        return self.platform.run(self.actor,task['id'],resume=True)

    def test_explicit_accept_and_write_saves_once_without_second_approval(self):
        task=self.accepted_task()
        result=self.platform.deliver_approved_patch(self.actor,task['id'])
        self.assertTrue(result['summary']['applied_to_project'])
        self.assertEqual(result['delivery']['status'],'applied')
        self.assertTrue((Path(self.project['root'])/'tests/test_calculator.py').exists())
        self.assertTrue(self.platform.deliver_approved_patch(self.actor,task['id'])['summary']['applied_to_project'])
        self.assertEqual(sum(e['kind']=='PatchApplied' for e in self.platform.store.list('event',task_id=task['id'])),1)

    def test_legacy_acceptance_does_not_silently_write(self):
        task=self.completed_write()
        self.platform.deliver_approved_patch(self.actor,task['id'])
        self.assertFalse((Path(self.project['root'])/'tests/test_calculator.py').exists())

    def test_rolling_back_revokes_automatic_reapplication(self):
        task=self.accepted_task();self.platform.deliver_approved_patch(self.actor,task['id'])
        self.platform.rollback_patch(self.actor,task['id'])
        self.platform.deliver_approved_patch(self.actor,task['id'])
        self.assertFalse((Path(self.project['root'])/'tests/test_calculator.py').exists())

    def test_changed_baseline_preserves_user_edits_and_reports_write_failure(self):
        task=self.accepted_task();path=Path(self.project['root'])/'tests/test_calculator.py';path.parent.mkdir(exist_ok=True);path.write_text('user edit')
        result=self.platform.deliver_approved_patch(self.actor,task['id'])
        self.assertEqual(result['delivery']['status'],'failed')
        self.assertFalse(result['summary']['applied_to_project'])
        self.assertEqual(path.read_text(),'user edit')

    def test_delivery_pending_blocks_followup_until_files_saved(self):
        task=self.accepted_task()
        follow=TaskInput(project_id=self.project['id'],session_id=task['session_id'],user_input='继续提问',engine='demo')
        with self.assertRaises(PlatformError):self.platform.create_task(self.actor,follow)
        self.platform.deliver_approved_patch(self.actor,task['id'])
        self.platform.create_task(self.actor,follow)

    def test_plan_approval_cannot_be_used_to_request_writeback(self):
        task=self.task(Mode.WRITE);pending=self.platform.run(self.actor,task['id'])
        with self.assertRaises(PlatformError):self.platform.decide(self.actor,pending['approval_id'],ApprovalDecision(decision='approved',apply_to_project=True))

    def test_http_acceptance_runs_and_saves_without_another_apply_request(self):
        task=self.task(Mode.WRITE);self.platform.run(self.actor,task['id']);pending=self.approve(task['id'])
        headers={'Authorization':'Bearer '+self.platform.store.local_token()}
        with TestClient(create_app(platform=self.platform)) as client:
            response=client.post('/api/approvals/'+pending['approval_id'],json={'decision':'approved','apply_to_project':True},headers=headers)
            self.assertEqual(response.status_code,202)
        self.assertTrue((Path(self.project['root'])/'tests/test_calculator.py').exists())

    def test_server_restart_resumes_only_an_explicitly_requested_unfinished_save(self):
        task=self.accepted_task()
        with TestClient(create_app(platform=self.platform)):
            pass
        result=self.platform.store.get('task',task['id'])
        self.assertTrue(result['summary']['applied_to_project'])
        self.assertEqual(result['delivery']['status'],'applied')

    def test_old_chat_write_mode_is_permission_to_modify_instead_of_forcing_a_patch(self):
        headers={'Authorization':'Bearer '+self.platform.store.local_token()}
        with TestClient(create_app(platform=self.platform)) as client:
            sid=client.post('/api/sessions',json={'project_id':self.project['id']},headers=headers).json()['id']
            response=client.post('/api/sessions/'+sid+'/messages',json={'content':'怎么运行','mode':'approval_write','engine':'demo','client_request_id':'auto-mode-proof'},headers=headers)
            self.assertEqual(response.status_code,202)
            task_id=response.json()['id']
        result=self.platform.store.get('task',task_id)
        self.assertEqual(result['requested_mode'],'auto')
        self.assertEqual(result['mode'],'readonly')
        self.assertFalse(self.platform.store.list('patch',task_id=task_id))


if __name__=='__main__':unittest.main()
