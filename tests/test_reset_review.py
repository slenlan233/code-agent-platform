import difflib
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from code_agent_platform.engine import DemoEngine, ModelEngine
from code_agent_platform.intent import requests_project_reset
from code_agent_platform.models import FileChange, Mode, PatchDraft, PlanDraft, PlatformError, ReviewDraft, ReviewIssue
from code_agent_platform.reset_verification import check_reset_plan, reset_paths
from code_agent_platform.review_scope import diff_lines, location_ranges, review_error
import test_platform as fixtures
import test_structured_retry as structured_fixtures


class ReviewLocationTests(unittest.TestCase):
    def test_mixed_deletions_and_additions_do_not_leak_between_files(self):
        text = ''.join(difflib.unified_diff(['old1\n', 'old2\n'], [], fromfile='a/deleted.txt', tofile='/dev/null'))
        text += ''.join(difflib.unified_diff(['kept\n', 'old\n'], ['kept\n', 'new\n'], fromfile='a/changed.txt', tofile='b/changed.txt'))
        text += ''.join(difflib.unified_diff([], ['added\n'], fromfile='/dev/null', tofile='b/added.txt'))
        locations = diff_lines(text)
        self.assertEqual(locations, {'deleted.txt': {'old': {1, 2}, 'new': set()},
                                    'changed.txt': {'old': {2}, 'new': {2}}, 'added.txt': {'old': set(), 'new': {1}}})
        self.assertEqual(location_ranges(locations)['deleted.txt']['old'], [[1, 2]])

    def test_content_resembling_headers_and_no_newline_markers_are_content(self):
        locations = diff_lines('--- a/a.txt\n+++ b/a.txt\n@@ -1,2 +1,2 @@\n--- literal\n-removed\n\\ No newline at end of file\n+++ literal\n+added\n\\ No newline at end of file\n')
        self.assertEqual(locations, {'a.txt': {'old': {1, 2}, 'new': {1, 2}}})

    def test_only_changed_lines_and_valid_sides_can_be_reported(self):
        locations = {'a.py': {'old': {2}, 'new': {3}}}
        issue = ReviewIssue(severity='blocking', path='a.py', line=2, side='old', title='Removed guard', explanation='The deleted guard is needed.', source_ids=['source'])
        self.assertIsNone(review_error(ReviewDraft(summary='Check', issues=[issue]), locations))
        for invalid in [issue.model_copy(update={'side': 'new'}), issue.model_copy(update={'path': 'outside.py'}), issue.model_copy(update={'line': 1})]:
            self.assertIsNotNone(review_error(ReviewDraft(summary='Check', issues=[invalid]), locations))

    def test_model_automatically_corrects_deleted_line_side_without_dropping_blocker(self):
        wrong = ReviewDraft(summary='Deletion defect', issues=[ReviewIssue(severity='blocking', path='a.txt', line=1, title='Removed guard', explanation='The guard is required.', source_ids=['real'])])
        fixed = wrong.model_copy(update={'issues': [wrong.issues[0].model_copy(update={'side': 'old'})]})
        engine, agent, task = structured_fixtures.StructuredRetryTests.run_engine(self, [{'structured_response': wrong}, {'structured_response': fixed}])
        context = {'diff': '--- a/a.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-guard\n', 'evidence': [{'source_id': 'real'}]}
        context['review_locations'] = location_ranges(diff_lines(context['diff']))
        with patch('code_agent_platform.engine.build_agent', return_value=agent):
            result = engine.run(None, task, context, ReviewDraft, 'Review')
        self.assertEqual(result.issues[0].side, 'old')
        self.assertEqual(result.issues[0].severity, 'blocking')
        self.assertEqual(agent.invoke.call_count, 2)
        self.assertEqual(engine.gateway.store.event.call_args.args[2]['reason'], 'review_contract')

    def test_outside_diff_cannot_pass_after_repeated_invalid_model_results(self):
        result = ReviewDraft(summary='Bad', issues=[ReviewIssue(severity='warning', path='outside.txt', line=1, title='Invalid location', explanation='Outside diff', source_ids=['real'])])
        engine, agent, task = structured_fixtures.StructuredRetryTests.run_engine(self, [{'structured_response': result}]*3)
        with patch('code_agent_platform.engine.build_agent', return_value=agent), self.assertRaises(PlatformError) as error:
            engine.run(None, task, {'evidence': [{'source_id': 'real'}]}, ReviewDraft, 'Review')
        self.assertEqual(error.exception.code, 'model_evidence_invalid')
        self.assertEqual(agent.invoke.call_count, 3)


