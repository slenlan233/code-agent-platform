from __future__ import annotations

import ast
import difflib
import json
import os
import re
import shutil
import time
from contextlib import contextmanager
from datetime import datetime
from importlib.resources import files as resources
from pathlib import Path
from threading import RLock
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from typing_extensions import TypedDict

from .config import Settings
from .engine import DemoEngine, ModelEngine, system_prompt
from .gateway import Gateway
from .git_support import repository_info
from .intent import requests_implementation_after_approval
from .models import Actor, ApprovalDecision,AnswerDraft,PlanDraft, Budget, DEFAULT_PROJECT_TOKEN_BUDGET, MAX_TOKEN_BUDGET, FileChange,GitDiffArguments, Mode, PlatformError, PowerShellArguments, ProjectInput, TaskInput, digest, identifier, timestamp
from pydantic import ValidationError
from .runtime import DockerRunner, Workspace
from .security import SECRET, authorize, blocked, files, redact, relative_path, safe_path
from .review_scope import diff_lines, location_ranges, review_error
from .reset_verification import check_reset_plan, is_reset, reset_paths, verify_reset
from .policy import enforce
from .storage import Store

_UNSET = object()


class TaskState(TypedDict, total=False):
    task_id: str
    context_id: str
    plan_id: str
    patch_id: str
    plan_approval: str
    diff_approval: str
    database_approval: str
    database_operation_id: str
    verification: str
    rejected: bool
    fix_count: int
    review_status: str
    review_fix_count: int
    answer_draft:dict|None
    plan_draft:dict|None


