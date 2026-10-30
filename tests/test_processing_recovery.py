import json
import time
import unittest
from unittest.mock import patch

from code_agent_platform.context_budget import compact_context, context_size
from code_agent_platform.engine import ModelEngine
from code_agent_platform.models import FileChange, Mode, PatchDraft, PlatformError
import test_platform as fixtures


class ProcessingRecoveryTests(unittest.TestCase):
    setUp = fixtures.PlatformTests.setUp
    task = fixtures.PlatformTests.task
    approve = fixtures.PlatformTests.approve

    def test_expired_execution_approval_reopens_same_plan_and_retains_generated_patch(self):
        from code_agent_platform.engine import DemoEngine
        platform = self.platform
        class ExpiringEngine(DemoEngine):
            expired = False
            def patch(engine, actor, task, context):
                proposal = super().patch(actor, task, context)
                if not engine.expired:
                    engine.expired = True
                    platform.store.mutate('approval', task['approval_id'], lambda row: {**row, 'expires_epoch': time.time()-1})
                return proposal
        self.platform.engine_override = ExpiringEngine()
        task = self.task(Mode.WRITE)
        self.platform.run(self.actor, task['id'])
        renewed = self.approve(task['id'])
        self.assertEqual(renewed['status'], 'awaiting_plan')
        self.assertTrue(renewed['patch_id'])
        self.assertIsNone(renewed['error'])
        approval = self.platform.store.get('approval', renewed['approval_id'])
        self.assertEqual(approval['status'], 'pending')
        self.assertEqual(approval['decision_history'][-1]['status'], 'approved')
        self.assertEqual(self.approve(task['id'])['status'], 'awaiting_diff')

    def patch_context(self, files):
        return {'approved_plan': {'files': files, 'hash': 'approved-scope'}}

    def proposal(self, context):
        return PatchDraft(changes=[FileChange(path=p, content='content '+p) for p in context['patch_batch']['files']], explanation='fixture')

    def test_truncated_batch_splits_without_leaving_approved_paths(self):
        task = self.task(Mode.WRITE)
        context = self.patch_context(['a.java', 'b.java', 'c.java', 'd.java'])
        engine = ModelEngine(self.settings, self.platform.gateway)
        batches = []
        def generate(actor, task, view, schema, instruction):
            batch = view['patch_batch']['files']
            batches.append(batch)
            if len(batch) == 3:
                raise PlatformError('model_output_truncated', 'fixture')
            result = self.proposal(view)
            return schema.model_validate(result.model_dump())
        with patch.object(engine, 'run', side_effect=generate):
            result = engine.patch(self.actor, task, context)
        self.assertEqual(batches, [['a.java', 'b.java', 'c.java'], ['a.java'], ['b.java', 'c.java'], ['d.java']])
        self.assertEqual([item.path for item in result.changes], context['approved_plan']['files'])
        self.assertFalse((self.root / 'a.java').exists())

    def test_retry_reuses_completed_batches_but_changed_generation_does_not(self):
        task = self.task(Mode.WRITE)
        context = self.patch_context(['a.java', 'b.java', 'c.java', 'd.java'])
        engine = ModelEngine(self.settings, self.platform.gateway)
        def first(actor, task, view, schema, instruction):
            if view['patch_batch']['files'] == ['d.java']:
                raise PlatformError('model_structure_missing', 'fixture')
            return self.proposal(view)
        with patch.object(engine, 'run', side_effect=first), self.assertRaises(PlatformError):
            engine.patch(self.actor, task, context)
        batches = []
        def finish(actor, task, view, schema, instruction):
            batches.append(view['patch_batch']['files'])
            return self.proposal(view)
        with patch.object(engine, 'run', side_effect=finish):
            result = engine.patch(self.actor, task, context)
        self.assertEqual(batches, [['d.java']])
        self.assertEqual(len(result.changes), 4)
        batches.clear()
        with patch.object(engine, 'run', side_effect=finish):
            engine.patch(self.actor, task, {**context, 'review_failures': {'summary': 'New repair'}})
        self.assertEqual(batches, [['a.java', 'b.java', 'c.java'], ['d.java']])

    def test_single_file_truncation_keeps_specific_failure(self):
        task = self.task(Mode.WRITE)
        engine = ModelEngine(self.settings, self.platform.gateway)
        with patch.object(engine, 'run', side_effect=PlatformError('model_output_truncated', 'fixture')), self.assertRaises(PlatformError) as error:
            engine.patch(self.actor, task, self.patch_context(['large.java']))
        self.assertEqual(error.exception.code, 'model_output_truncated')
        self.assertNotIn('patch_generation', self.platform.store.get('task', task['id']))

    def remote(self, *, legacy=False):
        self.settings.mcp_servers['bailian'].enabled = True
        project = self.platform.update_project(self.actor, self.project['id'], mcp_servers=['project_files', 'bailian'])
        task = self.task()
        item = {'path': 'bailian://space/index/document', 'text': '保留数据并新增迁移。', 'version': 'v1'}
        retrieval = {'tool': 'mcp__bailian__query_rag', 'arguments': {'query': '迁移约定'}}
        source = self.platform.gateway.remote_evidence(task, project, 'bailian', item, 'remote_document', retrieval=None if legacy else retrieval)
        self.platform.store.mutate('evidence', source['source_id'], lambda row: {**row, 'expires_epoch': time.time()-60})
        if legacy:
            self.platform.store.put('tool_call', {'task_id': task['id'], 'project_id': project['id'], 'name': retrieval['tool'],
                'input': retrieval['arguments'], 'output': {'evidence': [source]}, 'status': 'succeeded'})
        return task, project, item, source, retrieval

    def test_expired_remote_source_is_retrieved_and_identical_content_renews(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                task, project, item, source, retrieval = self.remote(legacy=legacy)
                def query(*args):
                    fresh = self.platform.gateway.remote_evidence(task, project, 'bailian', item, 'remote_document', retrieval=retrieval)
                    return {'evidence': [fresh]}
                with patch.object(self.platform.mcp, 'prepare'), patch.object(self.platform.gateway, 'invoke', side_effect=query) as invoke:
                    self.platform._validate_sources(task, [source['source_id']])
                invoke.assert_called_once_with(self.actor, task['id'], retrieval['tool'], retrieval['arguments'])
                self.assertGreater(self.platform.store.get('evidence', source['source_id'])['expires_epoch'], time.time())

    def test_changed_remote_content_cannot_reuse_old_approval_evidence(self):
        task, project, item, source, retrieval = self.remote()
        def query(*args):
            fresh = self.platform.gateway.remote_evidence(task, project, 'bailian', {**item, 'text': '已变更的规则'}, 'remote_document', retrieval=retrieval)
            return {'evidence': [fresh]}
        with patch.object(self.platform.mcp, 'prepare'), patch.object(self.platform.gateway, 'invoke', side_effect=query), self.assertRaises(PlatformError) as error:
            self.platform._validate_sources(task, [source['source_id']])
        self.assertEqual(error.exception.code, 'remote_source_changed')
        self.assertLess(self.platform.store.get('evidence', source['source_id'])['expires_epoch'], time.time())

    def test_changed_connection_is_not_silently_refreshed(self):
        task, project, item, source, retrieval = self.remote()
        self.settings.mcp_servers['bailian'].timeout_seconds += 1
        with patch.object(self.platform.gateway, 'invoke') as invoke, self.assertRaises(PlatformError) as error:
            self.platform._validate_sources(task, [source['source_id']])
        self.assertEqual(error.exception.code, 'source_changed')
        invoke.assert_not_called()


class ContextBudgetTests(unittest.TestCase):
    def archive(self, content, descriptor):
        self.saved.append((content, descriptor))
        return {'id': 'artifact-'+str(len(self.saved))}

    def test_compaction_preserves_scope_recent_dialogue_and_original_evidence(self):
        self.saved = []
        context = {
            'approved_plan': {'files': ['active.java'], 'steps': ['按原范围实现'], 'source_ids': ['source-one']},
            'patch_batch': {'files': ['active.java']},
            'conversation': {'recent_messages': [{'role': 'user', 'content': '三个功能都要'}],
                'previous_task_summary': {'applied_to_project': True, 'final_response': {'message': 'old '*20000}}},
            'evidence': [{'source_id': 'source-one', 'path': 'rules.md', 'snippet': '规范内容'*15000},
                {'source_id': 'source-active', 'path': 'active.java', 'snippet': '完整当前文件'}],
        }
        view, report = compact_context(context, limit=12000, archive=self.archive)
        self.assertLess(context_size(view), 12000)
        self.assertEqual(view['approved_plan'], context['approved_plan'])
        self.assertEqual(view['conversation']['recent_messages'], context['conversation']['recent_messages'])
        self.assertEqual(view['evidence'][1]['snippet'], '完整当前文件')
        self.assertEqual(len(context['evidence'][0]['snippet']), 60000)
        self.assertTrue(view['evidence'][0]['context_archive']['artifact_id'])
        self.assertIn(context['evidence'][0]['snippet'], [item[0] for item in self.saved])
        self.assertEqual(report['method'], 'recoverable_materials')

    def test_dependency_is_kept_when_unrelated_material_alone_can_release_space(self):
        self.saved = []
        controller = 'class TodoController { TodoService service; }'
        service = 'class TodoService {\n'+'// important implementation\n'*200+'public Todo find(int id) { return repository.find(id); }\n}'
        context = {'patch_batch': {'files': ['TodoController.java']},
            'project_rules': ['rules.md'], 'test_failures': {'stderr': 'must keep this failure'},
            'evidence': [{'path': 'TodoController.java', 'snippet': controller},
                {'path': 'TodoService.java', 'snippet': service},
                {'path': 'irrelevant.txt', 'snippet': 'unrelated\n'*5000},
                {'path': 'rules.md', 'snippet': 'all business rules stay'}]}
        view, report = compact_context(context, limit=10000, archive=self.archive)
        self.assertEqual(view['evidence'][1]['snippet'], service)
        self.assertEqual(view['test_failures'], context['test_failures'])
        self.assertEqual([item['path'] for item in report['materials']], ['irrelevant.txt'])

    def test_dependency_preview_retains_mid_file_signature_and_original_line_numbers(self):
        self.saved = []
        service = '// header\n'*220+'public TodoService findTodo(int id) { return null; }\n'+'// tail\n'*500
        context = {'patch_batch': {'files': ['TodoController.java']}, 'evidence': [
            {'path': 'TodoController.java', 'snippet': 'class TodoController { TodoService service; }'},
            {'path': 'TodoService.java', 'start_line': 101, 'snippet': service}]}
        view, _ = compact_context(context, limit=4500, archive=self.archive)
        self.assertIn('321: public TodoService findTodo', view['evidence'][1]['snippet'])
        self.assertEqual(self.saved[0][0], service)

    def test_no_archive_means_no_silent_loss_and_rules_or_requests_are_never_cut(self):
        for context in ({'evidence': [{'path': 'big.java', 'snippet': 'source'*5000}]},
                        {'evidence': [{'source_type': 'request', 'snippet': 'user constraint'*5000}]},
                        {'project_rules': ['rules.md'], 'evidence': [{'path': 'rules.md', 'snippet': 'rules'*5000}]}):
            self.saved = []
            with self.assertRaises(PlatformError):
                compact_context(context, limit=2000, archive=self.archive if 'project_rules' in context else None)

    def test_oversized_required_scope_is_not_silently_cut(self):
        context = {'approved_plan': {'steps': ['不可裁剪'*10000]}}
        with self.assertRaises(PlatformError) as error:
            compact_context(context, limit=1000)
        self.assertEqual(error.exception.code, 'context_budget')


if __name__ == '__main__':
    unittest.main()
