from __future__ import annotations

import hmac
import json
import os
import ipaddress
from urllib.parse import urlparse
from typing import Literal
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from importlib.resources import files

from fastapi import Depends, FastAPI, HTTPException, Request, Query
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, SecretStr

from .config import Settings
from .models import Actor, ApprovalDecision, DEFAULT_PROJECT_TOKEN_BUDGET, MAX_TOKEN_BUDGET, LOCAL_ACTOR, PlatformError, ProjectInput, TaskInput
from .service import Platform


class ProjectUpdate(BaseModel):
    enabled: bool | None = None
    allow_write: bool | None = None
    mcp_servers: list[str] | None = None
    test_command: list[str] | None = None
    sandbox_image: str | None = None
    database_test_profile: str | None = None
    policy_rules: list[dict] | None = None
    team: str | None = None
    token_budget:int|None=Field(default=None,ge=100,le=MAX_TOKEN_BUDGET)


class TaskBudgetUpdate(BaseModel):
    max_tokens:int|None=Field(default=None,ge=100,le=MAX_TOKEN_BUDGET)
    follow_project:bool=False


class FeedbackInput(BaseModel):
    category: str = Field(max_length=50)
    comment: str = Field(max_length=4000)


class FolderSelectionInput(BaseModel):
    path: str = Field(min_length=1,max_length=4096)


class SessionInput(BaseModel):
    project_id: str
    title: str = Field(default="",max_length=100)


class SessionRename(BaseModel):
    title: str = Field(min_length=1,max_length=100)


class ChatInput(BaseModel):
    content: str = Field(min_length=1,max_length=12000)
    mode: str = "auto"
    engine: str = "model"
    client_request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")


class FolderCreateInput(BaseModel):
    parent: str = Field(min_length=1,max_length=4096)
    name: str = Field(min_length=1,max_length=80)


class MySQLInstanceInput(BaseModel):
    host: str = Field(default='127.0.0.1',max_length=253)
    port: int = Field(default=3306,ge=1,le=65535)
    user: str = Field(min_length=1,max_length=32)
    password: SecretStr
    auto_provision: bool = True


