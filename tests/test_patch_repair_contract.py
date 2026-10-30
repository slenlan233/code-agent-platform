import json,unittest
from pydantic import ValidationError
from code_agent_platform.engine import DemoEngine,scoped_patch_type
from code_agent_platform.models import FileChange,Mode,PatchDraft,ReviewDraft,ReviewIssue
import test_platform as fixtures


class PatchRepairContractTests(unittest.TestCase):
    setUp=fixtures.PlatformTests.setUp
    task=fixtures.PlatformTests.task
    approve=fixtures.PlatformTests.approve

    def test_model_schema_limits_paths_and_rejects_duplicate_entries(self):
        schema=scoped_patch_type(['a.py','b.py'])
        self.assertEqual(schema.model_validate({'changes':[{'path':'a.py','content':'a'}],'explanation':'valid'}).changes[0].path,'a.py')
        for paths in (['outside.py'],['a.py','a.py']):
            with self.assertRaises(ValidationError):schema.model_validate({'changes':[{'path':p,'content':'x'} for p in paths],'explanation':'invalid'})

    def test_partial_repair_keeps_previously_generated_new_files(self):
        gateway=self.platform.gateway
        class Partial(DemoEngine):
            checks=0
            def plan(engine,*args):
                plan=super().plan(*args)
                return plan.model_copy(update={'files':[*plan.files,'notes.txt']})
            def patch(engine,actor,task,context):
                if context.get('previous_patch'):
                    return PatchDraft(changes=[FileChange(path='notes.txt',content='corrected note\n')],explanation='Repair only this file.')
                proposal=super().patch(actor,task,context)
                return proposal.model_copy(update={'changes':[*proposal.changes,FileChange(path='notes.txt',content='original note\n')]})
            def review(engine,actor,task,context):
                engine.checks+=1
                if engine.checks>1:return ReviewDraft(summary='Fixed.',issues=[])
                source=gateway.invoke(actor,task['id'],'read_file',{'path':'notes.txt'})
                return ReviewDraft(summary='Repair note.',issues=[ReviewIssue(severity='blocking',path='notes.txt',line=1,title='Fixture',explanation='Repair this note.',source_ids=[source['source_id']])])
        self.platform.engine_override=Partial()
        task=self.task(Mode.WRITE);self.platform.run(self.actor,task['id'])
        result=self.approve(task['id'])
        self.assertEqual(result['status'],'awaiting_diff')
        patch=self.platform.store.get('patch',result['patch_id'])
        payload=json.loads(self.platform.store.artifact_content(patch['artifact_id']))
        changes={c['path']:c['content'] for c in payload['changes']}
        self.assertIn('tests/test_calculator.py',changes)
        self.assertEqual(changes['notes.txt'],'corrected note\n')


if __name__=='__main__':unittest.main()
