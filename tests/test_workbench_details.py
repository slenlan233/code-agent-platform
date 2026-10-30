import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.messages import AIMessage, ToolMessage
from openai.types.chat import ChatCompletion
from code_agent_platform.deepseek_model import DeepSeekModel
from code_agent_platform.engine import UsageMiddleware
from code_agent_platform.models import LOCAL_ACTOR, PlatformError
from code_agent_platform.project_folders import browse_folders
from code_agent_platform.tool_inventory import inventory
from code_agent_platform.usage import accumulate_usage, provider_usage
import test_platform as fixtures
import test_platform_api_graph as api_fixtures


class UsageTests(unittest.TestCase):
    def message(self,cached=4):
        details={"input_tokens":10,"output_tokens":3,"total_tokens":13}
        if cached is not None:details["input_token_details"]={"cache_read":cached}
        return AIMessage(content="fixture",usage_metadata=details)

    def test_cache_is_a_subset_of_input_and_ignores_tool_messages(self):
        usage,reported=provider_usage([self.message(),ToolMessage(content="fixture",tool_call_id="one")])
        self.assertTrue(reported)
        self.assertEqual(usage,{"input_tokens":10,"output_tokens":3,"cache_read_tokens":4,"uncached_input_tokens":6})
        self.assertEqual(usage["input_tokens"]+usage["output_tokens"],13)

    def test_zero_hits_are_known_but_missing_hits_stay_unknown(self):
        self.assertEqual(provider_usage([self.message(0)])[0]["cache_read_tokens"],0)
        self.assertIsNone(provider_usage([self.message(None)])[0]["cache_read_tokens"])
        self.assertIsNone(provider_usage([self.message(),self.message(None)])[0]["uncached_input_tokens"])

    def test_aggregation_preserves_legacy_unknowns(self):
        usage,_=provider_usage([self.message()])
        new={"input_tokens":0,"output_tokens":0}
        accumulate_usage(new,usage);accumulate_usage(new,usage)
        self.assertEqual(new["cache_read_tokens"],8)
        self.assertEqual(new["uncached_input_tokens"],12)
        legacy={"input_tokens":20,"output_tokens":2}
        accumulate_usage(legacy,usage)
        self.assertIsNone(legacy["cache_read_tokens"])

    def test_deepseek_sdk_cache_fields_reach_accounting(self):
        model=DeepSeekModel(model="deepseek-flash",api_key="fixture-key")
        response=ChatCompletion.model_validate({"id":"fixture","object":"chat.completion","created":1,"model":"deepseek-flash","choices":[{"index":0,"finish_reason":"stop","message":{"role":"assistant","content":"fixture"}}],"usage":{"prompt_tokens":10,"completion_tokens":3,"total_tokens":13,"prompt_cache_hit_tokens":4,"prompt_cache_miss_tokens":6}})
        message=model._create_chat_result(response).generations[0].message
        self.assertEqual(provider_usage([message])[0]["cache_read_tokens"],4)


class WorkbenchServiceTests(unittest.TestCase):
    setUp=fixtures.PlatformTests.setUp
    task=fixtures.PlatformTests.task

    def test_model_call_and_task_both_save_cache_breakdown(self):
        task=self.task()
        middleware=UsageMiddleware(self.platform.gateway,task["id"],"context_agent")
        message=AIMessage(content="fixture",usage_metadata={"input_tokens":10,"output_tokens":3,"total_tokens":13,"input_token_details":{"cache_read":4}})
        for _ in range(2):middleware.wrap_model_call(None,lambda request:SimpleNamespace(result=[message]))
        updated=self.platform.store.get("task",task["id"])
        self.assertEqual(updated["usage"]["cache_read_tokens"],8)
        self.assertEqual(updated["usage"]["uncached_input_tokens"],12)
        self.assertTrue(all(row["cache_read_tokens"]==4 for row in self.platform.store.list("model_call",task_id=task["id"])))

    def test_inventory_lists_disabled_mysql_and_native_shell_without_password(self):
        with patch.dict(os.environ,{"MYSQL_READ_PASSWORD":"private-fixture-password","MYSQL_DATABASES":"example_db"}):
            data=inventory(self.platform,LOCAL_ACTOR,self.project)
        mysql=next(item for item in data["servers"] if item["server"]=="mysql_readonly")
        self.assertEqual(mysql["status"],"disabled")
        self.assertTrue(mysql["connection"]["password_configured"])
        self.assertNotIn("private-fixture-password",json.dumps(data))
        self.assertIn("run_powershell",[item["name"] for item in data["native_tools"]])
        self.assertTrue(mysql["available_tools"])

    def test_folder_browser_stays_within_existing_roots_and_hides_sensitive_dirs(self):
        (self.root/"project-folder").mkdir()
        (self.root/".ssh").mkdir()
        listing=browse_folders(self.settings,str(self.root))
        self.assertIn("project-folder",[row["name"] for row in listing["folders"]])
        self.assertNotIn(".ssh",[row["name"] for row in listing["folders"]])
        with self.assertRaises(PlatformError) as error:browse_folders(self.settings,str(self.root.parent))
        self.assertEqual(error.exception.code,"folder_scope")
        with self.assertRaises(PlatformError):browse_folders(self.settings,str(self.root/".."))


class WorkbenchApiTests(unittest.TestCase):
    setUp=api_fixtures.ApiGraphTests.setUp

    def test_folder_picker_requires_admin_and_authentication(self):
        self.assertEqual(self.client.get("/api/project-folders").status_code,401)
        reply=self.client.get("/api/project-folders",headers=self.headers)
        self.assertEqual(reply.status_code,200)
        self.assertEqual(reply.json()["allowed_roots"],[str(self.root.resolve())])
        actors={"reader-token":{"user_id":"reader","roles":["developer"],"projects":[self.project["id"]]}}
        with patch.dict(os.environ,{"AI_AGENT_USERS_JSON":json.dumps(actors)}):
            self.assertEqual(self.client.get("/api/project-folders",headers={"Authorization":"Bearer reader-token"}).status_code,403)
        self.assertEqual(self.client.get("/api/project-folders",params={"path":str(self.root.parent)},headers=self.headers).status_code,403)

    def test_tool_checks_show_all_tools_and_keep_mcp_transport_separate(self):
        response=self.client.post("/api/projects/"+self.project["id"]+"/tools/check",headers=self.headers)
        self.assertEqual(response.status_code,200)
        data=response.json()
        self.assertEqual({item["server"] for item in data["servers"]},{"project_files","mysql_readonly","bailian"})
        self.assertIn("run_powershell",[item["name"] for item in data["native_tools"]])
        detail=self.client.get("/api/tasks/"+data["task_id"],headers=self.headers).json()
        self.assertEqual(len(detail["tool_inventory"]["servers"]),3)
        self.assertEqual(detail["task"]["usage"]["model_calls"],0)
        self.assertIn("artifact_directory",detail)
        asset=self.client.get("/workbench-features.js")
        self.assertEqual(asset.status_code,200)
        self.assertIn("javascript",asset.headers["content-type"])


if __name__=="__main__":unittest.main()