def create_app(settings: Settings | None = None, *, platform: Platform | None = None):
    owned = platform is None
    platform = platform or Platform(settings or Settings.load())
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ai-agent")
    local_token = platform.store.local_token()
    bearer = HTTPBearer(auto_error=False)

    @asynccontextmanager
    async def lifespan(app):
        for task in platform.store.list('task'):
            if task['status'] == 'cancelling' and not platform.store.has_live_lease(task['id']):
                executor.submit(platform.finish_cancellation, task['id'])
            if task['status']=='completed' and (task.get('delivery') or {}).get('status') in {'pending','writing'}:
                actor=resolve_owner(task['owner_id'])
                if actor is not None:executor.submit(background,actor,task['id'],True)
                else:platform._set(task['id'],delivery={'status':'failed','error':{'code':'identity_revoked','message':'任务发起人的权限已不可用，文件未继续保存。'}})
            if task['status'] == 'running' and task.get('database_operation_id'):
                operation = platform.store.get('database_operation', task['database_operation_id'])
                if operation['status'] in {'dropping', 'database_deleted', 'accounts_deleted'}:
                    actor = resolve_owner(task['owner_id'])
                    if actor is not None:executor.submit(background, actor, task['id'], True)
        yield
        executor.shutdown(wait=True, cancel_futures=True)
        if owned:
            platform.close()

    from . import __version__
    app = FastAPI(title="AI 研发任务工作台", version=__version__, lifespan=lifespan)
    app.state.platform = platform

    def configured_actors():
        try:
            actors = {key: Actor.model_validate(value) for key, value in json.loads(os.getenv("AI_AGENT_USERS_JSON", "{}")).items()}
            identities = {}
            for actor in actors.values():
                if actor.user_id == LOCAL_ACTOR.user_id or actor.user_id in identities and identities[actor.user_id] != actor:
                    raise ValueError("reserved or conflicting identity")
                identities[actor.user_id] = actor
            return actors
        except (ValueError, TypeError):
            raise HTTPException(503, "账号配置不可用。") from None

    def identity(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if credentials is None:
            raise HTTPException(401, "请输入访问口令。")
        token = credentials.credentials
        if hmac.compare_digest(token, local_token):
            return LOCAL_ACTOR
        for candidate, actor in configured_actors().items():
            if hmac.compare_digest(token, candidate):
                return actor
        raise HTTPException(401, "访问口令无效。")

    def resolve_owner(user_id):
        if user_id == LOCAL_ACTOR.user_id:
            return LOCAL_ACTOR
        return next((actor for actor in configured_actors().values() if actor.user_id == user_id), None)
    platform.actor_resolver = resolve_owner

    def background(actor, task_id, resume=False):
        try:
            platform.run(actor, task_id, resume=resume)
            platform.deliver_approved_patch(actor,task_id)
        except PlatformError:
            pass  # 失败已保存到任务和审计中，不向日志写入凭证或原始异常响应。

    @app.exception_handler(PlatformError)
    async def platform_error(request, error):
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": {"code": error.code, "message": error.message}}, status_code=error.status)

    @app.get("/", response_class=HTMLResponse)
    def index():
        return files("code_agent_platform").joinpath("web/index.html").read_text(encoding="utf-8")

    @app.get("/workbench-features.js")
    def workbench_features():
        return Response(files("code_agent_platform").joinpath("web/workbench-features.js").read_text(encoding="utf-8"), media_type="application/javascript")

    @app.get("/chat.js")
    def chat_script():
        return Response(files("code_agent_platform").joinpath("web/chat.js").read_text(encoding="utf-8"),media_type="application/javascript")

    @app.get("/workbench.css")
    def workbench_style():
        return Response(files("code_agent_platform").joinpath("web/workbench.css").read_text(encoding="utf-8"),media_type="text/css")

    @app.get('/turns.js')
    def turn_script():
        return Response(files('code_agent_platform').joinpath('web/turns.js').read_text(encoding='utf-8'),media_type='application/javascript')

    @app.get('/budgets.js')
    def budget_script():
        return Response(files('code_agent_platform').joinpath('web/budgets.js').read_text(encoding='utf-8'),media_type='application/javascript')

    @app.get("/health")
    def health():
        return {"status": "ok", "version": __version__}

    @app.get("/api/me")
    def me(actor: Actor = Depends(identity)):
        return actor.model_dump(mode="json")

    @app.get("/api/environment")
    def environment(actor: Actor = Depends(identity)):
        host = urlparse(platform.settings.model_url).hostname
        configured = host in {"localhost", "127.0.0.1", "::1"} or platform.settings.allow_remote_model and bool(os.getenv(platform.settings.model_key_env))
        return {"docker": platform.runner.health(), "model": platform.settings.model_name, "model_configured": configured, "pricing_configured": platform.settings.input_price_per_million is not None and platform.settings.output_price_per_million is not None, "versions": platform.settings.versions(),"mysql_test_network":platform.settings.allow_mysql_test_network and "ai-agent/spring-mysql-it:0.1" in platform.settings.sandbox_images}

    @app.post("/api/projects/{project_id}/tools/check")
    def check_tools(project_id: str, actor: Actor = Depends(identity)):
        return platform.inspect_tools(actor, project_id)

    @app.get("/api/project-folders")
    def project_folders(request: Request, path: str | None = None, actor: Actor = Depends(identity)):
        actor.require("admin")
        from .project_folders import browse_folders
        if platform.settings.allow_local_project_selection:
            require_local_control(request,actor)
        return browse_folders(platform.settings,path,local=platform.settings.allow_local_project_selection)

    def require_local_control(request,actor):
        try:
            local=request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
        except ValueError:
            local=False
        if not local or actor.user_id!=LOCAL_ACTOR.user_id:
            raise PlatformError("local_control_required","请在本机以本地管理员身份执行此操作。",403)

    @app.get('/api/mysql-instance')
    def mysql_instance(actor:Actor=Depends(identity)):
        actor.require('admin')
        return platform.business_mysql.instance()

    @app.put('/api/mysql-instance')
    def configure_mysql(data:MySQLInstanceInput,request:Request,actor:Actor=Depends(identity)):
        require_local_control(request,actor)
        return platform.business_mysql.configure(actor,data.host,data.port,data.user,data.password.get_secret_value(),data.auto_provision)

    @app.get('/api/projects/{project_id}/database')
    def project_database(project_id:str,actor:Actor=Depends(identity)):
        project=platform.project(actor,project_id)
        return {'state':platform.business_mysql.public(project),'connection':platform.business_mysql.contract(project)}

    @app.post('/api/projects/{project_id}/database/prepare')
    def prepare_project_database(project_id:str,actor:Actor=Depends(identity)):
        actor.require('admin')
        return platform.business_mysql.ensure(actor,project_id)

    @app.post("/api/project-selections",status_code=201)
    def select_project_folder(data: FolderSelectionInput,request: Request,actor: Actor=Depends(identity)):
        require_local_control(request,actor)
        choice=platform.select_local_project(actor,data.path)
        return {"selection_id":choice["id"],"root":choice["root"],"expires_epoch":choice["expires_epoch"]}

    @app.post("/api/project-folders/create",status_code=201)
    def create_project_folder(data:FolderCreateInput,request:Request,actor:Actor=Depends(identity)):
        require_local_control(request,actor)
        actor.require("admin")
        if not platform.settings.allow_local_project_selection:
            raise PlatformError("folder_selection_disabled","本机项目创建尚未启用。",403)
        from .project_folders import create_folder
        folder=create_folder(data.parent,data.name)
        choice=platform.select_local_project(actor,str(folder))
        return {"selection_id":choice["id"],"root":choice["root"],"expires_epoch":choice["expires_epoch"]}

    @app.get("/api/sessions")
    def sessions(project_id:str,actor:Actor=Depends(identity)):
        return platform.conversations.list(actor,project_id)

    @app.get('/api/library')
    def library(view:Literal['archived','trash']='archived',actor:Actor=Depends(identity)):
        return platform.library.list(actor, view)

    @app.get('/api/library/projects/{project_id}')
    def library_project(project_id:str,actor:Actor=Depends(identity)):
        return platform.library.project_detail(actor, project_id)

    @app.get('/api/library/sessions/{session_id}')
    def library_session(session_id:str,before:str|None=None,actor:Actor=Depends(identity)):
        return platform.library.session_detail(actor, session_id, before=before)

    @app.get('/library.js')
    def library_script():
        return Response(files('code_agent_platform').joinpath('web/library.js').read_text(encoding='utf-8'), media_type='application/javascript')

    @app.post('/api/library/{kind}/{record_id}/{action}')
    def library_change(kind:Literal['project','session'],record_id:str,action:Literal['archive','delete','restore'],actor:Actor=Depends(identity)):
        return platform.library.change(actor, kind, record_id, action)

    @app.post("/api/sessions",status_code=201)
    def new_session(data:SessionInput,actor:Actor=Depends(identity)):
        return platform.conversations.describe(platform.conversations.create(actor,data.project_id,data.title))

    @app.get("/api/sessions/{session_id}")
    def session_detail(session_id:str,before:str|None=None,limit:int=Query(default=100,ge=1,le=300),actor:Actor=Depends(identity)):
        return platform.conversations.detail(actor,session_id,before=before,limit=limit)

    @app.patch("/api/sessions/{session_id}")
    def rename_session(session_id:str,data:SessionRename,actor:Actor=Depends(identity)):
        return platform.conversations.rename(actor,session_id,data.title)

    @app.post("/api/sessions/{session_id}/messages",status_code=202)
    def chat(session_id:str,data:ChatInput,actor:Actor=Depends(identity)):
        session=platform.conversations.session(actor,session_id)
        # TaskInput keeps mode/engine validation at the shared API boundary.
        from pydantic import ValidationError
        try:
            request_data=TaskInput(project_id=session["project_id"],session_id=session_id,user_input=data.content,mode='auto' if data.mode=='approval_write' else data.mode,engine=data.engine,client_request_id=data.client_request_id)
        except ValidationError:
            raise PlatformError("chat_input","请选择有效的工作模式和处理方式。") from None
        task=platform.create_task(actor,request_data)
        if task["status"]=="queued":executor.submit(background,actor,task["id"])
        return task

    @app.get("/api/projects/{project_id}/tools")
    def project_tools(project_id: str, actor: Actor = Depends(identity)):
        from .tool_inventory import inventory
        return inventory(platform,actor,platform.project(actor,project_id))

    @app.get("/api/projects")
    def projects(actor: Actor = Depends(identity)):
        return platform.projects(actor)

    @app.post("/api/projects", status_code=201)
    def add_project(data: ProjectInput, actor: Actor = Depends(identity)):
        return platform.register_project(actor, data)

    @app.patch("/api/projects/{project_id}")
    def update_project(project_id: str, data: ProjectUpdate, actor: Actor = Depends(identity)):
        return platform.update_project(actor, project_id, **data.model_dump(exclude_unset=True))

    @app.get('/api/projects/{project_id}/token-budget')
    def project_token_budget(project_id:str,actor:Actor=Depends(identity)):
        return platform.token_budget(actor,project_id)

    @app.patch('/api/tasks/{task_id}/token-budget')
    def task_token_budget(task_id:str,data:TaskBudgetUpdate,actor:Actor=Depends(identity)):
        if not data.follow_project and 'max_tokens' not in data.model_fields_set:
            raise PlatformError('task_budget','请选择跟随项目，或明确设置本轮额度。')
        return platform.update_task_token_budget(actor,task_id,**data.model_dump())

    @app.post("/api/demo")
    def demo(actor: Actor = Depends(identity)):
        return platform.create_demo(actor)

    @app.get("/api/tasks")
    def tasks(project_id: str, actor: Actor = Depends(identity)):
        platform.project(actor, project_id)
        return sorted(platform.store.list("task", project_id=project_id), key=lambda item: item["created_at"], reverse=True)

    @app.post("/api/tasks", status_code=202)
    def submit(data: TaskInput, actor: Actor = Depends(identity)):
        task = platform.create_task(actor, data)
        executor.submit(background, actor, task["id"])
        return task

    @app.get("/api/tasks/{task_id}")
    def detail(task_id: str, actor: Actor = Depends(identity)):
        return platform.detail(actor, task_id)

    @app.post("/api/tasks/{task_id}/resume", status_code=202)
    def resume(task_id: str, actor: Actor = Depends(identity)):
        task = platform.task(actor, task_id)
        platform.conversations.ensure_turn_available(task)
        if task["status"] in {"awaiting_plan", "awaiting_diff", "awaiting_database"} and platform.store.get("approval", task["approval_id"])["status"] == "pending":
            raise PlatformError("approval_required", "请先完成审批。", 403)
        executor.submit(background, actor, task_id, True)
        return {"status": "scheduled", "task_id": task_id}

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel(task_id: str, actor: Actor = Depends(identity)):
        return platform.cancel(actor, task_id)

    @app.post('/api/approvals/{approval_id}/renew')
    def renew_approval(approval_id: str, request: Request, actor: Actor = Depends(identity)):
        if platform.store.get('approval', approval_id)['kind'] == 'database':
            require_local_control(request, actor)
        return platform.renew_approval(actor, approval_id)

    @app.post("/api/approvals/{approval_id}", status_code=202)
    def decide(approval_id: str, decision: ApprovalDecision, request: Request, actor: Actor = Depends(identity)):
        if platform.store.get('approval', approval_id)['kind'] == 'database':
            require_local_control(request, actor)
        approval = platform.decide(actor, approval_id, decision)
        executor.submit(background, actor, approval["task_id"], True)
        return approval

    @app.post("/api/tasks/{task_id}/apply")
    def apply(task_id: str, actor: Actor = Depends(identity)):
        return platform.apply_patch(actor, task_id)

    @app.post("/api/tasks/{task_id}/rollback")
    def rollback(task_id: str, actor: Actor = Depends(identity)):
        return platform.rollback_patch(actor, task_id)

    @app.post("/api/projects/{project_id}/index")
    def index_project(project_id: str, actor: Actor = Depends(identity)):
        from .graph import CodeGraph
        return CodeGraph(platform).build(actor, project_id)

    @app.get("/api/projects/{project_id}/graph")
    def graph(project_id: str, symbol: str, direction: str = "callees", depth: int = 3, actor: Actor = Depends(identity)):
        from .graph import CodeGraph
        return CodeGraph(platform).query(actor, project_id, symbol, direction, depth)

    @app.get("/api/tasks/{task_id}/export")
    def export(task_id: str, actor: Actor = Depends(identity)):
        return platform.export_patch(actor, task_id)

    @app.get("/api/artifacts/{artifact_id}", response_class=PlainTextResponse)
    def artifact(artifact_id: str, actor: Actor = Depends(identity)):
        record = platform.store.get("artifact", artifact_id)
        platform.task(actor, record["task_id"])
        return platform.store.artifact_content(artifact_id)

    @app.post("/api/tasks/{task_id}/feedback")
    def feedback(task_id: str, data: FeedbackInput, actor: Actor = Depends(identity)):
        return platform.feedback(actor, task_id, data.category, data.comment)

    @app.get("/api/metrics")
    def metrics(project_id: str, actor: Actor = Depends(identity)):
        platform.project(actor, project_id)
        tasks = [item for item in platform.store.list("task", project_id=project_id) if item.get("kind") != "connector_check"]
        total_cost = sum(item.get("usage", {}).get("cost") or 0 for item in tasks)
        verified = sum(item.get("summary", {}).get("test_status") == "passed" for item in tasks)
        return {"tasks": len(tasks), "completed": sum(item["status"] == "completed" for item in tasks), "verified": verified, "failed": sum(item["status"] == "failed" for item in tasks), "input_tokens": sum(item.get("usage", {}).get("input_tokens", 0) for item in tasks), "output_tokens": sum(item.get("usage", {}).get("output_tokens", 0) for item in tasks), "reported_cost": total_cost, "unpriced_tasks": sum(item.get("usage", {}).get("cost") is None for item in tasks), "cost_per_verified_task": total_cost / verified if verified and all(item.get("usage", {}).get("cost") is not None for item in tasks) else None, "demo_tasks": sum(item["engine"] == "demo" for item in tasks)}

    return app