class Platform:
    def __init__(self, settings: Settings, *, runner=None, engine=None):
        self.settings = settings
        self.store = Store(settings.data_dir, settings.database_url)
        self.gateway = Gateway(settings, self.store)
        from .cancellation import ModelIO
        self.model_io = ModelIO()
        self.gateway.model_io = self.model_io
        from .conversations import Conversations
        self.conversations = Conversations(self)
        from .library import WorkspaceLibrary
        self.library = WorkspaceLibrary(self)
        self.gateway.conversations = self.conversations
        from .business_mysql import BusinessMySQL
        self.business_mysql = BusinessMySQL(self)
        from .database_deletion import DatabaseDeletion
        self.database_deletion = DatabaseDeletion(self)
        self.gateway.actor_resolver = lambda actor: getattr(self, "actor_resolver", lambda user_id: actor)(actor.user_id)
        self.runner = runner or DockerRunner(settings, check_cancelled=self.gateway.check_cancelled)
        self.workspace = Workspace(settings)
        self.command_workspace = Workspace(settings, namespace="command-workspaces")
        from .powershell import PowerShellRunner
        self.powershell_runner = PowerShellRunner(settings, check_cancelled=self.gateway.check_cancelled)
        self.gateway.tools["run_powershell"] = (PowerShellArguments, self.execute_powershell)
        self.engine_override = engine
        self.apply_lock = RLock()
        from .mcp_runtime import MCPRuntime
        self.mcp = MCPRuntime(self)
        self.gateway.mcp_runtime = self.mcp
        from .graph import CodeGraph
        CodeGraph(self).register_tool()

    def project(self, actor, project_id, *, write=False):
        actor = self.gateway.current_actor(actor)
        project = self.store.get("project", project_id)
        authorize(actor, project, "apply_patch" if write else "list_files", Mode.WRITE if write else Mode.READONLY)
        return project

    def register_project(self, actor: Actor, data: ProjectInput):
        actor.require("admin")
        root = Path(data.root).resolve()
        selection = None
        if data.selection_id:
            from .project_folders import selected_directory, directory_identity
            if not self.settings.allow_local_project_selection or actor.user_id != "local":
                raise PlatformError("folder_selection_disabled", "本机文件夹授权不可用。", 403)
            root=selected_directory(data.root)
            selection=self.store.get("project_selection",data.selection_id)
            if selection["owner_id"]!=actor.user_id or selection["root"]!=str(root) or selection["status"]!="pending" or selection["expires_epoch"]<time.time() or selection["root_identity"]!=directory_identity(root):
                raise PlatformError("folder_selection_invalid", "文件夹选择已失效或不属于当前项目，请重新选择。", 403)
        if not root.is_dir() or selection is None and not any(root.is_relative_to(parent.resolve()) for parent in self.settings.allowed_roots):
            raise PlatformError("project_root", "请先通过“选择项目文件夹”确认这个项目的访问范围。" if self.settings.allow_local_project_selection else "项目必须位于管理员登记的允许目录下。", 403)
        row = data.model_dump()
        row.update(id=identifier("project"), root=str(root), project_id=None)
        for path in [*data.rules, *data.knowledge_paths]:
            safe_path(root, path, must_exist=False)
        if any(name not in self.settings.mcp_servers for name in data.mcp_servers):
            raise PlatformError("mcp_profile", "项目引用了未登记的 MCP 服务。")
        if data.sandbox_image not in self.settings.sandbox_images:
            raise PlatformError("image_denied","运行镜像未由管理员登记。",403)
        if data.database_test_profile=='mysql' and not self.settings.allow_mysql_test_network:
            raise PlatformError('network_denied','MySQL 集成测试网络尚未由管理员启用。',403)
        for existing in self.store.list("project"):
            if Path(existing["root"]) == root and existing.get('lifecycle', 'active') != 'deleted':
                raise PlatformError("project_exists", "该目录已登记。", 409)
        if selection is not None:
            def consume(value):
                if value["status"]!="pending" or value["expires_epoch"]<time.time():
                    raise PlatformError("folder_selection_invalid", "文件夹选择已被使用或已过期。", 403)
                value.update(status="consumed",project_id=row["id"])
                return value
            self.store.mutate("project_selection",selection["id"],consume)
            row.update(access_source="local_selection",root_identity=selection["root_identity"])
        result=self.store.put("project", row)
        if data.allow_write and data.database_test_profile=='mysql' and self.business_mysql.instance().get('auto_provision'):
            try:self.business_mysql.ensure(actor,result['id'])
            except PlatformError as error:
                result['database_setup_error']=error.message
                self.store.put('project',result)
        return {**result,'business_database':self.business_mysql.public(result)}

    def select_local_project(self,actor,path):
        actor=self.gateway.current_actor(actor)
        actor.require("admin")
        if actor.user_id!="local" or not self.settings.allow_local_project_selection:
            raise PlatformError("folder_selection_disabled", "本机文件夹授权不可用。",403)
        from .project_folders import selected_directory,directory_identity
        root=selected_directory(path)
        return self.store.put("project_selection",{"id":identifier("selection"),"owner_id":actor.user_id,"root":str(root),"root_identity":directory_identity(root),"status":"pending","expires_epoch":time.time()+600})

    def projects(self, actor):
        actor = self.gateway.current_actor(actor)
        return [{**item,'business_database':self.business_mysql.public(item) if item.get('enabled') and Path(item['root']).is_dir() else {'status':'disabled','ready':False}} for item in self.store.list("project") if actor.allows(item["id"]) and item.get('lifecycle', 'active') == 'active']

    def update_project(self, actor, project_id, *, enabled=None, allow_write=None, mcp_servers=None, test_command=None, sandbox_image=None, policy_rules=None, team=None, database_test_profile=None,token_budget=_UNSET):
        actor.require("admin")
        project = self.store.get("project", project_id)
        if token_budget is not _UNSET:
            try:token_budget=ProjectInput.model_validate({**project,'token_budget':token_budget}).token_budget
            except ValidationError:raise PlatformError('project_budget','项目 token 总预算应为无上限，或不小于 100 的整数。') from None
            previous=project.get('token_budget',DEFAULT_PROJECT_TOKEN_BUDGET)
            project=self.store.mutate('project',project_id,lambda row:{**row,'token_budget':token_budget})
            self.store.put('event',{'project_id':project_id,'kind':'ProjectTokenBudgetChanged','detail':{'previous':previous,'limit':token_budget,'user_id':actor.user_id}})
            # The token allowance is separate from the approved code/config scope.
            for task in self.store.list('task',project_id=project_id):
                if task.get('token_budget_source')=='project' and task['status'] not in {'completed','cancelled'}:
                    self.store.mutate('task',task['id'],lambda row:{**row,'budget':{**row['budget'],'max_tokens':token_budget}})
            if all(value is None for value in (enabled,allow_write,mcp_servers,test_command,sandbox_image,policy_rules,team,database_test_profile)):
                return project
        if enabled is not None:
            project["enabled"] = enabled
        if allow_write is not None:
            project["allow_write"] = allow_write
        changes = {key: value for key, value in {"mcp_servers": mcp_servers, "test_command": test_command, "sandbox_image": sandbox_image, "policy_rules": policy_rules, "team": team,"database_test_profile":database_test_profile}.items() if value is not None}
        if changes:
            try:
                validated = ProjectInput.model_validate({**project, **changes})
            except ValidationError:
                raise PlatformError("project_config", "项目测试或工具配置格式不正确。") from None
            if any(name not in self.settings.mcp_servers for name in validated.mcp_servers):
                raise PlatformError("mcp_profile", "项目引用了未登记的 MCP 服务。")
            if validated.sandbox_image not in self.settings.sandbox_images:
                raise PlatformError("image_denied", "运行镜像未由管理员登记。", 403)
            if validated.database_test_profile=='mysql' and not self.settings.allow_mysql_test_network:
                raise PlatformError('network_denied','MySQL 集成测试网络尚未由管理员启用。',403)
            project.update({key: getattr(validated, key) for key in changes})
        project["version"] = str(int(project.get("version", "1")) + 1) if str(project.get("version", "1")).isdigit() else identifier("config")
        return self.store.put("project", project)

    def token_budget(self,actor,project_id):
        from .budgeting import project_token_usage
        project=self.project(actor,project_id)
        return {**project_token_usage(self.store,project),'can_edit':'admin' in actor.roles}

    def update_task_token_budget(self,actor,task_id,*,max_tokens=None,follow_project=False):
        actor.require('admin')
        task=self.task(actor,task_id)
        if task['status'] in {'completed','cancelled'}:raise PlatformError('task_finished','已结束的任务无需修改额度。',409)
        project=self.project(actor,task['project_id'])
        limit=project.get('token_budget',DEFAULT_PROJECT_TOKEN_BUDGET) if follow_project else max_tokens
        try:validated=Budget.model_validate({**task['budget'],'max_tokens':limit})
        except ValidationError:raise PlatformError('task_budget','本轮 token 上限应为无上限，或不小于 100 的整数。') from None
        result=self._set(task_id,budget=validated.model_dump(),token_budget_source='project' if follow_project else 'task')
        self.store.event(task_id,'TaskTokenBudgetChanged',{'previous':task['budget']['max_tokens'],'limit':limit,'source':result['token_budget_source'],'user_id':actor.user_id})
        return result

    def create_demo(self, actor):
        actor.require("admin")
        root = self.settings.data_dir / "demo-project"
        for existing in self.store.list("project"):
            if existing.get("is_demo"):
                return existing
        root.mkdir(parents=True, exist_ok=True)
        fixture = resources("code_agent_platform").joinpath("demo")
        for name in ("calculator.py", "docs/testing.md", "tests/.gitkeep", "AGENTS.md"):
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(fixture.joinpath(name).read_text(encoding="utf-8"), encoding="utf-8")
        data = ProjectInput(name="加法测试演示", root=str(root), allow_write=True)
        row = self.register_project(actor, data)
        row["is_demo"] = True
        row["demo_hash"] = digest((root / "calculator.py").read_bytes())
        return self.store.put("project", row)

    def create_task(self, actor: Actor, data: TaskInput):
        self.project(actor, data.project_id)
        with self._project_write_lock(data.project_id):
            return self._create_task_request(actor, data)

    def _create_task_request(self, actor: Actor, data: TaskInput):
        actor=self.gateway.current_actor(actor)
        actor.require("developer")
        if data.session_id:
            session=self.conversations.session(actor,data.session_id)
            if session["project_id"]!=data.project_id:
                raise PlatformError("session_denied","该对话不属于当前项目。",403)
        else:
            session=self.conversations.create(actor,data.project_id)
            if data.policy_rules:
                session=self.store.put("session",{**session,"policy_rules":data.policy_rules})
            data=data.model_copy(update={"session_id":session["id"],"policy_rules":[]})
        lease=self.store.acquire(session["id"],kind="session")
        try:
            from .conversations import is_active
            previous=self.conversations.tasks(session)
            if data.client_request_id:
                same=next((t for t in previous if t.get("client_request_id")==data.client_request_id),None)
                if same:
                    if (same["user_input"],same["mode"],same["engine"])!=(redact(data.user_input),data.mode.value,data.engine):
                        raise PlatformError("message_conflict","这条发送标识已用于其他内容，请重新发送。",409)
                    return same
            if any(is_active(t) and t.get("kind")!="connector_check" for t in previous):
                raise PlatformError("conversation_busy","当前对话还有执行或审批待处理，请先完成或停止这一轮。",409)
            task=self._create_task(actor,data)
            task=self._set(task['id'], history_task_ids=[t['id'] for t in previous if t.get('kind') != 'connector_check'])
            self.conversations.sync_task(task)
            return task
        finally:self.store.release(session["id"],lease,kind="session")

    def _create_task(self, actor: Actor, data: TaskInput):
        actor.require("developer")
        project = self.project(actor, data.project_id, write=data.mode == Mode.WRITE)
        if data.engine == "demo" and (not project.get("is_demo") or digest(safe_path(Path(project["root"]), "calculator.py").read_bytes()) != project.get("demo_hash")):
            raise PlatformError("demo_scope", "预设演示只适用于未修改的内置演示仓库。", 403)
        if data.session_id:
            session = self.store.get("session", data.session_id)
            if session["project_id"] != project["id"] or (session["owner_id"] != actor.user_id and "admin" not in actor.roles):
                raise PlatformError("session_denied", "会话不属于当前用户和项目。", 403)
        else:
            session = self.store.put("session", {"project_id": project["id"], "owner_id": actor.user_id, "summary": {}, "policy_rules": data.policy_rules})
        versions = {**self.settings.versions(), "prompt_hash": digest(system_prompt()), "project": project["version"]}
        priced = self.settings.input_price_per_million is not None and self.settings.output_price_per_million is not None
        row = {"id": identifier("task"), "project_id": project["id"], "session_id": session["id"], "owner_id": actor.user_id, "actor": actor.model_dump(mode="json"), "user_input": redact(data.user_input), "mode": data.mode.value, "engine": data.engine, "status": "queued", "current_node": "INIT", "budget": data.budget.model_dump(), "usage": {"tool_calls": 0, "model_calls": 0, "input_tokens": 0, "output_tokens": 0, "cost": 0 if data.engine == "demo" or priced else None}, "versions": versions, "started_epoch": time.time(), "approval_wait_seconds": 0}
        row["policy_rules"] = data.policy_rules if data.session_id else []
        row.update(conversation_version=1,client_request_id=data.client_request_id)
        row['requested_mode']=data.mode.value
        row['token_budget_source']='task' if 'budget' in data.model_fields_set else 'project'
        if row['token_budget_source']=='project':row['budget']['max_tokens']=project.get('token_budget',DEFAULT_PROJECT_TOKEN_BUDGET)
        row['ai_check_required']=True
        versions["scope_policy_hash"] = digest([session.get("policy_rules", []), row["policy_rules"]])
        self.store.put("task", row)
        if data.base_ref or data.head_ref:
            refs = GitDiffArguments(base_ref=data.base_ref, head_ref=data.head_ref)
            row.update(base_ref=refs.base_ref, head_ref=refs.head_ref)
            self.store.put("task", row)
        self.store.event(row["id"], "TaskCreated", {"mode": data.mode.value, "engine": data.engine, "versions": versions})
        return self.store.get("task", row["id"])

    def task(self, actor, task_id):
        row = self.store.get("task", task_id)
        self.project(actor, row["project_id"])
        from .security import require_active_record
        require_active_record(self.store.get('session', row['session_id']), '对话')
        return row

    def detail(self, actor, task_id):
        task = self.task(actor, task_id)
        result = {"task": task}
        result["artifact_directory"] = str(self.store.artifact_dir / task_id)
        from .tool_inventory import inventory
        result["tool_inventory"] = task.get("tool_inventory") or inventory(self, actor, self.project(actor, task["project_id"]), task.get("tool_connections", []))
        for kind in ("event", "tool_call", "model_call", "evidence", "plan", "approval", "patch", "sandbox_run", "review_run", "command_run", "database_operation", "artifact", "feedback"):
            result[kind + "s"] = sorted(self.store.list(kind, task_id=task_id), key=lambda item: item["created_at"])
        return result

    def _set(self, task_id, **values):
        def update(row):
            result = {**row, **values}
            if row.get('cancel_requested') and values.get('status') not in {None, 'cancelled'}:
                result['status'] = row['status']
                result['current_node'] = row['current_node']
            return result
        row=self.store.mutate("task", task_id, update)
        self.conversations.sync_task(row)
        return row

    def cancel(self, actor, task_id):
        actor.require('developer')
        task = self.task(actor, task_id)
        if task['status'] in {'completed', 'cancelled'}:
            return task
        active = self.store.has_live_lease(task_id)
        needs_cleanup = task['current_node'] in {'SANDBOX_VERIFY', 'POWERSHELL_COMMANDS'} or task.get('cancelled_node') in {'SANDBOX_VERIFY', 'POWERSHELL_COMMANDS'}
        task = self._set(task_id, cancel_requested=True, cancel_requested_at=timestamp(),
                         cancelled_node=task.get('cancelled_node') or task['current_node'],
                         status='cancelling' if active or needs_cleanup else 'cancelled', current_node='CANCELLING' if active or needs_cleanup else task['current_node'])
        self.store.event(task_id, 'TaskCancelled', {'user_id': actor.user_id, 'cleanup_pending': active})
        if not active and needs_cleanup:
            return self.finish_cancellation(task_id)
        return task

    def _complete_cancellation(self, task_id):
        task = self.store.get('task', task_id)
        cleanup = {'verified': True, 'not_needed': True}
        if task.get('cancelled_node') in {'SANDBOX_VERIFY', 'POWERSHELL_COMMANDS'} and isinstance(self.runner, DockerRunner):
            cleanup = self.runner.cleanup_task(task_id)
        self.store.event(task_id, 'TaskStopped', {'execution_stopped': True, 'docker_cleanup': cleanup})
        return self._set(task_id, status='cancelled', stopped_at=timestamp(), cancellation_cleanup=cleanup)

    def finish_cancellation(self, task_id):
        """Startup recovery for a worker that died while stopping."""
        lease = self.store.acquire(task_id)
        try:
            task = self.store.get('task', task_id)
            if task['status'] != 'cancelling':
                return task
            return self._complete_cancellation(task_id)
        finally:
            self.store.release(task_id, lease)

    def _node(self, task_id, name):
        task = self.store.get("task", task_id)
        resolver = getattr(self, "actor_resolver", None)
        actor = resolver(task["owner_id"]) if resolver else Actor.model_validate(task["actor"])
        if actor is None:
            raise PlatformError("identity_revoked", "任务发起人的权限已撤销。", 403)
        actor.require("developer")
        project = self.project(actor, task["project_id"])
        self.gateway.check_budget(task_id)
        self._set(task_id, current_node=name, status="running")
        self.store.event(task_id, "NodeStarted", {"node": name})
        return self.store.get("task", task_id), actor, project

    def _engine(self, task):
        return self.engine_override or (DemoEngine() if task["engine"] == "demo" else ModelEngine(self.settings, self.gateway))

    def _refresh_remote_source(self, task, project, evidence):
        server = evidence['server']
        config = self.settings.mcp_servers.get(server)
        # Replay only built-in knowledge retrieval and known read-only MySQL
        # operations. A third-party readOnlyHint is not a replay contract.
        allowed = {'query_rag'} if config and config.builtin == 'bailian' else {
            'mysql_list_databases', 'mysql_list_tables', 'mysql_describe_tables', 'mysql_execute_query'
        } if config and config.builtin == 'mysql' else set()
        if not allowed:
            raise PlatformError('source_changed', '远程来源已过期，需要重新检索。', 409)
        retrieval = evidence.get('retrieval')
        aliases = {'mcp__'+server+'__'+name for name in allowed}
        if retrieval is None:
            # Compatibility with evidence recorded before replay metadata existed.
            for call in reversed(self.store.list('tool_call', task_id=task['id'])):
                output = call.get('output') or {}
                if call.get('name') in aliases and call.get('status') == 'succeeded' and isinstance(output, dict) and any(
                    item.get('source_id') == evidence['source_id'] for item in output.get('evidence', []) if isinstance(item, dict)
                ):
                    retrieval = {'tool': call['name'], 'arguments': call['input']}
                    break
        if not retrieval or retrieval.get('tool') not in aliases or not isinstance(retrieval.get('arguments'), dict):
            raise PlatformError('source_changed', '远程引用缺少原检索记录，需要重新分析。', 409)
        if config.builtin == 'mysql' and retrieval['tool'].endswith('__mysql_execute_query'):
            from .integrations.sql_policy import readonly_sql
            try:
                readonly_sql(retrieval['arguments'].get('command'), retrieval['arguments'].get('params'))
            except ValueError:
                raise PlatformError('sql_readonly', '过期数据库依据不是可重放的受限只读查询。', 403) from None
        actor = self.gateway.current_actor(Actor.model_validate(task['actor']))
        temporary = self.store.get('task', task['id'])['status'] != 'running' and not any(key[0] == task['id'] for key in self.mcp.workers)
        try:
            self.mcp.prepare(actor, task)
            response = self.gateway.invoke(actor, task['id'], retrieval['tool'], retrieval['arguments'])
        finally:
            if temporary:
                self.mcp.close_task(task['id'])
        found = next((item for item in response.get('evidence', []) if item.get('source_id') == evidence['source_id']), None)
        if found is None:
            message = '数据库查询结果已变化，不能沿用原依据；请基于新结果重新分析并审批。' if config.builtin == 'mysql' else '重新检索未能确认原引用内容未变，需要重新规划并审批。'
            self.store.event(task['id'], 'RemoteSourceRefreshRejected', {'source_id': evidence['source_id'], 'server': server, 'reason': 'content_changed_or_missing'})
            raise PlatformError('remote_source_changed', message, 409)
        fresh = self.store.get('evidence', evidence['source_id'])
        if fresh.get('expires_epoch', 0) <= time.time() or fresh['content_hash'] != evidence['content_hash'] or fresh['binding_hash'] != evidence['binding_hash']:
            raise PlatformError('remote_source_changed', '远程来源已变化，不能续用原审批依据。', 409)
        self.store.event(task['id'], 'RemoteSourceRefreshed', {'source_id': evidence['source_id'], 'server': server,
            'source_type': evidence['source_type'], 'tool': retrieval['tool'], 'content_unchanged': True})
        return fresh

    def _validate_sources(self, task, source_ids):
        project = self.store.get("project", task["project_id"])
        for source_id in source_ids:
            evidence = self.store.get("evidence", source_id)
            if evidence["task_id"] != task["id"] or evidence["project_id"] != project["id"]:
                raise PlatformError("source_denied", "引用来源不属于当前任务。", 403)
            if evidence["source_type"] == "request":
                target_id=evidence.get("metadata",{}).get("request_task_id",task["id"])
                targets={task['id'], *(t['id'] for t in self.conversations.predecessors(task))}
                if target_id not in targets:
                    raise PlatformError("source_denied","引用消息不属于当前对话。",403)
                target=self.store.get("task",target_id)
                if digest(target["user_input"].encode("utf-8"))!=evidence["content_hash"]:
                    raise PlatformError("source_changed","需求内容已变化，需要重新分析。",409)
                continue
            if evidence["source_type"] == "patch":
                patch = self.store.get("patch", evidence.get("metadata", {}).get("patch_id", ""))
                payload = json.loads(self.store.artifact_content(patch["artifact_id"]))
                change = next((item for item in payload["changes"] if item["path"] == evidence["path"]), None)
                if patch["task_id"] != task["id"] or patch["project_id"] != project["id"] or patch["id"] != task.get("patch_id") or digest(payload) != patch["hash"] or change is None or change["content"] is None or digest(change["content"].encode("utf-8")) != evidence["content_hash"]:
                    raise PlatformError("source_changed", "引用的补丁内容已变化，需要重新审查。", 409)
                continue
            if evidence.get("snapshot_id"):
                snapshot = json.loads(self.store.artifact_content(evidence["snapshot_id"]))
                if digest(snapshot.get("text", "").encode("utf-8")) != evidence["content_hash"] or self.mcp.binding_hash(evidence["server"], project) != evidence["binding_hash"]:
                    raise PlatformError("source_changed", "远程快照或连接配置已变化，需要重新检索并核对审批依据。", 409)
                if evidence['expires_epoch'] < time.time():
                    self._refresh_remote_source(task, project, evidence)
                continue
            path = safe_path(Path(project["root"]), evidence["path"])
            if digest(path.read_bytes()) != evidence["content_hash"]:
                raise PlatformError("source_changed", "引用的项目文件已变化，需要重新分析。", 409)

    def _context(self, state):
        task, actor, project = self._node(state["task_id"], "RETRIEVE_CONTEXT")
        self.gateway.hook("UserPromptSubmit", actor, task, {"input": task["user_input"]})
        self.gateway.hook("SessionStart", actor, task, {"session_id": task["session_id"], "project_id": task["project_id"]})
        listing = self.gateway.invoke(actor, task["id"], "list_files", {"limit": 100})
        collected = []
        for rule in project["rules"]:
            try:
                safe_path(Path(project["root"]), rule)
            except PlatformError as error:
                if error.code == "not_found":
                    continue
                raise
            collected.append(self.gateway.invoke(actor, task["id"], "read_file", {"path": rule, "limit": 100}))
        preferred = sorted(listing["files"], key=lambda name: (0 if name in {"calculator.py", "README.md"} else 1 if name.endswith((".py", ".java", ".ts")) else 2, name))
        for path in preferred[:3]:
            try:
                collected.append(self.gateway.invoke(actor, task["id"], "read_file", {"path": path, "limit": 100}))
            except PlatformError as error:
                if error.code not in {"encoding", "binary_file", "file_limit", "secret_in_file"}:
                    raise
        knowledge = self.gateway.invoke(actor, task["id"], "rag_search", {"query": task["user_input"][:300], "limit": 6})
        unknowns = knowledge.get("unknowns", [])
        remote_profiles = [name for name in project.get("mcp_servers", []) if name in self.settings.mcp_servers and self.settings.mcp_servers[name].enabled and self.settings.mcp_servers[name].builtin == "bailian"]
        from .knowledge_routing import retrieval_decision, TRANSIENT_KNOWLEDGE_ERRORS
        selected_profiles = []
        for name in remote_profiles:
            retrieve, reason = retrieval_decision(task['user_input'], required=self.settings.mcp_servers[name].required)
            self.store.event(task['id'], 'KnowledgeRetrievalDecision', {'server': name, 'retrieve': retrieve, 'reason': reason})
            if retrieve:
                selected_profiles.append(name)
        remote_profiles = selected_profiles
        warnings = []
        if remote_profiles:
            connected = self.mcp.prepare(actor, task)
            for name in remote_profiles:
                if not any(item["server"] == name and item["status"] == "connected" for item in connected):
                    if self.settings.mcp_servers[name].required:
                        raise PlatformError('mcp_unavailable', '项目必需的知识服务不可用，不能绕过规范继续。', 409)
                    warnings.append('可选知识服务 '+name+' 暂不可用，本轮使用本地依据，未取得远程规范。')
                    continue
                try:
                    response = self.gateway.invoke(actor, task["id"], "mcp__"+name+"__query_rag", {"query": task["user_input"]})
                    collected.extend(response['evidence'])
                except PlatformError as error:
                    if self.settings.mcp_servers[name].required or error.status == 403 or error.code not in TRANSIENT_KNOWLEDGE_ERRORS:
                        raise
                    warnings.append('可选知识服务 '+name+' 查询失败，本轮使用本地依据，未取得远程规范。')
                    self.store.event(task['id'], 'KnowledgeRetrievalDegraded', {'server': name, 'error_code': error.code, 'local_sources_only': True})
        unknowns.extend(warnings)
        self._set(task['id'], knowledge_warnings=warnings)
        collected.extend(knowledge["evidence"])
        if task["user_input"]:
            source_id="source_"+digest([task["id"],"request"])[:24]
            item={"id":source_id,"source_id":source_id,"task_id":task["id"],"project_id":project["id"],"source_type":"request","path":"用户需求","start_line":1,"end_line":max(1,len(task["user_input"].splitlines())),"content_hash":digest(task["user_input"].encode("utf-8")),"snippet":task["user_input"],"version":"request-v1","title":"本轮用户需求"}
            collected.append(self.store.put("evidence",item))
        context = {"files": listing["files"], "evidence": list({item["source_id"]: item for item in collected}.values()), "conflicts": knowledge["conflicts"], "unknowns": unknowns, "session_summary": self.store.get("session", task["session_id"]).get("summary", {})}
        context["conversation"]=self.conversations.context(task)
        # Session.summary is the latest session result, which may be newer than
        # a retried task. Use only the causally bounded history snapshot.
        context['session_summary'] = context['conversation']['previous_task_summary']
        context['project']={'name':project['name'],'root':project['root'],'allow_write':project['allow_write']}
        context['project_rules'] = project['rules']
        context['version_control']=repository_info(project)
        context['business_database']={'connection':self.business_mysql.contract(project),'state':self.business_mysql.public(project),'instructions':'本轮代码若依赖 MySQL，在计划中 requires_database=true。平台在实施范围确认后自动准备永久库、业务账号和 .env。代码读取已给定环境变量，无需向用户索要密码。已有连接配置会沿用。自动测试使用单独的临时数据库。'}
        if task["mode"] in {Mode.WRITE,Mode.PLAN,Mode.AUTO}:
            from .project_profiles import execution_context
            context["execution_environment"]=execution_context(project)
        prior_ids = list(dict.fromkeys(context['conversation']['prior_task_ids']+context['conversation'].get('memory_request_task_ids', [])))
        for index,prior_id in enumerate(prior_ids):
            prior=self.store.get("task",prior_id)
            source_id="source_"+digest([task["id"],"prior_request",prior_id])[:24]
            snippet = prior['user_input'][:12000] if prior_id in context['conversation']['prior_task_ids'] else prior['user_input'][:4000]
            item={"id":source_id,"source_id":source_id,"task_id":task["id"],"project_id":project["id"],"source_type":"request","path":"先前用户消息 "+str(index+1),"start_line":1,"end_line":max(1,len(prior["user_input"].splitlines())),"content_hash":digest(prior["user_input"].encode("utf-8")),"snippet":snippet,"version":"request-v1","title":"当前对话历史需求","metadata":{"request_task_id":prior_id}}
            evidence=self.store.put("evidence",item)
            collected.append(evidence)
            context["evidence"].append(evidence)
        if task["mode"] == Mode.REVIEW:
            change = self.gateway.invoke(actor, task["id"], "git_diff", {"base_ref": task.get("base_ref"), "head_ref": task.get("head_ref")})
            context["diff"] = change["diff"]
            context["diff_truncated"] = change.get("truncated", False)
        artifact = self.store.artifact(task, "context.json", json.dumps(context, ensure_ascii=False), "application/json")
        self._set(task["id"], evidence_ids=[item["source_id"] for item in collected])
        return {"context_id": artifact["id"]}

    def _read_context(self, state):
        return json.loads(self.store.artifact_content(state["context_id"]))

    def _respond(self,state):
        task,actor,project=self._node(state['task_id'],'RESPOND')
        if task['mode']!=Mode.AUTO:task=self._set(task['id'],mode=Mode.AUTO.value)
        result=self._engine(task).respond(actor,task,self._read_context(state))
        if result.action=='plan' and requests_implementation_after_approval(task['user_input']):
            self.store.event(task['id'],'IntentCorrected',{'model_action':'plan','action':'modify','reason':'implementation_after_user_approval'})
            result.action='modify'
        mode=Mode.READONLY if result.action=='answer' else Mode.WRITE if result.action=='modify' and project['allow_write'] else Mode.PLAN
        self._set(task['id'],mode=mode.value,intent=result.action)
        self.store.event(task['id'],'IntentResolved',{'action':result.action,'mode':mode.value})
        if result.answer:return self._answer({**state,'answer_draft':result.answer.model_dump()})
        return self._plan({**state,'plan_draft':result.plan.model_dump()})

    def _answer(self, state):
        task, actor, project = self._node(state["task_id"], "ANSWER")
        answer = AnswerDraft.model_validate(state['answer_draft']) if state.get('answer_draft') else self._engine(task).answer(actor, task, self._read_context(state))
        if not answer.findings and not answer.unknowns:
            raise PlatformError("evidence_required", "回答缺少证据或待确认事项。")
        for finding in answer.findings:
            if finding.kind == "fact" and not finding.source_ids:
                raise PlatformError("evidence_required", "事实性结论必须附有效来源。")
            self._validate_sources(task, finding.source_ids)
        if re.search(r"测试已通过|测试全部通过|已运行测试|I ran|tests passed", answer.answer, re.I):
            raise PlatformError("unverified_claim", "只读任务没有测试执行凭据，不能报告测试通过。")
        self._set(task["id"], answer=answer.model_dump())
        return {}

    def _baseline(self, project, paths):
        result = {}
        for name in paths:
            path = safe_path(Path(project["root"]), name, must_exist=False)
            if path.exists() and (not path.is_file() or path.stat().st_size > self.settings.max_file_bytes):
                raise PlatformError("file_limit", "批准文件超出文本修改限制。")
            result[name] = digest(path.read_bytes()) if path.exists() else None
        return result

    def _plan(self, state):
        task, actor, project = self._node(state["task_id"], "PLAN")
        context = self._read_context(state)
        context['version_control']=repository_info(project)
        self.gateway.hook("BeforePlan", actor, task, {"context_id": state["context_id"]})
        if context["conflicts"]:
            raise PlatformError("knowledge_conflict", "生效规范存在明确冲突，需要先确认适用规则。", 409)
        plan = PlanDraft.model_validate(state['plan_draft']) if state.get('plan_draft') else self._engine(task).plan(actor, task, context)
        from .intent import requests_database_deletion
        if requests_database_deletion(task['user_input']):
            plan.database_action = 'drop_project_database'
        operation = None
        if plan.database_action == 'drop_project_database':
            if not requests_database_deletion(task['user_input']):
                raise PlatformError('database_delete_intent', '删除数据库必须由用户在本轮明确提出，清空代码、表数据或仓库说明不能代替。', 403)
            if plan.requires_database or plan.powershell_commands:
                raise PlatformError('database_delete_plan', '删库由独立操作执行，不能同时自动建库或通过命令删库。')
            if task['mode'] == Mode.WRITE:
                enforce(self.gateway.policy(actor, task, project, 'drop_database', ['agent_app_'+project['id'].removeprefix('project_')]))
            operation = self.database_deletion.prepare(actor, task)
        plan.files = sorted(set(relative_path(name) for name in plan.files))
        for command in plan.powershell_commands:
            if SECRET.search(command.script):
                raise PlatformError("secret_in_command", "命令包含疑似凭证，不能进入计划。", 403)
            enforce(self.gateway.policy(actor, task, project, "run_powershell", ["."]))
        if task["mode"] == Mode.WRITE and plan.files:
            decision = self.gateway.policy(actor, task, project, "apply_patch", plan.files)
            self.store.event(task["id"], "PolicyChecked", {"action": "apply_patch", **decision})
            enforce(decision)
        if set(plan.files) & set(project["rules"]):
            raise PlatformError("rules_mutation", "项目规则由管理员维护，不能由任务修改。", 403)
        if not plan.source_ids:
            raise PlatformError("evidence_required", "计划尚未引用本轮需求或已读取资料，模型需要补充引用。")
        self._validate_sources(task, plan.source_ids)
        baselines = self._baseline(project, plan.files)
        from .intent import requests_project_reset
        if requests_project_reset(task['user_input']) and set(plan.files) == reset_paths(self.settings, project):
            plan.verification_mode = 'project_reset'
        if plan.verification_mode == 'project_reset':
            check_reset_plan(self.settings, task, project, plan.model_dump())
        revision=repository_info(project)['revision']
        payload = {**plan.model_dump(), "baseline": baselines, "rules_baseline": self._baseline(project, project["rules"]), "repo_revision": revision, "versions": task["versions"]}
        if operation:
            payload['database_deletion'] = self.database_deletion.public(operation)
        payload['verification_environment']={'network':'mysql_internal' if project.get('database_test_profile')=='mysql' else 'none','database':'temporary_mysql' if project.get('database_test_profile')=='mysql' else None}
        if plan.verification_mode == 'project_reset':
            payload['verification_environment'] = {'network': 'none', 'database': None, 'method': 'file_manifest'}
        plan_hash = digest(payload)
        row = self.store.put("plan", {"id": "plan_" + plan_hash[:32], "project_id": project["id"], "task_id": task["id"], "hash": plan_hash, **payload})
        self._set(task["id"], plan_id=row["id"], approved_files=[],database_required=bool(row.get('requires_database') or row.get('database_change')))
        if operation:
            self._set(task['id'], database_operation_id=operation['id'], database_operation=self.database_deletion.public(operation),
                      database_only=not bool(plan.files), database_required=False)
        self.gateway.hook("AfterPlan", actor, task, {"plan_id": row["id"], "hash": row["hash"]})
        return {"plan_id": row["id"], **({'database_operation_id': operation['id']} if operation else {})}

    def _check_baseline(self, task, plan, *, recovering=None):
        project = self.store.get("project", task["project_id"])
        scope_hash = digest([self.store.get("session", task["session_id"]).get("policy_rules", []), task.get("policy_rules", [])])
        if task["versions"].get("scope_policy_hash", scope_hash) != scope_hash:
            raise PlatformError("policy_changed", "会话或任务策略已变化，需要重新规划审批。", 409)
        if project["version"] != plan["versions"]["project"] or self.settings.versions()["config_hash"] != task["versions"]["config_hash"] or digest(system_prompt()) != task["versions"]["prompt_hash"]:
            raise PlatformError("configuration_changed", "项目配置、策略或提示词已变化，需要重新规划审批。", 409)
        current = self._baseline(project, plan["files"])
        if recovering is None:
            if current != plan["baseline"]:
                raise PlatformError("baseline_changed", "批准范围内的代码基线已变化，需要重新规划。", 409)
        else:
            after = {item["path"]: item["after_hash"] for item in recovering}
            if any(value not in {plan["baseline"][name], after.get(name, plan["baseline"][name])} for name, value in current.items()):
                raise PlatformError("recovery_conflict", "恢复过程中发现其他文件修改，已停止自动写入。", 409)
        if self._baseline(project, project["rules"]) != plan.get("rules_baseline", {}):
            raise PlatformError("rules_changed", "项目规则已变化，需要重新规划。", 409)
        sources = plan["source_ids"]
        if recovering is not None:
            changed = {item["path"] for item in recovering}
            sources = [source for source in sources if self.store.get("evidence", source)["path"] not in changed]
        self._validate_sources(task, sources)
        if plan.get("repo_revision") is not None and repository_info(project)['revision'] != plan["repo_revision"]:
            raise PlatformError("revision_changed", "仓库提交基线已变化，需要重新规划。", 409)

    @staticmethod
    def _approval_window_key(approval):
        return digest([approval['id'], approval['waiting_since'], approval.get('decision_revision', 0)])

    def _reconcile_legacy_approval_wait(self, task):
        """Repair only the legacy pending cycles retaining an older decision."""
        now = time.time()
        seen = set(task.get('accounted_approval_windows', []))
        failures, opened = [], None
        for event in sorted(self.store.list('event', task_id=task['id']), key=lambda row: row['created_at']):
            when = datetime.fromisoformat(event['created_at']).timestamp()
            if event['kind'] == 'TaskFailed' and opened is None:
                opened = when
            elif event['kind'] == 'TaskResumed' and opened is not None:
                failures.append((opened, when))
                opened = None
        if opened is not None:
            failures.append((opened, now))
        repairs = []
        for approval in self.store.list('approval', task_id=task['id']):
            snapshots = [*approval.get('decision_history', []), approval]
            for index, old in enumerate(snapshots[:-1]):
                if old.get('status') != 'pending' or old.get('decision_revision') or not old.get('decided_at'):
                    continue
                start = float(old['waiting_since'])
                if datetime.fromisoformat(old['decided_at']).timestamp() >= start:
                    continue
                key = self._approval_window_key(old)
                end = min(float(snapshots[index+1]['waiting_since']), now)
                start = max(start, task['started_epoch'])
                if key in seen or end <= start:
                    continue
                # Failure pauses have already been accounted for separately.
                clipped = sorted((max(start, a), min(end, b)) for a, b in failures if a < end and b > start)
                covered, cursor = 0, start
                for a, b in clipped:
                    covered += max(0, b-max(a, cursor))
                    cursor = max(cursor, b)
                seconds = max(0, end-start-covered)
                repairs.append({'approval_id': approval['id'], 'seconds': seconds, 'window_start': start, 'window_end': end})
                seen.add(key)
        if not repairs:
            return task
        seconds = sum(row['seconds'] for row in repairs)
        task = self._set(task['id'], approval_wait_seconds=task.get('approval_wait_seconds', 0)+seconds,
            legacy_approval_wait_seconds=task.get('legacy_approval_wait_seconds', 0)+seconds,
            accounted_approval_windows=sorted(seen))
        self.store.event(task['id'], 'LegacyApprovalWaitReconciled', {'seconds': seconds, 'windows': repairs})
        return task

    def _pending(self, task, kind, subject_id, subject_hash):
        approval_id = "approval_" + digest([task["id"], kind, subject_hash])[:32]
        try:
            approval = self.store.get("approval", approval_id)
        except PlatformError as error:
            if error.status != 404:
                raise
            approval = self.store.put("approval", {"id": approval_id, "task_id": task["id"], "project_id": task["project_id"], "kind": kind, "subject_id": subject_id, "subject_hash": subject_hash, "status": "pending", "expires_epoch": time.time() + self.settings.approval_seconds, "waiting_since": time.time()})
        self._set(task["id"], status="awaiting_" + kind, approval_id=approval_id)
        return approval

    def _renew_approval_record(self, task, approval):
        now = time.time()
        def renew(row):
            previous = {key: value for key, value in row.items() if key != 'decision_history'}
            clean = {key: value for key, value in row.items() if key not in {
                'reviewer_id', 'reviewer_roles', 'comment', 'decided_at', 'apply_to_project', 'database_name'}}
            return {**clean, 'decision_history': [*row.get('decision_history', []), previous],
                'status': 'pending', 'expires_epoch': now+self.settings.approval_seconds,
                'waiting_since': now, 'renewal_reason': 'expired', 'decision_revision': row.get('decision_revision', 0)+1}
        result = self.store.mutate('approval', approval['id'], renew)
        counted = task.get('counted_approvals', [])
        windows = set(task.get('accounted_approval_windows', []))
        window_key = self._approval_window_key(approval)
        waiting = max(0, now-approval['waiting_since']) if approval['status'] == 'pending' and window_key not in windows else 0
        if approval['status'] == 'pending':
            windows.add(window_key)
        if task['status'] == 'completed' and task.get('finished_epoch'):
            waiting += max(0, now-task['finished_epoch'])
        self._set(task['id'], status='awaiting_'+approval['kind'], current_node='APPROVE_'+approval['kind'].upper(),
            approval_id=approval['id'], error=None, failed_node=None, failure_category=None,
            finished_epoch=None, finished_at=None,
            approval_wait_seconds=task.get('approval_wait_seconds', 0)+waiting,
            counted_approvals=[item for item in counted if item != approval['id']],
            accounted_approval_windows=sorted(windows),
            approval_renewal={'reason': 'expired', 'kind': approval['kind'], 'subject_hash': approval['subject_hash'],
                'plan_hash': self.store.get('plan', task['plan_id'])['hash']})
        self.store.event(task['id'], 'ApprovalRenewalRequested', {'approval_id': approval['id'],
            'kind': approval['kind'], 'subject_hash': approval['subject_hash'], 'generated_patch_retained': bool(task.get('patch_id'))})
        return result

    def renew_approval(self, actor, approval_id):
        """Renew the viewing window, never an execution grant; require a new click."""
        actor.require('reviewer')
        approval = self.store.get('approval', approval_id)
        task = self.task(actor, approval['task_id'])
        lease = self.store.acquire(task['id'])
        try:
            task = self.task(actor, task['id'])
            approval = self.store.get('approval', approval_id)
            if task.get('approval_id') != approval_id or task['status'] != 'awaiting_'+approval['kind'] or approval['status'] != 'pending':
                raise PlatformError('stale_approval', '当前不是等待这份审批，请刷新。', 409)
            self.project(actor, task['project_id'], write=True)
            self._check_baseline(task, self.store.get('plan', task['plan_id']))
            if approval['kind'] == 'database':
                self.database_deletion.validate_pending(actor, self.store.get('database_operation', approval['subject_id']))
            if approval['kind'] == 'diff':
                from .checks import require_ai_check
                require_ai_check(task, self.store)
            if approval['expires_epoch'] >= time.time():
                return approval
            return self._renew_approval_record(task, approval)
        finally:
            self.mcp.close_task(task['id'])
            self.store.release(task['id'], lease)

    def _reopen_execution_approval(self, actor, task, graph, config):
        saved = graph.get_state(config).values
        current = self.store.get('approval', task['approval_id']) if task.get('approval_id') else None
        if task['status'].startswith('awaiting_') and current and current['status'] == 'approved' and current['expires_epoch'] >= time.time():
            return False
        # Prefer an expired final/database approval over an older plan grant.
        for kind in ('diff', 'database', 'plan'):
            approval_id = (current['id'] if current and current['kind'] == kind and current['status'] == 'approved'
                           else saved.get(kind+'_approval'))
            if not approval_id:
                continue
            approval = self.store.get('approval', approval_id)
            if approval['status'] != 'approved' or approval['expires_epoch'] >= time.time():
                continue
            if kind == 'database':
                operation = self.store.get('database_operation', approval['subject_id'])
                if operation['status'] != 'prepared':
                    continue
                self.database_deletion.validate_pending(actor, operation)
            self._check_baseline(task, self.store.get('plan', task['plan_id']))
            if kind == 'diff':
                from .checks import require_ai_check
                require_ai_check(task, self.store)
            if task['status'] == 'running':
                idle = max(0, time.time()-datetime.fromisoformat(task['updated_at']).timestamp())
                task = self._set(task['id'], approval_wait_seconds=task.get('approval_wait_seconds', 0)+idle)
            self._renew_approval_record(task, approval)
            upstream = {'plan': 'plan', 'diff': 'review', 'database': 'approve_plan'}[kind]
            graph.update_state(config, {'rejected': False}, as_node=upstream)
            return True
        return False

    def _approval(self, state, kind):
        task, actor, project = self._node(state["task_id"], "APPROVE_" + kind.upper())
        subject = self.store.get('database_operation', state['database_operation_id']) if kind == 'database' else self.store.get("plan" if kind == "plan" else "patch", state["plan_id"] if kind == "plan" else state["patch_id"])
        approval = self._pending(task, kind, subject["id"], subject["hash"])
        if kind == 'database' and approval['status'] != 'rejected':
            self.database_deletion.validate_pending(actor, subject)
        response = interrupt({"kind": kind, "approval_id": approval["id"], "subject_hash": subject["hash"]})
        saved = self.store.get("approval", approval["id"])
        if not isinstance(response, dict) or response.get("approval_id") != saved["id"] or saved["status"] not in {"approved", "rejected"}:
            raise PlatformError("approval_required", "没有后端审批记录。", 403)
        if saved["status"] == "approved" and saved["expires_epoch"] < time.time():
            raise PlatformError("approval_expired", "审批已过期。", 409)
        if kind == "plan" and saved["status"] == "approved":
            self._set(task["id"], approved_files=subject["files"])
        return {kind + "_approval": saved["id"], "rejected": saved["status"] == "rejected"}

    def execute_powershell(self, actor, task, project, script, timeout_seconds=30):
        actor.require("developer")
        plan = self.store.get("plan", task.get("plan_id", ""))
        self._check_baseline(task, plan)
        arguments = PowerShellArguments(script=script, timeout_seconds=timeout_seconds).model_dump()
        if arguments not in plan.get("powershell_commands", []):
            raise PlatformError("command_not_approved", "该 PowerShell 命令不在已批准计划内，需要重新规划审批。", 403)
        approval_id = "approval_" + digest([task["id"], "plan", plan["hash"]])[:32]
        approval = self.store.get("approval", approval_id)
        if approval['status'] == 'approved' and approval['expires_epoch'] < time.time():
            raise PlatformError('approval_expired', '原计划审批已过期，请重新确认后执行命令。', 409)
        if approval["status"] != "approved":
            raise PlatformError("approval_required", "PowerShell 命令需要有效的计划审批。", 403)
        if self.settings.powershell_image not in self.settings.sandbox_images:
            raise PlatformError("image_denied", "PowerShell 镜像未登记。", 403)
        run_id = "command_" + digest([task["id"], plan["hash"], arguments])[:32]
        previous = [item for item in self.store.list("command_run", task_id=task["id"]) if item["id"] == run_id]
        if previous:
            row = previous[0]
            return {**row, "stdout": self.store.artifact_content(row["stdout_id"]), "stderr": self.store.artifact_content(row["stderr_id"]), "reused": True}
        self.gateway.hook("BeforeCommand", actor, task, {"operation": "powershell", **arguments})
        def permitted(name, path):
            return self.gateway.source_allowed(actor, task, project, name) and not SECRET.search(path.read_bytes().decode("utf-8", errors="ignore"))
        workspace = self.command_workspace.prepare(task, project, [], allowed=permitted)
        try:
            result = self.powershell_runner.run(task, project, workspace, **arguments)
            self.gateway.current_actor(actor).require("developer")
            self._check_baseline(task, plan)
            self.gateway.hook("AfterCommand", actor, task, {"operation": "powershell", "exit_code": result.get("exit_code"), "status": result["status"]})
            if result["status"] == "unavailable":
                raise PlatformError("command_environment", result["reason"], 409)
            if Workspace.fingerprint(Path(workspace["path"])) != workspace["fingerprint"]:
                raise PlatformError("command_source_changed", "命令改变了只读工作区，结果不能采用。", 409)
            stdout = self.store.artifact(task, "powershell-stdout.log", result.pop("stdout", ""))
            stderr = self.store.artifact(task, "powershell-stderr.log", result.pop("stderr", ""))
            row = self.store.put("command_run", {"id": run_id, "task_id": task["id"], "project_id": project["id"], "plan_hash": plan["hash"], "arguments": arguments, "source_fingerprint": digest(workspace["fingerprint"]), "stdout_id": stdout["id"], "stderr_id": stderr["id"], **result})
            self.store.event(task["id"], "PowerShellExecuted", {"command_run_id": row["id"], "status": row["status"], "exit_code": row.get("exit_code")})
            return {**row, "stdout": self.store.artifact_content(stdout["id"]), "stderr": self.store.artifact_content(stderr["id"])}
        finally:
            self.command_workspace.cleanup(task["id"])

    def _commands(self, state):
        task = self.store.get("task", state["task_id"])
        plan = self.store.get("plan", state["plan_id"])
        if plan.get("powershell_commands"):
            task, actor, project = self._node(task["id"], "POWERSHELL_COMMANDS")
            for command in plan["powershell_commands"]:
                self.gateway.invoke(actor, task["id"], "run_powershell", command)
        return {}

    def _prepare_database(self,state):
        task,actor,project=self._node(state['task_id'],'PREPARE_DATABASE')
        plan=self.store.get('plan',state['plan_id'])
        if plan.get('database_action') == 'drop_project_database':return {}
        if not (plan.get('requires_database') or plan.get('database_change') is True):return {}
        result=self.gateway.controlled(actor,task['id'],'prepare_database',state['plan_approval'],plan['hash'],lambda:self.business_mysql.ensure(actor,project['id']))
        self.store.event(task['id'],'ProjectDatabasePrepared',result)
        return {}

    def _delete_database(self, state):
        task, actor, project = self._node(state['task_id'], 'DELETE_DATABASE')
        plan = self.store.get('plan', state['plan_id'])
        self._check_baseline(task, plan)
        operation = self.store.get('database_operation', state['database_operation_id'])
        try:
            result = self.gateway.controlled(actor, task['id'], 'drop_database', state['database_approval'], operation['hash'],
                lambda: self.database_deletion.execute(actor, task, operation['id']))
            self._set(task['id'], database_result=result)
        finally:
            self._set(task['id'], database_operation=self.database_deletion.public(self.store.get('database_operation', operation['id'])))
        return {}

    def _patch(self, state):
        task, actor, project = self._node(state["task_id"], "GENERATE_PATCH")
        plan = self.store.get("plan", state["plan_id"])
        self._check_baseline(task, plan)
        context = {**self._read_context(state), "approved_plan": plan}
        context['business_database']={'connection':self.business_mysql.contract(project),'state':self.business_mysql.public(project)}
        context["powershell_results"] = [{**row, "stdout": self.store.artifact_content(row["stdout_id"])[:12000], "stderr": self.store.artifact_content(row["stderr_id"])[:12000]} for row in self.store.list("command_run", task_id=task["id"])]
        if state.get("verification")=='failed':
            failure = self.store.get('sandbox_run', task['verification']['id'])
            if failure['task_id'] != task['id'] or failure['project_id'] != project['id'] or failure['patch_id'] != task['patch_id']:
                raise PlatformError('verification_scope', '返修测试记录不属于当前补丁。', 409)
            context['test_failures'] = {**failure, 'log_read_instructions':
                'stdout/stderr 只是首尾预览。完整日志使用 read_tool_result(artifact_id=stdout_id 或 stderr_id, offset, length) 分页读取，优先查看末尾失败摘要；不要通过 read_file 读取平台产物目录。'}
            for stream in ('stdout', 'stderr'):
                content = self.store.artifact_content(failure[stream+'_id'])
                context['test_failures'][stream] = content if len(content) <= 12000 else content[:2000]+'\n[…中间日志省略，可按产物 ID 分页读取完整内容…]\n'+content[-9000:]
                context['test_failures'][stream+'_characters'] = len(content)
                context['test_failures'][stream+'_truncated'] = len(content) > 12000
            context["repair_attempt"] = state["fix_count"]
        if state.get('review_status')=='blocked':
            context['review_failures']=task.get('review')
            context['repair_attempt']=state.get('review_fix_count',0)
        previous=None
        if (state.get('verification')=='failed' or state.get('review_status')=='blocked') and task.get('patch_id'):
            previous=self.store.get('patch',task['patch_id'])
            context['previous_patch']=json.loads(self.store.artifact_content(previous['artifact_id']))
            if digest(context['previous_patch'])!=previous['hash']:
                raise PlatformError('patch_changed','返修补丁已变化，请重新检查。',409)
        repair_workspace=None
        try:
            if previous:
                repair_workspace=Workspace(self.settings,namespace='review-workspaces')
                repair_workspace.prepare(task,project,context['previous_patch']['changes'])
                task=self._set(task['id'],review_patch_id=previous['id'])
            proposal = self._engine(task).patch(actor, task, context)
        finally:
            if repair_workspace:
                repair_workspace.cleanup(task['id'])
                self._set(task['id'],review_patch_id=None)
        proposed_names=set()
        for change in proposal.changes:
            name=relative_path(change.path)
            if name not in plan['files'] or name in proposed_names:
                raise PlatformError('scope_drift','补丁包含未批准或重复的文件。',403)
            proposed_names.add(name)
        if previous:
            merged={c['path']:FileChange(path=c['path'],content=c['content']) for c in context['previous_patch']['changes']}
            merged.update({c.path:c for c in proposal.changes})
            proposal=proposal.model_copy(update={'changes':list(merged.values())})
        changes, paths, diff = [], set(), []
        for change in proposal.changes:
            name = relative_path(change.path)
            if name not in plan["files"] or name in paths:
                raise PlatformError("scope_drift", "补丁包含未批准或重复的文件。", 403)
            paths.add(name)
            path = safe_path(Path(project["root"]), name, must_exist=False)
            before = path.read_bytes().decode("utf-8") if path.exists() else None
            after = change.content
            if after is not None and SECRET.search(after):
                raise PlatformError("secret_in_patch", "补丁中包含疑似明文凭证。", 403)
            if name.endswith(".py") and after is not None:
                after_tree = ast.parse(after, filename=name)
                before_tree = ast.parse(before.lstrip("\ufeff")) if before else None
                def assertions(tree):
                    return sum(isinstance(item, ast.Assert) or isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute) and item.func.attr.startswith("assert") for item in ast.walk(tree)) if tree else 0
                def tests(tree):
                    return {item.name for item in ast.walk(tree) if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test")} if tree else set()
                if "test" in name and (re.search(r"@.*skip|expectedFailure|pytest\.skip|pytest\.xfail|assert\s+True|assertTrue\(True\)|self\.skipTest", after) or assertions(after_tree) < assertions(before_tree) or not tests(before_tree).issubset(tests(after_tree))):
                    raise PlatformError("test_weakened", "自动修改不能减少断言或跳过测试。", 403)
            if after == before:
                continue
            changes.append({"path": name, "before": before, "content": after, "before_hash": plan["baseline"][name], "after_hash": digest(after.encode()) if after is not None else None})
            for line in difflib.unified_diff((before or "").splitlines(keepends=True), (after or "").splitlines(keepends=True), fromfile="a/" + name if before is not None else "/dev/null", tofile="b/" + name if after is not None else "/dev/null"):
                diff.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
        if not changes:
            raise PlatformError("empty_patch", "未生成有效修改。")
        payload = {"changes": changes, "plan_hash": plan["hash"], "explanation": proposal.explanation}
        patch_hash = digest(payload)
        artifact = self.store.artifact(task, "patch.json", json.dumps(payload, ensure_ascii=False), "application/json")
        diff_artifact = self.store.artifact(task, "changes.diff", "".join(diff), "text/x-diff")
        row = self.store.put("patch", {"id": "patch_" + digest([task["id"], patch_hash])[:32], "task_id": task["id"], "project_id": project["id"], "hash": patch_hash, "plan_id": plan["id"], "artifact_id": artifact["id"], "diff_artifact_id": diff_artifact["id"], "files": sorted(paths), "status": "proposed"})
        self._set(task["id"], patch_id=row["id"],verification=None,review=None,ai_check=None)
        return {"patch_id": row["id"],"verification":"not_run","review_status":"not_run"}

    def _verify(self, state):
        task, actor, project = self._node(state["task_id"], "SANDBOX_VERIFY")
        plan = self.store.get("plan", state["plan_id"])
        patch = self.store.get("patch", state["patch_id"])
        self._check_baseline(task, plan)
        payload = json.loads(self.store.artifact_content(patch["artifact_id"]))
        if digest(payload) != patch['hash']:
            raise PlatformError('patch_changed', '待验证补丁已变化。', 409)
        self.gateway.hook("BeforeFileEdit", actor, task, {"operation": "workspace", "paths": plan["files"], "patch_hash": patch["hash"]})
        workspace = self.gateway.controlled(actor, task["id"], "apply_patch", state["plan_approval"], plan["hash"], lambda: self.workspace.prepare(task, project, payload["changes"]))
        self.gateway.hook("AfterFileEdit", actor, task, {"operation": "workspace", "paths": plan["files"], "patch_hash": patch["hash"]})
        if is_reset(task, plan):
            result = self.gateway.controlled(actor, task['id'], 'run_tests', state['plan_approval'], plan['hash'],
                lambda: verify_reset(self.settings, task, project, plan, payload['changes'], workspace))
        else:
            self.gateway.hook("BeforeCommand", actor, task, {"command": project["test_command"], "workspace": workspace["kind"]})
            result = self.gateway.controlled(actor, task["id"], "run_tests", state["plan_approval"], plan["hash"], lambda: self.runner.verify(task, project, workspace))
            self.gateway.hook("AfterCommand", actor, task, {"exit_code": result.get("exit_code"), "status": result["status"]})
        if Workspace.fingerprint(Path(workspace["path"])) != workspace["fingerprint"]:
            result.update(status="failed", reason="验证程序修改了源码区域，结果不可导出。")
        stdout = self.store.artifact(task, "stdout.log", result.pop("stdout", ""))
        stderr = self.store.artifact(task, "stderr.log", result.pop("stderr", ""))
        run = self.store.put("sandbox_run", {"task_id": task["id"], "project_id": project["id"], "patch_id": patch["id"], "workspace_kind": workspace["kind"], "stdout_id": stdout["id"], "stderr_id": stderr["id"], **result})
        self.store.event(task["id"], "AfterTest", {"sandbox_run_id": run["id"], "status": run["status"]})
        self._set(task["id"], verification=run)
        self.gateway.hook("AfterTest", actor, task, {"sandbox_run_id": run["id"], "result": run})
        self.workspace.cleanup(task["id"])
        return {"verification": run["status"], "fix_count": state.get("fix_count", 0) + (run["status"] == "failed")}

    def _wait_environment(self, state):
        self._set(state["task_id"], status="waiting_environment", current_node="WAIT_ENVIRONMENT", environment_waiting_since=time.time())
        interrupt({"kind": "environment", "message": "请准备 Docker 和登记镜像后恢复任务。"})
        return {}

    def _review(self, state):
        task, actor, project = self._node(state["task_id"], "CODE_REVIEW")
        context = self._read_context(state)
        if state.get("patch_id"):
            patch = self.store.get("patch", state["patch_id"])
            context["diff"] = self.store.artifact_content(patch["diff_artifact_id"])
            context["verification"] = task.get("verification")
            approved_plan = self.store.get('plan', patch['plan_id'])
            context['approved_plan'] = {key: approved_plan[key] for key in ('goal', 'files', 'steps', 'test_plan', 'verification_mode') if key in approved_plan}
        changed = diff_lines(context.get('diff', ''))
        context['review_locations'] = location_ranges(changed)
        review_workspace = None
        workspace_manager = Workspace(self.settings, namespace="review-workspaces")
        try:
            if state.get("patch_id"):
                self._check_baseline(task, self.store.get("plan", patch["plan_id"]))
                payload = json.loads(self.store.artifact_content(patch["artifact_id"]))
                if digest(payload) != patch["hash"]:
                    raise PlatformError("patch_changed", "待审查补丁已变化。", 409)
                review_workspace = workspace_manager.prepare(task, project, payload["changes"])
                task = self._set(task["id"], review_patch_id=patch["id"])
            review = self._engine(task).review(actor, task, context)
            issue_error = review_error(review, changed)
            if issue_error:
                raise PlatformError('review_scope', issue_error)
            for issue in review.issues:
                self._validate_sources(task, issue.source_ids)
            blocked_review=any(item.severity=='blocking' for item in review.issues)
            run=self.store.put('review_run',{'task_id':task['id'],'project_id':project['id'],'patch_id':state.get('patch_id'),'status':'blocked' if blocked_review else 'passed','review':review.model_dump()})
            self._set(task["id"], review=review.model_dump(),ai_check={'status':run['status'],'patch_id':state.get('patch_id'),'patch_hash':patch['hash'] if state.get('patch_id') else None,'review_run_id':run['id']})
            self.store.event(task['id'],'AIReviewCompleted',{'status':run['status'],'review_run_id':run['id']})
        finally:
            if review_workspace:
                workspace_manager.cleanup(task["id"])
                self._set(task["id"], review_patch_id=None)
        return {'review_status':run['status'],'review_fix_count':state.get('review_fix_count',0)+(1 if blocked_review else 0)}

    @staticmethod
    def _diff_lines(text):
        return {path: sides['new'] for path, sides in diff_lines(text).items()}

    def _summary(self, state):
        task = self.store.get("task", state["task_id"])
        self.gateway.check_cancelled(task['id'])
        actor = self.gateway.current_actor(Actor.model_validate(task["actor"]))
        actor.require("developer")
        if task.get('database_only') and task['mode'] == Mode.WRITE:
            status = 'cancelled' if state.get('rejected') or task['status'] == 'cancelled' else 'completed'
            result = task.get('database_result') or {}
            summary = {'goal': task['user_input'], 'mode': task['mode'], 'engine': task['engine'], 'test_status': 'not_run',
                'database_result': result, 'applied_to_project': False, 'final_response': {'message':
                '本次数据库删除已取消。' if status == 'cancelled' else '已删除本项目数据库 '+result.get('database', '')+'，并清理两个专属账号、本地连接配置和保存的项目凭证。操作记录已保留。', 'next_steps': []}}
            self.gateway.hook('TaskCompleted', actor, task, {'status': status, 'summary': summary})
            finished = time.time()
            self._set(task['id'], status=status, current_node='DONE', summary=summary, finished_at=timestamp(), finished_epoch=finished,
                execution_ms=round(max(0, finished-task['started_epoch']-task.get('approval_wait_seconds', 0))*1000))
            self.store.mutate('session', task['session_id'], lambda row: {**row, 'summary': {'previous_task_id': task['id'], **summary}})
            self.store.event(task['id'], 'TaskCompleted', {'status': status, 'database_status': result.get('status')})
            return {}
        status = "cancelled" if state.get("rejected") or task["status"] == "cancelled" else "failed" if state.get("verification") == "failed" or task['mode']==Mode.WRITE and state.get('review_status')=='blocked' else "completed"
        summary = {"goal": task["user_input"], "mode": task["mode"], "engine": task["engine"], "plan_id": state.get("plan_id"), "patch_id": state.get("patch_id"), "test_status": (task.get("verification") or {}).get("status", "not_run"), "applied_to_project": False, "source_ids": task.get("evidence_ids", []), "unknowns": (task.get("answer") or {}).get("unknowns", [])}
        if task.get('database_result'):
            summary['database_result'] = task['database_result']
        if status=='completed' and task['mode']==Mode.WRITE:
            from .checks import require_ai_check
            require_ai_check(task,self.store)
            context={'files':self.store.get('patch',task['patch_id'])['files'],'verification':task.get('verification'),'ai_check':task.get('ai_check'),'review':task.get('review'),'database':self.business_mysql.public(self.store.get('project',task['project_id'])),'written_to_project':False}
            context['database_deletion'] = task.get('database_result')
            try:
                final=self._engine(task).finalize(actor,task,context)
                summary['final_response']=final.model_dump()
                summary['final_response_source']='model' if task['engine']=='model' else 'demo'
            except (PlatformError,AttributeError):
                summary['final_response']={'message':'本轮修改已生成并通过验证和 AI 代码检查。','next_steps':[]}
                summary['final_response_source']='execution_facts'
                self.store.event(task['id'],'FinalSummaryFallback',{'reason':'模型总结暂不可用，使用实际执行事实。'})
        self.gateway.hook("TaskCompleted", actor, task, {"status": status, "summary": summary})
        finished_epoch = time.time()
        self._set(task["id"], status=status, current_node="DONE", summary=summary, finished_at=timestamp(), finished_epoch=finished_epoch,
                  execution_ms=round(max(0, finished_epoch - task["started_epoch"] - task.get("approval_wait_seconds", 0)) * 1000))
        if status == "failed" and state.get("verification") == "failed":
            self._set(task["id"], failure_category="tests", failed_node="SANDBOX_VERIFY", error={"code":"tests_failed","message":"隔离验证未通过，已达到自动修复上限，请查看报告并重新规划。"})
        elif status=='failed' and state.get('review_status')=='blocked':
            self._set(task['id'],failure_category='ai_check',failed_node='CODE_REVIEW',error={'code':'ai_check_blocked','message':'AI 代码检查仍有阻断问题，已达到自动修复上限。改动已保留，未进入人工最终确认，也未写回项目。'})
        session = self.store.get("session", task["session_id"])
        session["summary"] = {"previous_task_id": task["id"], **summary}
        self.store.put("session", session)
        self.store.event(task["id"], "TaskCompleted", {"status": status, "test_status": summary["test_status"]})
        return {}

    def _graph(self, saver, mode):
        graph = StateGraph(TaskState)
        for name, node in {"context": self._context,'respond':self._respond, "answer": self._answer, "plan": self._plan, "approve_plan": lambda state: self._approval(state, "plan"), 'approve_database':lambda state:self._approval(state,'database'), 'delete_database':self._delete_database, "database":self._prepare_database,"commands": self._commands, "patch": self._patch, "verify": self._verify, "wait_environment": self._wait_environment, "review": self._review, "approve_diff": lambda state: self._approval(state, "diff"), "summary": self._summary}.items():
            graph.add_node(name, node)
        graph.add_edge(START, "context")
        resolved=lambda state:self.store.get('task',state['task_id'])['mode']
        graph.add_edge("context", 'respond' if mode==Mode.AUTO else "answer" if mode == Mode.READONLY else "review" if mode == Mode.REVIEW else "plan")
        planned = lambda state: 'summary' if resolved(state) != Mode.WRITE else 'approve_database' if self.store.get('task', state['task_id']).get('database_only') else 'approve_plan'
        graph.add_conditional_edges('respond', planned)
        graph.add_edge("answer", "summary")
        graph.add_conditional_edges('plan', planned)
        def after_plan_approval(state):
            if state.get('rejected'):
                return 'summary'
            operation_id = state.get('database_operation_id')
            if operation_id and self.store.get('database_operation', operation_id)['status'] != 'completed':
                return 'approve_database'
            return 'database'
        graph.add_conditional_edges("approve_plan", after_plan_approval)
        graph.add_conditional_edges('approve_database', lambda state:'summary' if state.get('rejected') else 'delete_database')
        graph.add_conditional_edges('delete_database', lambda state:'summary' if self.store.get('task', state['task_id']).get('database_only') else 'database')
        graph.add_edge('database','commands')
        graph.add_edge("commands", "patch")
        graph.add_edge("patch", "verify")
        graph.add_conditional_edges("verify", lambda state: "wait_environment" if state["verification"] == "unavailable" else "review" if state["verification"] == "passed" else "patch" if state.get("fix_count", 0) < 3 else "summary")
        graph.add_edge("wait_environment", "verify")
        graph.add_conditional_edges('review',lambda state:'summary' if resolved(state)!=Mode.WRITE else 'patch' if state.get('review_status')=='blocked' and state.get('review_fix_count',0)<3 else 'summary' if state.get('review_status')=='blocked' else 'approve_diff')
        graph.add_edge("approve_diff", "summary")
        graph.add_edge("summary", END)
        return graph.compile(checkpointer=saver)

    @contextmanager
    def _saver(self):
        if self.settings.database_url and self.settings.database_url.startswith("postgresql"):
            try:
                from langgraph.checkpoint.postgres import PostgresSaver
            except ImportError:
                raise PlatformError("postgres_dependency", "请安装 postgres 可选依赖。") from None
            with PostgresSaver.from_conn_string(self.settings.database_url.replace("postgresql+psycopg://", "postgresql://")) as saver:
                saver.setup()
                yield saver
        else:
            with SqliteSaver.from_conn_string(str(self.settings.data_dir / "checkpoints.sqlite")) as saver:
                yield saver

    def run(self, actor, task_id, *, resume=False):
        task = self.task(actor, task_id)
        self.conversations.ensure_turn_available(task)
        lease = self.store.acquire(task_id)
        run_started = False
        try:
            task = self.store.get('task', task_id)
            if task["status"] in {"completed", "cancelled"}:
                return task
            self.gateway.check_cancelled(task_id)
            if task["status"] == "queued":
                task = self._set(task_id, started_epoch=time.time())
            command = None
            if resume and task["status"] in {"awaiting_plan", "awaiting_diff", "awaiting_database"}:
                approval = self.store.get("approval", task["approval_id"])
                if approval["status"] not in {"approved", "rejected"}:
                    raise PlatformError("approval_required", "请先审批当前计划或补丁。", 403)
                command = Command(resume={"approval_id": approval["id"]})
            elif resume and task["status"] == "waiting_environment":
                if task.get("environment_waiting_since"):
                    task = self._set(task_id, approval_wait_seconds=task["approval_wait_seconds"] + time.time() - task["environment_waiting_since"], environment_waiting_since=None)
                command = Command(resume=True)
            elif task["status"] != "queued" and not resume:
                raise PlatformError("resume_required", "任务已经启动，请使用恢复操作。", 409)
            if resume and task["status"] == "failed":
                failed_at=task.get('failure_waiting_since') or task.get('finished_epoch') or datetime.fromisoformat(task['updated_at']).timestamp()
                paused=max(0,time.time()-failed_at)
                task=self._set(task_id, error=None, failed_node=None, failure_category=None,
                               approval_wait_seconds=task.get('approval_wait_seconds',0)+paused,
                               failure_wait_seconds=task.get('failure_wait_seconds',0)+paused,
                               failure_waiting_since=None,finished_epoch=None,finished_at=None)
                self.store.event(task_id,'TaskResumed',{'reason':'failure','paused_seconds':round(paused,3)})
            task = self._reconcile_legacy_approval_wait(task)
            run_started = True
            with self._saver() as saver:
                graph = self._graph(saver, task["mode"])
                config = {"configurable": {"thread_id": task_id}, "recursion_limit": task['budget'].get('max_graph_steps',200)}
                if resume and self._reopen_execution_approval(actor, task, graph, config):
                    command = None
                try:
                    graph.invoke(command if resume else {"task_id": task_id, "fix_count": 0,"review_fix_count":0}, config)
                except PlatformError as error:
                    if error.code != 'approval_expired' or not self._reopen_execution_approval(actor, self.store.get('task', task_id), graph, config):
                        raise
                    # Publish the normal interrupt immediately: no retry click,
                    # no model call and no execution under an expired grant.
                    graph.invoke(None, config)
            return self.store.get("task", task_id)
        except PlatformError as error:
            if self.store.get("task",task_id).get('cancel_requested') or self.store.get("task",task_id)["status"]=="cancelled":
                self.store.event(task_id,"TaskStopObserved",{"code":error.code})
                return self.store.get("task",task_id)
            if error.code not in {"approval_required", "resume_required", "task_busy"}:
                current = self.store.get("task", task_id)
                category = "budget" if "budget" in error.code else "context" if any(item in error.code for item in ("source", "evidence", "knowledge", "baseline")) else "permission" if error.status == 403 else "model" if error.code.startswith("model") else "workflow"
                self._set(task_id, status="failed", failed_node=current["current_node"], failure_category=category, failure_waiting_since=time.time(),error={"code": error.code, "message": error.message})
                self.store.event(task_id, "TaskFailed", {"code": error.code, "message": error.message})
            raise
        except Exception as error:
            current = self.store.get("task", task_id)
            if current.get('cancel_requested'):
                return current
            self._set(task_id, status="failed", failed_node=current["current_node"], failure_category="execution",failure_waiting_since=time.time(), error={"code": "execution_failed", "message": "任务执行失败。", "type": type(error).__name__})
            self.store.event(task_id, "TaskFailed", {"code": "execution_failed", "type": type(error).__name__})
            raise PlatformError("execution_failed", "任务执行失败，请查看审计记录。") from error
        finally:
            try:
                if run_started:
                    current = self.store.get("task", task_id)
                    try:
                        self.gateway.hook("SessionEnd", actor, current, {"session_id": current["session_id"], "status": current["status"]})
                    except PlatformError as cleanup_error:
                        if current["status"] not in {"failed", "cancelled"}:
                            self._set(task_id, status="failed", error={"code": cleanup_error.code, "message": cleanup_error.message})
                            raise
            finally:
                try:
                    self.mcp.close_task(task_id)
                finally:
                    try:
                        if self.store.get('task', task_id).get('cancel_requested'):
                            self._complete_cancellation(task_id)
                    finally:
                        self.store.release(task_id, lease)

    def decide(self, actor, approval_id, decision: ApprovalDecision):
        try:
            return self._decide(actor, approval_id, decision)
        except PlatformError as error:
            try:
                approval = self.store.get("approval", approval_id)
                self.store.event(approval["task_id"], "ApprovalDenied", {"user_id": actor.user_id, "approval_id": approval_id, "code": error.code})
            except PlatformError:
                pass
            raise

    def _decide(self, actor, approval_id, decision: ApprovalDecision):
        actor.require("reviewer")
        approval = self.store.get("approval", approval_id)
        task = self.task(actor, approval["task_id"])
        if approval.get('decision_revision', 0) and decision.decision_revision != approval['decision_revision']:
            raise PlatformError('approval_revision_changed', '审批已刷新，请查看当前审批卡后重新确认；旧页面的确认请求不能重放。', 409)
        if approval['kind'] == 'database':
            self.database_deletion.require_actor(actor)
            if approval['status'] == 'pending' and decision.decision == 'approved':
                operation = self.store.get('database_operation', approval['subject_id'])
                if decision.database_name != operation['target']['database']:
                    raise PlatformError('database_name_confirmation', '确认请求必须包含页面展示的完整目标库名。', 403)
                if operation['task_id'] != task['id'] or operation['id'] != task.get('database_operation_id') or operation['hash'] != approval['subject_hash']:
                    raise PlatformError('database_target_changed', '数据库确认目标不匹配。', 409)
                self.database_deletion.validate_pending(actor, operation)
        if decision.apply_to_project and (approval['kind']!='diff' or decision.decision!='approved'):
            raise PlatformError('delivery_intent','只有接受具体文件改动时才能请求写入项目。')
        if approval['kind']=='diff' and decision.decision=='approved':
            from .checks import require_ai_check
            require_ai_check(task,self.store)
        self.project(actor, task["project_id"], write=True)
        plan = self.store.get("plan", task["plan_id"])
        self._check_baseline(task, plan)
        if approval["expires_epoch"] < time.time():
            raise PlatformError("approval_expired", "审批已过期，请重新发起计划。", 409)
        if task.get("approval_id") != approval_id:
            raise PlatformError("stale_approval", "这不是任务当前等待的审批。", 409)
        def save(row):
            if row["status"] != "pending":
                if row["status"] == decision.decision and row.get("reviewer_id") == actor.user_id:
                    if bool(row.get('apply_to_project'))!=decision.apply_to_project:raise PlatformError('approval_decided','审批已经处理，请刷新查看当前写入状态。',409)
                    return row
                raise PlatformError("approval_decided", "该审批已经处理。", 409)
            row.update(status=decision.decision, reviewer_id=actor.user_id, reviewer_roles=sorted(actor.roles), comment=redact(decision.comment), decided_at=timestamp(),apply_to_project=decision.apply_to_project,
                       expires_epoch=time.time()+self.settings.approval_seconds)
            if approval['kind'] == 'database':row['database_name'] = decision.database_name
            return row
        result = self.store.mutate("approval", approval_id, save)
        if decision.apply_to_project:self._set(task['id'],delivery={'status':'pending'})
        def wait(row):
            window_key = self._approval_window_key(approval)
            if approval['status'] == 'pending' and window_key not in row.get('accounted_approval_windows', []):
                row["approval_wait_seconds"] += time.time() - approval["waiting_since"]
                row['accounted_approval_windows'] = [*row.get('accounted_approval_windows', []), window_key]
                row["counted_approvals"] = list(dict.fromkeys([*row.get("counted_approvals", []), approval_id]))
            return row
        self.store.mutate("task", task["id"], wait)
        self.store.event(task["id"], "ApprovalDecided", {"approval_id": approval_id, "decision": decision.decision, "reviewer_id": actor.user_id})
        return result

    def deliver_approved_patch(self,actor,task_id):
        task=self.task(actor,task_id)
        if task['status']!='completed' or task['mode']!=Mode.WRITE or not task.get('approval_id'):return task
        approval=self.store.get('approval',task['approval_id'])
        if approval['kind']!='diff' or approval['status']!='approved' or not approval.get('apply_to_project'):return task
        if (task.get('apply_operation') or {}).get('status')=='rolled_back':return task
        if task.get('summary',{}).get('applied_to_project'):return self._set(task_id,delivery={'status':'applied'})
        self._set(task_id,delivery={'status':'writing'})
        try:
            self.apply_patch(actor,task_id)
            return self._set(task_id,delivery={'status':'applied'})
        except Exception as error:
            current=self.store.get('task',task_id)
            if current.get('summary',{}).get('applied_to_project'):return self._set(task_id,delivery={'status':'applied'})
            if isinstance(error,PlatformError) and error.code=='task_busy':return current
            if isinstance(error, PlatformError) and error.code == 'approval_expired' and not current.get('apply_operation'):
                # An accepted patch can sit idle before delivery. Reopen the
                # exact Diff, not a new plan; never reset a partial-write journal.
                self.conversations.ensure_turn_available(current)
                lease = self.store.acquire(task_id)
                try:
                    with self._saver() as saver:
                        graph = self._graph(saver, current['mode'])
                        config = {'configurable': {'thread_id': task_id}, 'recursion_limit': current['budget'].get('max_graph_steps', 200)}
                        if self._reopen_execution_approval(actor, current, graph, config):
                            self._set(task_id, delivery={'status': 'awaiting_approval'})
                            graph.invoke(None, config)
                            return self.store.get('task', task_id)
                finally:
                    self.mcp.close_task(task_id)
                    self.store.release(task_id, lease)
            detail={'code':getattr(error,'code','write_failed'),'message':getattr(error,'message','保存项目文件失败，请查看记录后重试。')}
            self.store.event(task_id,'ProjectWriteFailed',detail)
            return self._set(task_id,delivery={'status':'failed','error':detail})

    def export_patch(self, actor, task_id):
        task = self.task(actor, task_id)
        if task["status"] != "completed" or task.get("summary", {}).get("test_status") != "passed":
            raise PlatformError("not_verified", "修改尚未完成审批和验证。", 409)
        from .checks import require_ai_check
        require_ai_check(task,self.store)
        patch = self.store.get("patch", task["patch_id"])
        plan = self.store.get("plan", task["plan_id"])
        changes = json.loads(self.store.artifact_content(patch["artifact_id"]))["changes"] if task.get("applied_patch_hash") == patch["hash"] else None
        self._check_baseline(task, plan, recovering=changes)
        return self.gateway.controlled(actor, task_id, "export_patch", task["approval_id"], patch["hash"], lambda: {"artifact_id": patch["diff_artifact_id"], "diff": self.store.artifact_content(patch["diff_artifact_id"])})

    @staticmethod
    def _atomic_content(target, content):
        if content is None:
            target.unlink(missing_ok=True)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + "." + identifier("write") + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def _project_write_lock(self, project_id):
        token = self.store.acquire(project_id, kind="project")
        try:
            with self.apply_lock:
                yield
        finally:
            self.store.release(project_id, token, kind="project")

    def apply_patch(self, actor, task_id):
        actor.require("developer")
        task = self.task(actor, task_id)
        patch = self.store.get("patch", task["patch_id"])
        if task.get("applied_patch_hash") == patch["hash"]:
            return {"status": "already_applied", "patch_id": patch["id"]}
        project = self.project(actor, task["project_id"], write=True)
        plan = self.store.get("plan", task["plan_id"])
        payload = json.loads(self.store.artifact_content(patch["artifact_id"]))
        operation = task.get("apply_operation")
        recovering = operation and operation["hash"] == patch["hash"] and operation["status"] == "applying"
        if recovering:
            journal = json.loads(self.store.artifact_content(operation["journal_id"]))
            if digest(journal) != patch["hash"]:
                raise PlatformError("journal_changed", "恢复日志不匹配已审批补丁。", 409)
            self._check_baseline(task, plan, recovering=payload["changes"])
        else:
            self.export_patch(actor, task_id)

        def apply():
            lease = self.store.acquire(task_id)
            try:
                with self._project_write_lock(project["id"]):
                    self._check_baseline(task, plan, recovering=payload["changes"] if recovering else None)
                    current_task = self.store.get("task", task_id)
                    if recovering:
                        journal_id = current_task["apply_operation"]["journal_id"]
                    else:
                        journal = self.store.artifact(task, "rollback.json", json.dumps(payload, ensure_ascii=False), "application/json")
                        journal_id = journal["id"]
                        self._set(task_id, apply_operation={"hash": patch["hash"], "journal_id": journal_id, "status": "applying"})
                    self.gateway.hook("BeforeFileEdit", actor, task, {"operation": "apply", "paths": plan["files"], "patch_hash": patch["hash"]})
                    for change in payload["changes"]:
                        # 每一步按实际文件 hash 恢复，重复请求不再次应用已经写入的内容。
                        self.gateway.check_cancelled(task_id)
                        self.project(actor, task["project_id"], write=True)
                        target = safe_path(Path(project["root"]), change["path"], must_exist=False)
                        actual = digest(target.read_bytes()) if target.exists() else None
                        if actual == change["after_hash"]:
                            continue
                        if actual != change["before_hash"]:
                            raise PlatformError("recovery_conflict", "文件已被其他操作修改，写入已停止。", 409)
                        self._atomic_content(target, change["content"])
                    self._set(task_id, applied_patch_hash=patch["hash"], rollback_artifact_id=journal_id,
                              apply_operation={"hash": patch["hash"], "journal_id": journal_id, "status": "applied"},
                              summary={**task["summary"], "applied_to_project": True})
                    self.store.event(task_id, "PatchApplied", {"patch_id": patch["id"], "rollback_artifact_id": journal_id})
                    self.gateway.hook("AfterFileEdit", actor, task, {"operation": "apply", "paths": plan["files"], "patch_hash": patch["hash"]})
                    return {"status": "applied", "patch_id": patch["id"], "rollback_artifact_id": journal_id}
            finally:
                self.store.release(task_id, lease)
        return self.gateway.controlled(actor, task_id, "apply_patch", task["approval_id"], patch["hash"], apply)

    def rollback_patch(self, actor, task_id):
        actor.require("developer")
        task = self.task(actor, task_id)
        project = self.project(actor, task["project_id"], write=True)
        patch = self.store.get("patch", task["patch_id"])
        operation = task.get("apply_operation", {})
        if operation.get("status") == "rolled_back":
            return {"status": "already_rolled_back"}
        if operation.get("status") not in {"applied", "applying", "rolling_back"}:
            raise PlatformError("not_applied", "该任务没有可恢复的应用记录。", 409)
        payload = json.loads(self.store.artifact_content(operation["journal_id"]))
        if digest(payload) != patch["hash"]:
            raise PlatformError("journal_changed", "回滚记录与补丁不匹配。", 409)
        def rollback():
            lease = self.store.acquire(task_id)
            try:
                with self._project_write_lock(project["id"]):
                    for change in payload["changes"]:
                        target = safe_path(Path(project["root"]), change["path"], must_exist=False)
                        actual = digest(target.read_bytes()) if target.exists() else None
                        allowed = {change["before_hash"], change["after_hash"]} if operation["status"] in {"applying", "rolling_back"} else {change["after_hash"]}
                        if actual not in allowed:
                            raise PlatformError("rollback_conflict", "文件在应用后又被修改，不能自动覆盖。", 409)
                    self._set(task_id, apply_operation={**operation, "status": "rolling_back"})
                    self.gateway.hook("BeforeFileEdit", actor, task, {"operation": "rollback", "paths": [item["path"] for item in payload["changes"]]})
                    for change in reversed(payload["changes"]):
                        self.project(actor, task["project_id"], write=True)
                        target = safe_path(Path(project["root"]), change["path"], must_exist=False)
                        self._atomic_content(target, change["before"])
                    self._set(task_id, applied_patch_hash=None, apply_operation={**operation, "status": "rolled_back"},
                              summary={**task["summary"], "applied_to_project": False})
                    self.store.event(task_id, "PatchRolledBack", {"patch_id": patch["id"]})
                    self.gateway.hook("AfterFileEdit", actor, task, {"operation": "rollback", "patch_hash": patch["hash"]})
                    return {"status": "rolled_back", "patch_id": patch["id"]}
            finally:
                self.store.release(task_id, lease)
        return self.gateway.controlled(actor, task_id, "apply_patch", task["approval_id"], patch["hash"], rollback)

    def feedback(self, actor, task_id, category, comment):
        task = self.task(actor, task_id)
        return self.store.put("feedback", {"task_id": task_id, "project_id": task["project_id"], "user_id": actor.user_id, "category": category, "comment": redact(comment)})

    def close(self):
        self.mcp.close()
        self.model_io.close()
        self.store.close()

    def inspect_tools(self, actor, project_id, *, call=None, arguments=None):
        task = self.create_task(actor, TaskInput(project_id=project_id, user_input="检查当前项目的工具连接", engine="model"))
        self._set(task["id"], kind="connector_check")
        try:
            servers = self.mcp.prepare(actor, task)
            from .tool_inventory import inventory
            capabilities = inventory(self, actor, self.project(actor, project_id), servers)
            for server in capabilities["servers"]:
                config = self.settings.mcp_servers[server["server"]]
                if config.builtin == "mysql" and server["status"] == "connected" and server["connection"]["databases"]:
                    alias = "mcp__" + server["server"] + "__mysql_execute_query"
                    if alias not in server["tools"]:
                        continue
                    database = server["connection"]["databases"][0]
                    try:
                        self.gateway.invoke(actor,task["id"],alias,{"command":"SELECT 1 AS connection_ok","database":database})
                        server.update(database_status="connected",database_checked=database)
                    except PlatformError as error:
                        server.update(database_status="unavailable",database_error=error.code)
            self._set(task["id"], tool_inventory=capabilities)
            result = self.gateway.invoke(actor, task["id"], call, arguments or {}) if call else None
            self._set(task["id"], status="completed", finished_at=timestamp(), finished_epoch=time.time(), summary={"kind": "connector_check", "test_status": "not_run", "model_called": False})
            self.store.event(task["id"], "ConnectorCheckCompleted", {**capabilities, "model_called": False})
            return {"task_id": task["id"], **capabilities, "result": result}
        except PlatformError as error:
            self._set(task["id"], status="failed", error={"code": error.code, "message": error.message})
            raise
        finally:
            self.mcp.close_task(task["id"])
