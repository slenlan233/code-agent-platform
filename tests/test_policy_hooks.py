import unittest
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from code_agent_platform.models import ApprovalDecision, LOCAL_ACTOR, Mode, PlatformError, TaskInput
from code_agent_platform.policy import PolicyRule
import test_platform as platform_fixtures


class GovernanceTests(unittest.TestCase):
    setUp = platform_fixtures.PlatformTests.setUp
    task = platform_fixtures.PlatformTests.task
    approve = platform_fixtures.PlatformTests.approve

    def test_enterprise_deny_wins_over_session_allow_and_dot_path(self):
        self.platform.settings.policy_rules = [PolicyRule(rule_id="protect-calculator", effect="deny", tools=["read_file"], paths=["calculator.py"])]
        task = self.task(policy_rules=[{"rule_id":"try-allow","effect":"allow","tools":["*"]}])
        with self.assertRaises(PlatformError) as error:
            self.platform.gateway.invoke(LOCAL_ACTOR, task["id"], "read_file", {"path":"./calculator.py"})
        self.assertEqual(error.exception.code, "policy_denied")
        call = self.platform.store.list("tool_call", task_id=task["id"])[-1]
        self.assertEqual(call["policy_decision"]["effect"], "deny")
        self.assertTrue(any(item["layer"] == "enterprise" for item in call["policy_decision"]["matched"]))

    def test_mcp_alias_cannot_bypass_the_same_read_policy(self):
        self.platform.settings.policy_rules = [PolicyRule(rule_id="protect-calculator", effect="deny", tools=["read_file"], paths=["calculator.py"])]
        task = self.task()
        self.platform.mcp.prepare(LOCAL_ACTOR, task)
        with self.assertRaises(PlatformError) as error:
            self.platform.gateway.invoke(LOCAL_ACTOR, task["id"], "mcp__project_files__read_file", {"path":"calculator.py"})
        self.assertEqual(error.exception.code, "policy_denied")

    def test_project_write_policy_denies_before_approval(self):
        self.platform.update_project(LOCAL_ACTOR,self.project["id"],policy_rules=[{"rule_id":"no-tests-write","effect":"deny","tools":["apply_patch"],"paths":["tests/**"]}])
        task = self.task(Mode.WRITE)
        with self.assertRaises(PlatformError) as error:
            self.platform.run(LOCAL_ACTOR,task["id"])
        self.assertEqual(error.exception.code,"policy_denied")
        self.assertEqual(self.platform.store.list("approval",task_id=task["id"]),[])

    def test_failing_required_hook_prevents_command_execution_and_is_audited(self):
        task = self.task(Mode.WRITE)
        self.platform.run(LOCAL_ACTOR,task["id"])
        self.platform.gateway.hooks["BeforeCommand"].append(lambda actor, task, payload: False)
        with patch.object(self.platform.runner,"verify") as verify, self.assertRaises(PlatformError) as error:
            self.approve(task["id"])
        verify.assert_not_called()
        self.assertEqual(error.exception.code,"hook_rejected")
        events=self.platform.store.list("event",task_id=task["id"])
        self.assertTrue(any(item["kind"]=="HookExecuted" and item["detail"]["hook"]=="BeforeCommand" and item["detail"]["status"]=="failed" for item in events))

    def test_passed_label_without_exit_code_is_rejected(self):
        task=self.task(Mode.WRITE)
        self.platform.run(LOCAL_ACTOR,task["id"])
        with patch.object(self.platform.runner,"verify",return_value={"status":"passed","exit_code":None,"stdout":"","stderr":"","backend":"fixture"}), self.assertRaises(PlatformError) as error:
            self.approve(task["id"])
        self.assertEqual(error.exception.code,"verification_evidence")
        self.assertNotEqual(self.platform.store.get("task",task["id"])["status"],"completed")

    def test_hooks_record_real_plan_lifecycle_and_session_rules_survive(self):
        first=self.task(Mode.PLAN,policy_rules=[{"rule_id":"session-no-shell","effect":"deny","tools":["run_shell"]}])
        self.platform.run(LOCAL_ACTOR,first["id"])
        hooks={item["detail"]["hook"] for item in self.platform.store.list("event",task_id=first["id"]) if item["kind"]=="HookExecuted"}
        self.assertTrue({"UserPromptSubmit","SessionStart","BeforePlan","AfterPlan","TaskCompleted","SessionEnd"}.issubset(hooks))
        second=self.task(Mode.PLAN,session_id=first["session_id"])
        decision=self.platform.gateway.policy(LOCAL_ACTOR,second,self.project,"run_shell",["."])
        self.assertEqual(decision["effect"],"deny")

    def test_protected_sources_do_not_leak_through_search_rag_or_listing(self):
        private = Path(self.project["root"])/"docs/private.md"
        private.write_text("restricted_unique_business_fact",encoding="utf-8")
        self.platform.update_project(LOCAL_ACTOR,self.project["id"],policy_rules=[{"rule_id":"private-doc","effect":"deny","tools":["read_file"],"paths":["docs/private.md"]}])
        task=self.task()
        for name,args in (("search_code",{"query":"restricted_unique_business_fact"}),("rag_search",{"query":"restricted_unique_business_fact"}),("list_files",{})):
            result=self.platform.gateway.invoke(LOCAL_ACTOR,task["id"],name,args)
            self.assertNotIn("restricted_unique_business_fact",json.dumps(result))
            self.assertNotIn("docs/private.md",json.dumps(result))

    def test_graph_filters_previously_indexed_protected_sources(self):
        from code_agent_platform.graph import CodeGraph
        import test_platform_api_graph as graph_fixtures
        for name,content in graph_fixtures.JAVA.items():
            (Path(self.project["root"])/name).write_text(content,encoding="utf-8")
        graph=CodeGraph(self.platform)
        graph.build(LOCAL_ACTOR,self.project["id"])
        index=self.platform.store.get("graph","graph_"+self.project["id"])
        symbol=next(item["id"] for item in index["nodes"] if item["type"]=="API")
        self.platform.update_project(LOCAL_ACTOR,self.project["id"],policy_rules=[{"rule_id":"private-mapper","effect":"deny","tools":["read_file"],"paths":["UserMapper.xml"]}])
        result=graph.query(LOCAL_ACTOR,self.project["id"],symbol)
        self.assertEqual(result["status"],"ready")
        self.assertNotIn("sys_user",json.dumps(result))
        self.assertNotIn("UserMapper.xml",json.dumps(result))

    def test_identity_revocation_is_rechecked_on_every_tool_call(self):
        task=self.task()
        self.platform.actor_resolver=lambda user_id: None
        with self.assertRaises(PlatformError) as error:
            self.platform.gateway.invoke(LOCAL_ACTOR,task["id"],"read_file",{"path":"calculator.py"})
        self.assertEqual(error.exception.code,"identity_revoked")

    def test_git_status_does_not_expose_protected_or_sensitive_paths(self):
        root=Path(self.project["root"])
        (root/"docs/private.md").write_text("internal",encoding="utf-8")
        (root/".env").write_text("EXAMPLE=private",encoding="utf-8")
        subprocess.run(["git","init",str(root)],check=True,capture_output=True)
        self.platform.update_project(LOCAL_ACTOR,self.project["id"],policy_rules=[{"rule_id":"private-doc","effect":"deny","tools":["read_file"],"paths":["docs/private.md"]}])
        result=self.platform.gateway.invoke(LOCAL_ACTOR,self.task()["id"],"git_status",{})
        self.assertNotIn("docs/private.md",json.dumps(result))
        self.assertNotIn(".env",json.dumps(result))

    def test_repair_receives_actual_failure_logs_and_stops_after_two_repairs(self):
        from code_agent_platform.engine import DemoEngine
        class RecordingEngine(DemoEngine):
            repairs=[]
            def patch(inner,actor,task,context):
                if "test_failures" in context:
                    inner.repairs.append(context["test_failures"])
                return super().patch(actor,task,context)
        engine=RecordingEngine()
        self.platform.engine_override=engine
        task=self.task(Mode.WRITE)
        self.platform.run(LOCAL_ACTOR,task["id"])
        with patch.object(self.platform.runner,"verify",side_effect=lambda *args: {"status":"failed","exit_code":1,"stdout":"test trace","stderr":"expected 5, got 4","backend":"fixture"}) as verify:
            result=self.approve(task["id"])
        self.assertEqual(verify.call_count,3)
        self.assertEqual(len(engine.repairs),2)
        self.assertTrue(all(item["stderr"]=="expected 5, got 4" for item in engine.repairs))
        self.assertEqual(result["status"],"failed")
        self.assertEqual(result["failure_category"],"tests")
        self.assertEqual(self.platform.store.list("approval",task_id=task["id"])[-1]["kind"],"plan")


if __name__ == "__main__":
    unittest.main()
