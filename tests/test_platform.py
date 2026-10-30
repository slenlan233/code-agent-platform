"""Governance and workflow integration tests; no provider API requests."""

import json
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from langchain_core.tools import tool
from pydantic import ValidationError
from sqlalchemy import update

from code_agent_platform.config import Settings
from code_agent_platform.models import Actor, ApprovalDecision, Budget, LOCAL_ACTOR, Mode, PlatformError, ProjectInput, TaskInput
from code_agent_platform.runtime import DockerRunner
from code_agent_platform.security import safe_path
from code_agent_platform.service import Platform


class FixtureRunner:
    """Only test fixtures execute locally; production always uses DockerRunner."""
    def health(self):
        return {"available": True, "version": "test-fixture"}

    def verify(self, task, project, workspace):
        result = subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests"], cwd=workspace["path"], capture_output=True, timeout=20)
        return {"status": "passed" if result.returncode == 0 else "failed", "exit_code": result.returncode, "stdout": result.stdout.decode(errors="replace"), "stderr": result.stderr.decode(errors="replace"), "backend": "test_fixture", "test_count": 3}


class MissingRunner(FixtureRunner):
    def verify(self, task, project, workspace):
        return {"status": "unavailable", "exit_code": None, "stdout": "", "stderr": "", "reason": "fixture offline"}


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.settings = Settings(data_dir=self.root / "data", allowed_roots=[self.root])
        self.platform = Platform(self.settings, runner=FixtureRunner())
        self.addCleanup(lambda: self.platform.close())
        self.actor = LOCAL_ACTOR
        self.project = self.platform.create_demo(self.actor)

    def task(self, mode=Mode.READONLY, **kwargs):
        return self.platform.create_task(self.actor, TaskInput(project_id=self.project["id"], user_input="解释加法函数与测试规范，补充单元测试", mode=mode, engine="demo", **kwargs))

    def approve(self, task_id, decision="approved"):
        task = self.platform.store.get("task", task_id)
        revision = self.platform.store.get('approval', task['approval_id']).get('decision_revision', 0)
        self.platform.decide(self.actor, task["approval_id"], ApprovalDecision(decision=decision, decision_revision=revision))
        return self.platform.run(self.actor, task_id, resume=True)

    def completed_write(self):
        task = self.task(Mode.WRITE)
        first = self.platform.run(self.actor, task["id"])
        self.assertEqual(first["status"], "awaiting_plan")
        second = self.approve(task["id"])
        self.assertEqual(second["status"], "awaiting_diff")
        self.assertEqual(second["verification"]["status"], "passed")
        final = self.approve(task["id"])
        self.assertEqual(final["status"], "completed")
        return final

    def test_readonly_task_has_sources_audit_and_no_writes(self):
        task = self.task()
        result = self.platform.run(self.actor, task["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["summary"]["test_status"], "not_run")
        self.assertTrue(result["answer"]["findings"][0]["source_ids"])
        self.assertGreater(result["usage"]["tool_calls"], 0)
        self.assertTrue(self.platform.store.list("tool_call", task_id=task["id"]))
        self.assertFalse((Path(self.project["root"]) / "tests/test_calculator.py").exists())

    def test_unknown_write_tool_is_denied_and_audited(self):
        task = self.task()
        with self.assertRaises(PlatformError) as result:
            self.platform.gateway.invoke(self.actor, task["id"], "WriteFile", {"path": "calculator.py", "content": "bad"})
        self.assertEqual(result.exception.status, 403)
        calls = self.platform.store.list("tool_call", task_id=task["id"])
        self.assertEqual(calls[-1]["status"], "denied")

    def test_sensitive_path_traversal_and_cross_project(self):
        task = self.task()
        for name in ("../outside.txt", ".env", ".git/config", "C:/Windows/win.ini", "file.txt:stream"):
            with self.subTest(name=name), self.assertRaises(PlatformError):
                self.platform.gateway.invoke(self.actor, task["id"], "read_file", {"path": name})
        outsider = Actor(user_id="outsider", projects=frozenset({"other"}))
        with self.assertRaises(PlatformError):
            self.platform.task(outsider, task["id"])

    def test_sessions_are_owned_and_unique(self):
        first, second = self.task(), self.task()
        self.assertNotEqual(first["session_id"], second["session_id"])
        other = Actor(user_id="other", projects=frozenset({self.project["id"]}))
        with self.assertRaises(PlatformError):
            self.platform.create_task(other, TaskInput(project_id=self.project["id"], session_id=first["session_id"], user_input="hello", engine="demo"))

    def test_plan_only_does_not_create_patch(self):
        task = self.task(Mode.PLAN)
        result = self.platform.run(self.actor, task["id"])
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["plan_id"])
        self.assertEqual(self.platform.store.list("patch", task_id=task["id"]), [])

    def test_real_fixture_tests_then_diff_approval_export_and_apply(self):
        result = self.completed_write()
        target = Path(self.project["root"]) / "tests/test_calculator.py"
        self.assertFalse(target.exists())
        exported = self.platform.export_patch(self.actor, result["id"])
        self.assertIn("+++ b/tests/test_calculator.py", exported["diff"])
        applied = self.platform.apply_patch(self.actor, result["id"])
        self.assertEqual(applied["status"], "applied")
        self.assertTrue(target.exists())
        self.assertEqual(self.platform.apply_patch(self.actor, result["id"])["status"], "already_applied")
        self.assertTrue(applied["rollback_artifact_id"])
        self.assertEqual(self.platform.export_patch(self.actor, result["id"])["diff"], exported["diff"])

    def test_approval_cannot_be_bypassed_or_reused_after_baseline_change(self):
        task = self.task(Mode.WRITE)
        self.platform.run(self.actor, task["id"])
        with self.assertRaises(PlatformError):
            self.platform.run(self.actor, task["id"], resume=True)
        path = Path(self.project["root"]) / "calculator.py"
        path.write_text("def add(a, b): return a - b\n", encoding="utf-8")
        with self.assertRaises(PlatformError) as changed:
            self.approve(task["id"])
        self.assertEqual(changed.exception.code, "source_changed")

    def test_rejection_has_no_side_effect(self):
        task = self.task(Mode.WRITE)
        self.platform.run(self.actor, task["id"])
        result = self.approve(task["id"], "rejected")
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.platform.store.list("patch", task_id=task["id"]), [])

    def test_expired_approval_and_role_are_enforced(self):
        task = self.task(Mode.WRITE)
        waiting = self.platform.run(self.actor, task["id"])
        developer = Actor(user_id="dev", projects=frozenset({self.project["id"]}))
        with self.assertRaises(PlatformError):
            self.platform.decide(developer, waiting["approval_id"], ApprovalDecision(decision="approved"))
        self.platform.store.mutate("approval", waiting["approval_id"], lambda row: {**row, "expires_epoch": 0})
        with self.assertRaises(PlatformError) as expired:
            self.approve(task["id"])
        self.assertEqual(expired.exception.code, "approval_expired")

    def test_project_shutdown_stops_inflight_execution(self):
        task = self.task(Mode.WRITE)
        self.platform.run(self.actor, task["id"])
        self.platform.update_project(self.actor, self.project["id"], allow_write=False)
        with self.assertRaises(PlatformError):
            self.approve(task["id"])
        self.assertEqual(self.platform.store.list("sandbox_run", task_id=task["id"]), [])

    def test_budget_is_a_backend_limit(self):
        task = self.task(budget=Budget(max_tool_calls=1))
        with self.assertRaises(PlatformError) as exhausted:
            self.platform.run(self.actor, task["id"])
        self.assertEqual(exhausted.exception.code, "tool_budget")

    def test_interrupt_survives_platform_restart(self):
        task = self.task(Mode.WRITE)
        self.platform.run(self.actor, task["id"])
        self.platform.close()
        self.platform = Platform(self.settings, runner=FixtureRunner())
        after = self.approve(task["id"])
        self.assertEqual(after["status"], "awaiting_diff")

    def test_missing_sandbox_waits_and_can_resume(self):
        self.platform.runner = MissingRunner()
        task = self.task(Mode.WRITE)
        self.platform.run(self.actor, task["id"])
        pending = self.approve(task["id"])
        self.assertEqual(pending["status"], "waiting_environment")
        self.platform.runner = FixtureRunner()
        result = self.platform.run(self.actor, task["id"], resume=True)
        self.assertEqual(result["status"], "awaiting_diff")

    def test_docker_has_isolation_flags_and_no_host_shell(self):
        command = DockerRunner(self.settings).command(self.project, {"path": str(self.root / "stage")}, "fixture")
        for flag in ("--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pull=never"):
            self.assertIn(flag, command)
        self.assertNotIn("--privileged", command)
        self.assertNotIn("/var/run/docker.sock", " ".join(command))

    def test_apply_recovers_after_write_before_receipt(self):
        result = self.completed_write()
        original = self.platform._atomic_content
        calls = []
        def interrupted(target, content):
            original(target, content)
            calls.append(target)
            raise RuntimeError("simulated crash after atomic file write")
        with patch.object(self.platform, "_atomic_content", side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                self.platform.apply_patch(self.actor, result["id"])
        self.assertEqual(len(calls), 1)
        recovered = self.platform.apply_patch(self.actor, result["id"])
        self.assertEqual(recovered["status"], "applied")
        self.assertEqual(self.platform.apply_patch(self.actor, result["id"])["status"], "already_applied")

    def test_rollback_preserves_later_manual_changes(self):
        result = self.completed_write()
        self.platform.apply_patch(self.actor, result["id"])
        target = Path(self.project["root"]) / "tests/test_calculator.py"
        target.write_text(target.read_text(encoding="utf-8") + "\n# manual change\n", encoding="utf-8")
        with self.assertRaises(PlatformError) as conflict:
            self.platform.rollback_patch(self.actor, result["id"])
        self.assertEqual(conflict.exception.code, "rollback_conflict")
        self.assertIn("manual change", target.read_text(encoding="utf-8"))

    def test_explicit_rollback_restores_original_content(self):
        result = self.completed_write()
        self.platform.apply_patch(self.actor, result["id"])
        self.assertEqual(self.platform.rollback_patch(self.actor, result["id"])["status"], "rolled_back")
        self.assertFalse((Path(self.project["root"]) / "tests/test_calculator.py").exists())
        self.assertEqual(self.platform.rollback_patch(self.actor, result["id"])["status"], "already_rolled_back")

    def test_registered_readonly_mcp_uses_gateway_audit_and_hooks(self):
        @tool
        async def repository_note(path: str) -> str:
            """Return a trusted server fixture note."""
            return "fixture note for " + path
        with self.assertRaises(ValueError):
            self.platform.gateway.register_mcp("unsafe", repository_note)
        self.platform.gateway.register_mcp("repository_note", repository_note, read_only=True, resource_fields=("path",))
        hooks = []
        self.platform.gateway.hooks["PreToolUse"].append(lambda *args: hooks.append("pre"))
        self.platform.gateway.hooks["PostToolUse"].append(lambda *args: hooks.append("post"))
        task = self.task()
        result = self.platform.gateway.invoke(self.actor, task["id"], "repository_note", {"path": "calculator.py"})
        self.assertIn("fixture note", result)
        self.assertEqual(hooks, ["pre", "post"])
        with self.assertRaises(PlatformError):
            self.platform.gateway.invoke(self.actor, task["id"], "repository_note", {"path": "../outside"})
        self.assertEqual(self.platform.store.list("tool_call", task_id=task["id"])[-1]["status"], "denied")

    def test_live_lease_blocks_duplicate_and_stale_local_process_can_resume(self):
        task = self.task()
        token = self.platform.store.acquire(task["id"])
        with self.assertRaises(PlatformError) as busy:
            self.platform.store.acquire(task["id"])
        self.assertEqual(busy.exception.code, "task_busy")
        # A reused PID with a different process creation time is a stale holder.
        parts = token.split(":")
        parts[2] = "0"
        table = self.platform.store.tables["task"]
        with self.platform.store.engine.begin() as connection:
            connection.execute(update(table).where(table.c.id == task["id"]).values(lease_id=":".join(parts)))
        recovered = self.platform.store.acquire(task["id"])
        self.platform.store.release(task["id"], recovered)

    def test_invalid_git_option_cannot_create_a_review_task(self):
        before = len(self.platform.store.list("task"))
        with self.assertRaises(ValidationError):
            TaskInput(project_id=self.project["id"], user_input="review", mode=Mode.REVIEW, base_ref="--output=file")
        self.assertEqual(len(self.platform.store.list("task")), before)

    def test_workspace_bytes_match_the_approved_patch_on_windows(self):
        task = self.task(Mode.WRITE)
        content = "alpha\nbeta\r\ngamma\n"
        workspace = self.platform.workspace.prepare(task, self.project, [{"path": "tests/endings.txt", "content": content}])
        try:
            self.assertEqual((Path(workspace["path"]) / "tests/endings.txt").read_bytes(), content.encode("utf-8"))
        finally:
            self.platform.workspace.cleanup(task["id"])

    def test_completed_idle_time_does_not_consume_execution_budget(self):
        result = self.completed_write()
        self.platform.store.mutate("task", result["id"], lambda row: {**row, "budget": {**row["budget"], "max_seconds": 60}})
        with patch("code_agent_platform.gateway.time.time", return_value=time.time() + 600):
            exported = self.platform.export_patch(self.actor, result["id"])
        self.assertIn("test_calculator.py", exported["diff"])
        self.platform.store.mutate("task", result["id"], lambda row: {key: value for key, value in row.items() if key != "finished_epoch"})
        with patch("code_agent_platform.gateway.time.time", return_value=time.time() + 600):
            self.assertEqual(self.platform.export_patch(self.actor, result["id"])["diff"], exported["diff"])

    def test_project_write_lease_prevents_two_platforms_from_overwriting(self):
        result = self.completed_write()
        other = Platform(self.settings, runner=FixtureRunner())
        try:
            token = other.store.acquire(self.project["id"], kind="project")
            try:
                with self.assertRaises(PlatformError) as busy:
                    self.platform.apply_patch(self.actor, result["id"])
                self.assertEqual(busy.exception.code, "task_busy")
                self.assertFalse((Path(self.project["root"]) / "tests/test_calculator.py").exists())
            finally:
                other.store.release(self.project["id"], token, kind="project")
            self.assertEqual(self.platform.apply_patch(self.actor, result["id"])["status"], "applied")
        finally:
            other.close()


if __name__ == "__main__":
    unittest.main()
