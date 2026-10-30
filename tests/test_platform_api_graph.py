import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from code_agent_platform.api import create_app
from code_agent_platform.config import Settings
from code_agent_platform.graph import CodeGraph
from code_agent_platform.models import Actor, LOCAL_ACTOR, ProjectInput, TaskInput
from code_agent_platform.service import Platform

from test_platform import FixtureRunner


JAVA = {
    "UserController.java": '''package demo;
@RequestMapping("/api")
class UserController {
    private UserService service;
    @GetMapping("/users/{id}")
    public String find(int id) { return service.find(id); }
}
''',
    "UserService.java": '''package demo;
class UserService {
    private UserMapper mapper;
    public String find(int id) { return mapper.find(id); }
}
''',
    "UserMapper.java": '''package demo;
interface UserMapper { String find(int id); }
''',
    "UserMapper.xml": '''<mapper namespace="demo.UserMapper"><select id="find">select phone from sys_user where id = #{id}</select></mapper>''',
}


class ApiGraphTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.platform = Platform(Settings(data_dir=self.root / "data", allowed_roots=[self.root]), runner=FixtureRunner())
        self.addCleanup(self.platform.close)
        self.project = self.platform.create_demo(LOCAL_ACTOR)
        self.app = create_app(platform=self.platform)
        self.client = self.enterContext(TestClient(self.app))
        self.headers = {"Authorization": "Bearer " + self.platform.store.local_token()}

    def wait(self, task_id, statuses):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            result = self.client.get("/api/tasks/" + task_id, headers=self.headers)
            self.assertEqual(result.status_code, 200)
            task = result.json()["task"]
            if task["status"] in statuses:
                return task
            time.sleep(0.05)
        self.fail("background task did not finish")

    def test_auth_and_cross_project_checks(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/api/projects").status_code, 401)
        actors = {"outsider-token": {"user_id": "outsider", "roles": ["developer"], "projects": ["other"]}}
        with patch.dict(os.environ, {"AI_AGENT_USERS_JSON": json.dumps(actors)}):
            headers = {"Authorization": "Bearer outsider-token"}
            self.assertEqual(self.client.get("/api/projects", headers=headers).json(), [])
            response = self.client.get("/api/tasks?project_id=" + self.project["id"], headers=headers)
            self.assertEqual(response.status_code, 403)

    def test_connector_check_is_authenticated_audited_and_not_a_model_success(self):
        url = "/api/projects/" + self.project["id"] + "/tools/check"
        self.assertEqual(self.client.post(url).status_code, 401)
        result = self.client.post(url, headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["servers"][0]["status"], "connected")
        task = self.platform.store.get("task", result.json()["task_id"])
        self.assertEqual(task["usage"]["model_calls"], 0)
        self.assertEqual(task["kind"], "connector_check")
        self.assertEqual(self.client.get("/api/metrics?project_id="+self.project["id"], headers=self.headers).json()["tasks"], 0)
        actors = {"outsider-token": {"user_id": "outsider", "roles": ["developer"], "projects": ["other"]}}
        with patch.dict(os.environ, {"AI_AGENT_USERS_JSON": json.dumps(actors)}):
            self.assertEqual(self.client.post(url, headers={"Authorization": "Bearer outsider-token"}).status_code, 403)

    def test_console_html_and_approval_api_closed_loop(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("研发任务工作台", page.text)
        submitted = self.client.post("/api/tasks", headers=self.headers, json={"project_id": self.project["id"], "user_input": "补加法测试", "mode": "approval_write", "engine": "demo"})
        self.assertEqual(submitted.status_code, 202)
        task_id = submitted.json()["id"]
        task = self.wait(task_id, {"awaiting_plan", "failed"})
        self.assertEqual(task["status"], "awaiting_plan")
        bypass = self.client.post("/api/tasks/" + task_id + "/resume", headers=self.headers)
        self.assertEqual(bypass.status_code, 403)
        self.client.post("/api/approvals/" + task["approval_id"], headers=self.headers, json={"decision": "approved"})
        task = self.wait(task_id, {"awaiting_diff", "failed"})
        self.assertEqual(task["status"], "awaiting_diff")
        self.client.post("/api/approvals/" + task["approval_id"], headers=self.headers, json={"decision": "approved"})
        task = self.wait(task_id, {"completed", "failed"})
        self.assertEqual(task["status"], "completed")
        exported = self.client.get("/api/tasks/" + task_id + "/export", headers=self.headers)
        self.assertEqual(exported.status_code, 200)
        self.assertIn("test_calculator.py", exported.json()["diff"])
        metrics = self.client.get("/api/metrics?project_id=" + self.project["id"], headers=self.headers).json()
        self.assertEqual(metrics["verified"], 1)
        self.assertEqual(metrics["demo_tasks"], 1)

    def java_project(self):
        root = self.root / "java"
        root.mkdir()
        for name, content in JAVA.items():
            (root / name).write_text(content, encoding="utf-8")
        return self.platform.register_project(LOCAL_ACTOR, ProjectInput(name="Java fixture", root=str(root)))

    def test_java_api_service_mapper_table_chain_and_impact(self):
        project = self.java_project()
        graph = CodeGraph(self.platform)
        build = graph.build(LOCAL_ACTOR, project["id"])
        self.assertGreaterEqual(build["edges"], 4)
        result = graph.query(LOCAL_ACTOR, project["id"], "GET /api/users/{id}", depth=5)
        self.assertEqual(result["status"], "ready")
        self.assertIn("sys_user", [item["name"] for item in result["nodes"]])
        impact = graph.query(LOCAL_ACTOR, project["id"], "sys_user", direction="callers", depth=5)
        self.assertIn("GET /api/users/{id}", [item["name"] for item in impact["nodes"]])
        self.assertFalse(result["complete"])
        repeated = graph.build(LOCAL_ACTOR, project["id"])
        self.assertEqual(repeated["parsed_files"], 0)

    def test_graph_staleness_and_removed_relationship(self):
        project = self.java_project()
        graph = CodeGraph(self.platform)
        graph.build(LOCAL_ACTOR, project["id"])
        path = Path(project["root"]) / "UserService.java"
        path.write_text('package demo; class UserService { public String find(int id) { return "local"; } }', encoding="utf-8")
        self.assertEqual(graph.query(LOCAL_ACTOR, project["id"], "sys_user")["status"], "stale")
        graph.build(LOCAL_ACTOR, project["id"])
        result = graph.query(LOCAL_ACTOR, project["id"], "GET /api/users/{id}", depth=5)
        self.assertNotIn("sys_user", [item["name"] for item in result["nodes"]])

    def test_graph_tool_is_audited_and_uses_current_project(self):
        project = self.java_project()
        CodeGraph(self.platform).build(LOCAL_ACTOR, project["id"])
        task = self.platform.create_task(LOCAL_ACTOR, TaskInput(project_id=project["id"], user_input="查询调用链"))
        result = self.platform.gateway.invoke(LOCAL_ACTOR, task["id"], "graph_query", {"symbol": "GET /api/users/{id}", "depth": 5})
        self.assertTrue(result["evidence"])
        self.assertTrue(self.platform.store.list("tool_call", task_id=task["id"]))
        for source in result["evidence"]:
            self.assertEqual(source["project_id"], project["id"])


if __name__ == "__main__":
    unittest.main()
