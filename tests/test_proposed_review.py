import unittest
from pathlib import Path

from code_agent_platform.engine import DemoEngine
from code_agent_platform.models import Mode,PlatformError,ReviewDraft,ReviewIssue
import test_platform as fixtures


class ProposedReviewTests(unittest.TestCase):
    setUp=fixtures.PlatformTests.setUp
    task=fixtures.PlatformTests.task
    approve=fixtures.PlatformTests.approve

    def start_review(self, *, fail=False):
        gateway=self.platform.gateway
        class Reader(DemoEngine):
            def review(engine,actor,task,context):
                data=gateway.invoke(actor,task['id'],'read_file',{'path':'tests/test_calculator.py'})
                engine.source=data
                names=gateway.invoke(actor,task['id'],'list_files',{})['files']
                assert 'tests/test_calculator.py' in names
                results=gateway.invoke(actor,task['id'],'search_code',{'query':'test_positive'})['evidence']
                assert any(item['source_type']=='patch' for item in results)
                if fail:raise PlatformError('review_fixture','Interrupted review fixture')
                return ReviewDraft(issues=[ReviewIssue(severity='suggestion',path='tests/test_calculator.py',line=1,title='Fixture',explanation='Proposed file evidence',source_ids=[data['source_id']])],summary='Read the actual proposed file.')
        engine=Reader()
        self.platform.engine_override=engine
        task=self.task(Mode.WRITE)
        self.platform.run(self.actor,task['id'])
        try:
            result=self.approve(task['id'])
        except PlatformError:
            if not fail:raise
            result=self.platform.store.get('task',task['id'])
        return result,engine

    def test_review_reads_new_file_without_writing_original_and_sources_remain_valid(self):
        task,engine=self.start_review()
        self.assertEqual(task['status'],'awaiting_diff')
        self.assertEqual(engine.source['source_type'],'patch')
        self.assertFalse((Path(self.project['root'])/'tests/test_calculator.py').exists())
        self.platform._validate_sources(task,[engine.source['source_id']])
        self.assertFalse((self.settings.data_dir/'review-workspaces'/task['id']).exists())
        self.assertIsNone(task['review_patch_id'])

    def test_patch_evidence_rejects_tampered_patch_and_other_task(self):
        task,engine=self.start_review()
        with self.assertRaises(PlatformError):self.platform._validate_sources(self.task(),[engine.source['source_id']])
        patch=self.platform.store.get('patch',task['patch_id'])
        self.platform.store.put('patch',{**patch,'hash':'tampered'})
        with self.assertRaises(PlatformError) as error:self.platform._validate_sources(task,[engine.source['source_id']])
        self.assertEqual(error.exception.code,'source_changed')

    def test_failed_review_cleans_its_copy_and_resumes_without_stale_error(self):
        task,engine=self.start_review(fail=True)
        self.assertEqual(task['status'],'failed')
        self.assertFalse((self.settings.data_dir/'review-workspaces'/task['id']).exists())
        self.assertIsNone(task['review_patch_id'])
        self.platform.engine_override=DemoEngine()
        resumed=self.platform.run(self.actor,task['id'],resume=True)
        self.assertEqual(resumed['status'],'awaiting_diff')
        self.assertIsNone(resumed.get('error'))

    def repair_engine(self,*,fail=False):
        gateway=self.platform.gateway
        class Repair(DemoEngine):
            reviews=0
            source=None
            def review(engine,actor,task,context):
                engine.reviews+=1
                if engine.reviews>1:return ReviewDraft(summary='Passed repair review.',issues=[])
                evidence=gateway.invoke(actor,task['id'],'read_file',{'path':'tests/test_calculator.py'})
                return ReviewDraft(summary='Fixture needs repair.',issues=[ReviewIssue(severity='blocking',path='tests/test_calculator.py',line=1,title='Fixture',explanation='Repair fixture.',source_ids=[evidence['source_id']])])
            def patch(engine,actor,task,context):
                if context.get('previous_patch'):
                    engine.source=gateway.invoke(actor,task['id'],'read_file',{'path':'tests/test_calculator.py'})
                    names={tool.name for tool in gateway.langchain_tools(actor,task['id'])}
                    assert 'mcp__project_files__read_file' not in names
                    if fail:raise PlatformError('repair_fixture','Interrupted repair')
                return super().patch(actor,task,context)
        return Repair()

    def test_repair_reads_previous_new_file_before_it_is_written_to_original(self):
        engine=self.repair_engine();self.platform.engine_override=engine
        task=self.task(Mode.WRITE);self.platform.run(self.actor,task['id'])
        result=self.approve(task['id'])
        self.assertEqual(result['status'],'awaiting_diff')
        self.assertEqual(engine.source['source_type'],'patch')
        self.assertFalse((Path(self.project['root'])/'tests/test_calculator.py').exists())
        self.assertIsNone(result['review_patch_id'])
        self.assertFalse((self.settings.data_dir/'review-workspaces'/task['id']).exists())

    def test_failed_repair_always_cleans_its_proposed_file_view(self):
        engine=self.repair_engine(fail=True);self.platform.engine_override=engine
        task=self.task(Mode.WRITE);self.platform.run(self.actor,task['id'])
        with self.assertRaises(PlatformError):self.approve(task['id'])
        result=self.platform.store.get('task',task['id'])
        self.assertIsNone(result['review_patch_id'])
        self.assertFalse((self.settings.data_dir/'review-workspaces'/task['id']).exists())

    def test_model_file_lookup_can_recover_but_denied_paths_still_stop(self):
        task=self.task()
        tools={tool.name:tool for tool in self.platform.gateway.langchain_tools(self.actor,task['id'])}
        result=tools['read_file'].invoke({'path':'missing.py'})
        self.assertEqual(result['status'],'unavailable')
        self.assertEqual(result['error']['code'],'not_found')
        calls=self.platform.store.list('tool_call',task_id=task['id'])
        self.assertEqual(next(c for c in calls if c['name']=='read_file')['status'],'failed')
        with self.assertRaises(PlatformError):tools['read_file'].invoke({'path':'.env'})


if __name__=='__main__':unittest.main()
