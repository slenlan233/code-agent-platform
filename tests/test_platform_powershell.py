"""Command approval and DeepSeek wire-contract regressions, no paid calls."""
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from code_agent_platform.config import Settings
from code_agent_platform.deepseek_model import DeepSeekModel
from code_agent_platform.engine import DemoEngine
from code_agent_platform.models import LOCAL_ACTOR, Mode, PlatformError, PowerShellArguments
from code_agent_platform.powershell import PowerShellRunner
import test_platform as fixtures


SCRIPT = "Get-Content -LiteralPath calculator.py; Write-Output '中文'"


class CommandEngine(DemoEngine):
    def plan(self, actor, task, context):
        plan = super().plan(actor, task, context)
        plan.powershell_commands = [PowerShellArguments(script=SCRIPT, timeout_seconds=15)]
        return plan


class CommandTests(unittest.TestCase):
    setUp = fixtures.PlatformTests.setUp
    task = fixtures.PlatformTests.task
    approve = fixtures.PlatformTests.approve

    def prepare(self):
        self.platform.engine_override = CommandEngine()
        task = self.task(Mode.WRITE)
        self.platform.run(LOCAL_ACTOR, task["id"])
        return task

    def test_command_does_not_execute_before_plan_approval(self):
        with patch.object(self.platform.powershell_runner, "run") as runner:
            task = self.prepare()
            runner.assert_not_called()
            with self.assertRaises(PlatformError):
                self.platform.gateway.invoke(LOCAL_ACTOR, task["id"], "run_powershell", {"script": SCRIPT, "timeout_seconds": 15})
            runner.assert_not_called()

    def test_approved_command_filters_sources_audits_and_reuses_result(self):
        root = Path(self.project["root"])
        (root/"docs/private.md").write_text("private-example", encoding="utf-8")
        (root/"docs/token.md").write_text("api_key=" + "sk-" + "f"*32, encoding="utf-8")
        self.platform.update_project(LOCAL_ACTOR, self.project["id"], policy_rules=[{"rule_id":"private", "effect":"deny", "tools":["read_file"], "paths":["docs/private.md"]}])
        task = self.prepare()
        def execute(task, project, workspace, **kwargs):
            snapshot = Path(workspace["path"])
            self.assertTrue((snapshot/"calculator.py").exists())
            self.assertFalse((snapshot/"docs/private.md").exists())
            self.assertFalse((snapshot/"docs/token.md").exists())
            return {"status":"passed", "exit_code":0, "stdout":"中文", "stderr":"", "backend":"fixture", "kind":"powershell"}
        with patch.object(self.platform.powershell_runner,"run",side_effect=execute) as runner:
            second = self.approve(task["id"])
            self.assertEqual(second["status"], "awaiting_diff")
            result = self.platform.gateway.invoke(LOCAL_ACTOR, task["id"], "run_powershell", {"script":SCRIPT, "timeout_seconds":15})
            self.assertTrue(result["reused"])
            self.assertEqual(result["stdout"], "中文")
            self.assertEqual(runner.call_count,1)
        self.assertEqual(len(self.platform.detail(LOCAL_ACTOR,task["id"])["command_runs"]),1)
        self.assertFalse(self.platform.command_workspace.directory(task["id"]).exists())

    def test_unapproved_script_or_timeout_cannot_execute(self):
        task = self.prepare()
        with patch.object(self.platform.powershell_runner,"run",return_value={"status":"passed","exit_code":0,"stdout":"","stderr":""}):
            self.approve(task["id"])
        for args in ({"script":"Get-Location","timeout_seconds":15},{"script":SCRIPT,"timeout_seconds":16}):
            with self.assertRaises(PlatformError) as error:
                self.platform.gateway.invoke(LOCAL_ACTOR,task["id"],"run_powershell",args)
            self.assertEqual(error.exception.code,"command_not_approved")

    def test_readonly_mode_cannot_execute_powershell(self):
        task = self.task()
        self.assertNotIn("run_powershell",[tool.name for tool in self.platform.gateway.langchain_tools(LOCAL_ACTOR,task["id"])])
        with self.assertRaises(PlatformError) as error:
            self.platform.gateway.invoke(LOCAL_ACTOR,task["id"],"run_powershell",{"script":"Get-Location"})
        self.assertEqual(error.exception.code,"mode_denied")

    def test_long_request_still_reaches_planning_or_answer(self):
        task=self.platform.create_task(LOCAL_ACTOR,fixtures.TaskInput(project_id=self.project["id"],engine="demo",user_input="请解释这个项目，"*100))
        result=self.platform.run(LOCAL_ACTOR,task["id"])
        self.assertEqual(result["status"],"completed")

    def test_enterprise_command_deny_prevents_plan_approval(self):
        from code_agent_platform.policy import PolicyRule
        self.settings.policy_rules=[PolicyRule(rule_id="no-commands",effect="deny",tools=["run_powershell"])]
        with self.assertRaises(PlatformError) as error:
            self.prepare()
        self.assertEqual(error.exception.code,"policy_denied")

    def test_command_mount_is_readonly_and_has_no_key_or_host_environment(self):
        runner=PowerShellRunner(self.settings)
        project={**self.project,"sandbox_image":self.settings.powershell_image,"test_command":["pwsh"]}
        command=runner.command(project,{"path":"C:/fixture"},"fixture")
        self.assertIn("type=bind,source=C:/fixture,target=/workspace,readonly",command)
        self.assertIn("--network=none",command)
        self.assertIn("--cap-drop=ALL",command)
        self.assertNotIn("AI_AGENT_API_KEY"," ".join(command))


class DeepSeekContractTests(unittest.TestCase):
    def test_thinking_survives_tool_turn_and_prior_assistant_turn(self):
        model=DeepSeekModel(model="deepseek-flash",api_key="fixture-key",base_url="https://api.deepseek.com",extra_body={"thinking":{"type":"enabled"}})
        messages=[HumanMessage(content="read"),AIMessage(content="",additional_kwargs={"reasoning_content":"fixture-reasoning"},tool_calls=[{"id":"call-1","name":"read_file","args":{"path":"a.py"}}]),ToolMessage(content="fixture",tool_call_id="call-1"),AIMessage(content="done",additional_kwargs={"reasoning_content":"fixture-end"})]
        payload=model._get_request_payload(messages,tools=[{"type":"function","function":{"name":"read_file","parameters":{"type":"object"}}}])
        self.assertEqual(payload["messages"][1]["reasoning_content"],"fixture-reasoning")
        self.assertEqual(payload["messages"][3]["reasoning_content"],"fixture-end")
        forced=model._get_request_payload(messages,tool_choice="required")
        self.assertEqual(forced["tool_choice"],"auto")
        disabled=model._get_request_payload(messages,tool_choice="required",extra_body={"thinking":{"type":"disabled"}})
        self.assertEqual(disabled["tool_choice"],"required")

    def test_settings_selects_deepseek_without_exposing_key(self):
        settings=Settings(data_dir=Path(".temp/fixture"),allowed_roots=[Path.cwd()],model_url="https://api.deepseek.com",allow_remote_model=True)
        with patch.dict("os.environ",{"AI_AGENT_API_KEY":"fixture-key"}):
            model=settings.model()
        self.assertIsInstance(model,DeepSeekModel)
        self.assertEqual(model.extra_body,{"thinking":{"type":"enabled"}})
        self.assertNotIn("fixture-key",str(settings.versions()))


if __name__=="__main__":
    unittest.main()
