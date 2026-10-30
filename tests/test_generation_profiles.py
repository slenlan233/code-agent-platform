import json
import unittest
from unittest.mock import patch

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field, ValidationError

from code_agent_platform.engine import ModelEngine, scoped_patch_type
from code_agent_platform.generation import GenerationSettings, patch_batches
from code_agent_platform.models import AnswerDraft, FileChange, Mode, PatchDraft, PlatformError
import test_platform as fixtures


class CaptureModel(BaseChatModel):
    coding: bool = False
    calls: list[dict] = Field(default_factory=list)

    @property
    def _llm_type(self):
        return 'offline-output-profile-check'

    def bind_tools(self, tools, **kwargs):
        return self.bind(**kwargs)

    def _generate(self, messages, **kwargs):
        self.calls.append(kwargs)
        name = 'PatchDraft' if self.coding else 'AnswerDraft'
        args = {'changes': [{'path': 'new.java', 'content': 'class New {}'}], 'explanation': 'fixture'} if self.coding else {
            'answer': 'fixture answer', 'findings': [], 'unknowns': ['This fixture does not establish business facts.']}
        message = AIMessage(content='', tool_calls=[{'id': 'result', 'name': name, 'args': args}],
            usage_metadata={'input_tokens': 12, 'output_tokens': 8, 'total_tokens': 20},
            response_metadata={'finish_reason': 'tool_calls'})
        return ChatResult(generations=[ChatGeneration(message=message)])


class GenerationProfileTests(unittest.TestCase):
    setUp = fixtures.PlatformTests.setUp
    task = fixtures.PlatformTests.task

    def enable(self):
        before = self.settings.versions()
        self.settings.generation = GenerationSettings()
        after = self.settings.versions()
        self.assertEqual(before['config_hash'], after['config_hash'])
        self.assertNotEqual(before['generation'], after['generation'])

    def test_real_langchain_request_and_audit_use_stage_limits_and_context_reserve(self):
        self.enable()
        for coding in (False, True):
            task = self.task(Mode.WRITE if coding else Mode.READONLY)
            model = CaptureModel(coding=coding)
            engine = ModelEngine(self.settings, self.platform.gateway)
            with patch.object(type(self.settings), 'model', return_value=model):
                result = engine.run(self.actor, task, {}, scoped_patch_type(['new.java']) if coding else AnswerDraft, 'Return fixture result')
            limit, timeout = (131072, 600) if coding else (32768, 180)
            self.assertEqual(model.calls[0]['max_tokens'], limit)
            self.assertEqual(model.calls[0]['timeout'], timeout)
            record = self.platform.store.list('model_call', task_id=task['id'])[0]
            self.assertEqual(record['output_limit_tokens'], limit)
            self.assertEqual(record['request_timeout_seconds'], timeout)
            self.assertEqual(self.platform.store.get('task', task['id'])['context_usage']['reserved_output_tokens'], limit)
            self.assertFalse((self.root/'new.java').exists())

    def test_new_file_batches_use_larger_capacity_but_large_files_stay_separate(self):
        self.enable()
        paths = [f'File{i}.java' for i in range(15)]
        batches, _ = patch_batches(paths, {}, self.settings)
        self.assertEqual([len(batch) for batch in batches], [8, 7])
        context = {'previous_patch': {'changes': [{'path': paths[0], 'content': 'x'*200000}]}}
        batches, _ = patch_batches(paths, context, self.settings)
        self.assertEqual(batches[0], [paths[0]])
        self.assertEqual([path for batch in batches for path in batch], paths)

    def test_size_or_output_failure_splits_without_expanding_approved_scope(self):
        self.enable()
        for reason in ('model_output_truncated', 'context_budget', 'context_window_exceeded'):
            with self.subTest(reason=reason):
                self._check_split(reason)

    def test_larger_code_profile_does_not_keep_the_old_180k_material_barrier(self):
        self.enable()
        task = self.task(Mode.WRITE)
        model = CaptureModel(coding=True)
        context = {'patch_batch': {'files': ['new.java']}, 'evidence': [
            {'path': 'new.java', 'snippet': 'x'*200000}]}
        with patch.object(type(self.settings), 'model', return_value=model):
            result = ModelEngine(self.settings, self.platform.gateway).run(self.actor, task, context, scoped_patch_type(['new.java']), 'Return fixture result')
        self.assertEqual(result.changes[0].path, 'new.java')
        self.assertEqual(context['evidence'][0]['snippet'], 'x'*200000)

    def _check_split(self, reason):
        task = self.task(Mode.WRITE)
        engine = ModelEngine(self.settings, self.platform.gateway)
        paths = [f'File{i}.java' for i in range(9)]
        batches = []
        def generate(actor, task, context, schema, instruction):
            batch = context['patch_batch']['files']
            batches.append(batch)
            if len(batch) > 4:
                raise PlatformError(reason, 'fixture')
            return PatchDraft(changes=[FileChange(path=path, content='class Fixture {}') for path in batch], explanation='fixture')
        with patch.object(engine, 'run', side_effect=generate):
            result = engine.patch(self.actor, task, {'approved_plan': {'files': paths, 'hash': 'approved'}})
        self.assertEqual([len(batch) for batch in batches], [8, 4, 4, 1])
        self.assertEqual([change.path for change in result.changes], paths)

    def test_changing_output_profile_preserves_completed_batch_cache(self):
        self.enable()
        task = self.task(Mode.WRITE)
        engine = ModelEngine(self.settings, self.platform.gateway)
        paths = [f'File{i}.java' for i in range(12)]
        context = {'approved_plan': {'files': paths, 'hash': 'approved'}}
        def fail_last(actor, task, view, schema, instruction):
            if view['patch_batch']['files'][0] == paths[8]:
                raise PlatformError('model_structure_missing', 'fixture')
            return PatchDraft(changes=[FileChange(path=p, content='first '+p) for p in view['patch_batch']['files']], explanation='fixture')
        with patch.object(engine, 'run', side_effect=fail_last), self.assertRaises(PlatformError):
            engine.patch(self.actor, task, context)
        self.settings.generation.max_batch_files = 2
        batches = []
        def finish(actor, task, view, schema, instruction):
            batch = view['patch_batch']['files']
            batches.append(batch)
            return PatchDraft(changes=[FileChange(path=p, content='remaining '+p) for p in batch], explanation='fixture')
        with patch.object(engine, 'run', side_effect=finish):
            result = engine.patch(self.actor, task, context)
        self.assertEqual(batches, [paths[8:10], paths[10:]])
        self.assertTrue(all(change.content.startswith('first ') for change in result.changes[:8]))

    def test_config_validation_covers_provider_output_range(self):
        self.enable()
        self.assertEqual(GenerationSettings(code_output_tokens=393216).code_output_tokens, 393216)
        with self.assertRaises(ValidationError):
            GenerationSettings(code_output_tokens=393217)


if __name__ == '__main__':
    unittest.main()
