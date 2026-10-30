import json
import tempfile
import unittest
from pathlib import Path

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from code_agent_platform.config import Settings
from code_agent_platform.evaluation import evaluate
from code_agent_platform.models import Budget, LOCAL_ACTOR, Mode, PlatformError, TaskInput
from code_agent_platform.service import Platform

from test_platform import FixtureRunner


class ReceiptModel(BaseChatModel):
    @property
    def _llm_type(self):
        return "offline-provider-receipt"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, **kwargs):
        context = json.loads(messages[-1].content)["context"]
        source = context["evidence"][0]["source_id"]
        message = AIMessage(content="", tool_calls=[{"name": "AnswerDraft", "id": "answer-1", "args": {"answer": "源码定义了加法函数。", "findings": [{"claim": "源码定义了加法函数。", "source_ids": [source], "kind": "fact"}], "unknowns": []}}], usage_metadata={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18})
        return ChatResult(generations=[ChatGeneration(message=message)])


class ModelSettings(Settings):
    def model(self):
        return ReceiptModel()


class UnreportedModel(ReceiptModel):
    def _generate(self, messages, **kwargs):
        result = super()._generate(messages, **kwargs)
        result.generations[0].message.usage_metadata = None
        return result


class UnreportedSettings(Settings):
    def model(self):
        return UnreportedModel()


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.settings = Settings(data_dir=root / "data", allowed_roots=[root])
        self.platform = Platform(self.settings, runner=FixtureRunner())
        self.addCleanup(lambda: self.platform.close())
        self.project = self.platform.create_demo(LOCAL_ACTOR)

    def test_fixed_workflow_and_negative_dataset_is_not_model_accuracy(self):
        result = evaluate(self.platform, LOCAL_ACTOR)
        self.assertEqual(result["total"], 40)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["passed"], 40)
        self.assertIsNone(result["model_accuracy"])
        self.assertIsNone(result["human_time_savings"])
        self.assertEqual(result["scope"], "workflow_and_policy")

    def test_token_cap_remains_after_summary_compaction(self):
        task = self.platform.create_task(LOCAL_ACTOR, TaskInput(project_id=self.project["id"], user_input="固定预算测试", engine="demo", budget=Budget(max_tokens=100)))
        self.platform.store.mutate("task", task["id"], lambda row: {**row, "usage": {**row["usage"], "input_tokens": 100}, "summary": {"compact": True}})
        with self.assertRaises(PlatformError) as result:
            self.platform.gateway.invoke(LOCAL_ACTOR, task["id"], "read_file", {"path": "calculator.py"})
        self.assertEqual(result.exception.code, "token_budget")

    def test_model_usage_and_cost_are_measured_per_call(self):
        self.platform.close()
        settings = ModelSettings.model_validate({**self.settings.model_dump(), "input_price_per_million": 1.0, "output_price_per_million": 2.0})
        self.platform = Platform(settings, runner=FixtureRunner())
        task = self.platform.create_task(LOCAL_ACTOR, TaskInput(project_id=self.project["id"], user_input="解释代码", engine="model"))
        result = self.platform.run(LOCAL_ACTOR, task["id"])
        self.assertEqual(result["usage"]["input_tokens"], 11)
        self.assertEqual(result["usage"]["output_tokens"], 7)
        self.assertAlmostEqual(result["usage"]["cost"], 0.000025)
        calls = self.platform.store.list("model_call", task_id=task["id"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["usage_source"], "provider")

    def test_missing_usage_is_unknown_cost_and_stops_further_execution(self):
        self.platform.close()
        settings = UnreportedSettings.model_validate({**self.settings.model_dump(), "input_price_per_million": 1.0, "output_price_per_million": 2.0})
        self.platform = Platform(settings, runner=FixtureRunner())
        task = self.platform.create_task(LOCAL_ACTOR, TaskInput(project_id=self.project["id"], user_input="解释代码", engine="model"))
        with self.assertRaises(PlatformError) as error:
            self.platform.run(LOCAL_ACTOR, task["id"])
        self.assertEqual(error.exception.code, "usage_unreported")
        result = self.platform.store.get("task", task["id"])
        self.assertIsNone(result["usage"]["cost"])
        self.assertEqual(result["usage"]["unreported_calls"], 1)
        self.assertEqual(result["status"], "failed")

    def test_explicit_missing_dataset_is_an_error(self):
        with self.assertRaises(PlatformError) as error:
            evaluate(self.platform, LOCAL_ACTOR, Path(self.directory.name) / "missing.json")
        self.assertEqual(error.exception.code, "dataset_missing")

    def test_model_evaluation_uses_model_and_reports_checks_and_repeated_samples(self):
        from code_agent_platform.evaluation import evaluate_model
        self.platform.close()
        self.platform = Platform(ModelSettings.model_validate(self.settings.model_dump()), runner=FixtureRunner())
        dataset = Path(self.directory.name)/'model-cases.json'
        dataset.write_text(json.dumps({'version': 'fixture-v1', 'cases': [
            {'id': 'answer', 'fixture': 'python', 'mode': 'readonly', 'prompt': '解释 add', 'expected_terms': ['加法']},
            {'id': 'wrong', 'fixture': 'java', 'mode': 'readonly', 'prompt': '解释代码', 'expected_terms': ['IMPOSSIBLE_EXPECTATION']}]}), encoding='utf-8')
        result = evaluate_model(self.platform, LOCAL_ACTOR, dataset, repeats=2)
        self.assertEqual((result['total'], result['passed'], result['failed']), (4, 2, 2))
        self.assertEqual(result['engine'], 'model')
        self.assertEqual(result['input_tokens'], 44)
        self.assertIsNone(result['model_accuracy'])
        self.assertEqual({r['fixture'] for r in result['cases']}, {'java', 'python'})
        self.assertTrue(all(r['checks']['no_project_write'] for r in result['cases']))

    def test_model_evaluation_rejects_automatic_write_approval(self):
        from code_agent_platform.evaluation import evaluate_model
        dataset = Path(self.directory.name)/'unsafe-cases.json'
        dataset.write_text(json.dumps({'version': 'fixture', 'cases': [
            {'id': 'write', 'fixture': 'python', 'mode': 'approval_write', 'prompt': '修改代码'}]}), encoding='utf-8')
        with self.assertRaises(PlatformError) as error:
            evaluate_model(self.platform, LOCAL_ACTOR, dataset)
        self.assertEqual(error.exception.code, 'evaluation_input')


if __name__ == "__main__":
    unittest.main()