class ProjectResetTests(unittest.TestCase):
    setUp = fixtures.PlatformTests.setUp
    approve = fixtures.PlatformTests.approve
    task = fixtures.PlatformTests.task

    def reset_engine(self, *, keep_scaffold=False):
        gateway = self.platform.gateway
        paths = sorted(reset_paths(self.settings, self.project))
        class Reset(DemoEngine):
            def plan(engine, actor, task, context):
                return PlanDraft(goal='Clear the whole project', files=paths, steps=['Delete approved files'],
                                 source_ids=[e['source_id'] for e in context['evidence']], test_plan='Check file manifest', rollback_plan='Restore journal')
            def patch(engine, actor, task, context):
                return PatchDraft(changes=[FileChange(path=p, content='# empty scaffold\n' if keep_scaffold and p == paths[0] else None) for p in paths], explanation='Delete source files')
            def review(engine, actor, task, context):
                engine.original = gateway.invoke(actor, task['id'], 'read_file', {'path': paths[0], 'view': 'original'})
                return ReviewDraft(summary='Deletion is within the approved reset scope.', issues=[ReviewIssue(
                    severity='suggestion', path=paths[0], side='old', line=1, title='Deleted source',
                    explanation='The source is removed as requested.', source_ids=[engine.original['source_id']])])
        return Reset()

    def start(self, engine):
        self.platform.engine_override = engine
        task = self.task(Mode.WRITE)
        self.platform.store.mutate('task', task['id'], lambda row: {**row, 'user_input': 'Clear this project so I can start again'})
        self.platform.run(self.actor, task['id'])
        return task

    def test_reset_uses_manifest_without_docker_then_applies_and_can_roll_back(self):
        root = Path(self.project['root'])
        (root/'.env').write_text('APP_DB_PASSWORD=fixture-only', encoding='utf-8')
        original = {name: (root/name).read_bytes() for name in reset_paths(self.settings, self.project)}
        self.platform.runner.verify = Mock(side_effect=AssertionError('A reset must not execute project code'))
        engine = self.reset_engine()
        task = self.start(engine)
        proposed = self.approve(task['id'])
        self.assertEqual(proposed['status'], 'awaiting_diff')
        self.assertEqual(proposed['verification']['verification_kind'], 'project_reset')
        self.assertEqual(proposed['verification']['test_count'], 0)
        self.platform.runner.verify.assert_not_called()
        self.assertEqual(engine.original['source_type'], 'code')
        self.platform._validate_sources(proposed, [engine.original['source_id']])
        self.assertTrue(all((root/name).exists() for name in original))
        self.approve(task['id'])
        self.platform.apply_patch(self.actor, task['id'])
        self.assertFalse(any((root/name).exists() for name in original))
        self.assertEqual((root/'.env').read_text(), 'APP_DB_PASSWORD=fixture-only')
        self.platform.rollback_patch(self.actor, task['id'])
        self.assertEqual({name: (root/name).read_bytes() for name in original}, original)

    def test_restoring_empty_scaffold_cannot_satisfy_the_reset_verifier(self):
        self.platform.runner.verify = Mock(side_effect=AssertionError('Must not run a build'))
        task = self.start(self.reset_engine(keep_scaffold=True))
        result = self.approve(task['id'])
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['verification']['status'], 'failed')
        self.assertEqual(result['verification']['verification_kind'], 'project_reset')
        self.assertFalse(self.platform.store.list('review_run', task_id=task['id']))
        self.platform.runner.verify.assert_not_called()

    def test_a_partial_reset_or_new_unapproved_file_cannot_skip_project_tests(self):
        task = self.task(Mode.WRITE)
        task['user_input'] = 'Clear this project'
        names = sorted(reset_paths(self.settings, self.project))
        for plan in [{'files': names[:-1]}, {'files': names, 'requires_database': True}]:
            with self.assertRaises(PlatformError): check_reset_plan(self.settings, task, self.project, plan)
        (Path(self.project['root'])/'unapproved.txt').write_text('new')
        with self.assertRaises(PlatformError): check_reset_plan(self.settings, task, self.project, {'files': names})

    def test_normal_development_cannot_claim_reset_to_avoid_tests(self):
        task = self.task(Mode.WRITE)
        with self.assertRaises(PlatformError) as error:
            check_reset_plan(self.settings, task, self.project, {'files': sorted(reset_paths(self.settings, self.project))})
        self.assertEqual(error.exception.code, 'reset_intent_required')

    def test_deletion_without_explicit_reset_still_runs_project_tests(self):
        engine = self.reset_engine()
        self.platform.engine_override = engine
        self.platform.runner.verify = Mock(return_value={'status': 'passed', 'exit_code': 0,
            'backend': 'test_fixture', 'test_count': 0, 'stdout': '', 'stderr': ''})
        task = self.task(Mode.WRITE)
        self.platform.run(self.actor, task['id'])
        result = self.approve(task['id'])
        self.assertEqual(result['status'], 'awaiting_diff')
        self.assertEqual(result['verification']['backend'], 'test_fixture')
        self.assertNotIn('verification_kind', result['verification'])
        self.platform.runner.verify.assert_called_once()

    def test_reset_intent_requires_explicit_project_scope(self):
        for text in ['Clear this project', 'Reset the entire project', '\u5e2e\u6211\u628a\u8fd9\u4e2a\u9879\u76ee\u91cc\u7684\u6240\u6709\u5220\u6389\uff0c\u6211\u8981\u91cd\u65b0\u505a', '\u6e05\u7a7a\u5f53\u524d\u9879\u76ee']:
            self.assertTrue(requests_project_reset(text), text)
        for text in ['Delete all failed tests', 'Fix the project tests', 'Do not clear the project', '\u4e0d\u8981\u6e05\u7a7a\u9879\u76ee']:
            self.assertFalse(requests_project_reset(text), text)


if __name__ == '__main__':
    unittest.main()
