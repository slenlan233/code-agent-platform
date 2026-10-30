import unittest
from code_agent_platform.engine import DemoEngine
from code_agent_platform.models import Mode,PlatformError,ReviewDraft,ReviewIssue,ApprovalDecision
import test_platform as fixtures


class AICheckTests(unittest.TestCase):
    setUp=fixtures.PlatformTests.setUp
    task=fixtures.PlatformTests.task
    approve=fixtures.PlatformTests.approve
    completed_write=fixtures.PlatformTests.completed_write

    def blocking_engine(self,always=False):
        gateway=self.platform.gateway
        class Check(DemoEngine):
            checks=0
            repair_context=None
            def review(engine,actor,task,context):
                engine.checks+=1
                if always or engine.checks==1:
                    evidence=gateway.invoke(actor,task['id'],'read_file',{'path':'tests/test_calculator.py'})
                    return ReviewDraft(summary='Fixture requires a repair.',issues=[ReviewIssue(severity='blocking',title='Fixture block',explanation='Repair before final approval.',path='tests/test_calculator.py',line=1,source_ids=[evidence['source_id']])])
                return ReviewDraft(summary='Repair accepted by AI check.',issues=[])
            def patch(engine,actor,task,context):
                if context.get('review_failures'):engine.repair_context=context
                return super().patch(actor,task,context)
        return Check()

    def test_blocking_check_repairs_retests_and_only_then_requests_human_confirmation(self):
        engine=self.blocking_engine()
        self.platform.engine_override=engine
        task=self.task(Mode.WRITE)
        self.platform.run(self.actor,task['id'])
        result=self.approve(task['id'])
        self.assertEqual(result['status'],'awaiting_diff')
        self.assertEqual(engine.checks,2)
        self.assertEqual(result['ai_check']['status'],'passed')
        self.assertIn('previous_patch',engine.repair_context)
        self.assertIn('review_failures',engine.repair_context)
        self.assertEqual(len(self.platform.store.list('sandbox_run',task_id=task['id'])),2)
        self.assertEqual(len(self.platform.store.list('review_run',task_id=task['id'])),2)
        self.assertEqual([a['kind'] for a in self.platform.store.list('approval',task_id=task['id'])].count('diff'),1)

    def test_repeated_blocking_check_stops_without_final_approval_or_apply(self):
        engine=self.blocking_engine(True)
        self.platform.engine_override=engine
        task=self.task(Mode.WRITE)
        self.platform.run(self.actor,task['id'])
        result=self.approve(task['id'])
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['error']['code'],'ai_check_blocked')
        self.assertEqual(engine.checks,3)
        self.assertFalse(any(a['kind']=='diff' for a in self.platform.store.list('approval',task_id=task['id'])))
        with self.assertRaises(PlatformError):self.platform.export_patch(self.actor,task['id'])
        with self.assertRaises(PlatformError):self.platform.apply_patch(self.actor,task['id'])

    def test_tampered_check_cannot_be_human_approved_or_exported(self):
        task=self.task(Mode.WRITE)
        self.platform.run(self.actor,task['id'])
        result=self.approve(task['id'])
        self.platform._set(task['id'],ai_check={**result['ai_check'],'patch_hash':'stale'})
        with self.assertRaises(PlatformError) as blocked:
            self.platform.decide(self.actor,result['approval_id'],ApprovalDecision(decision='approved'))
        self.assertEqual(blocked.exception.code,'ai_check_required')

    def test_final_response_is_stored_and_reused_in_conversation(self):
        task=self.completed_write()
        self.assertEqual(task['summary']['final_response_source'],'demo')
        messages=self.platform.conversations.detail(self.actor,task['session_id'])['messages']
        self.assertEqual(next(m for m in messages if m['kind']=='complete')['content'],task['summary']['final_response']['message'])


if __name__=='__main__':unittest.main()
