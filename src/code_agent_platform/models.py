from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator,model_validator


def identifier(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value: Any) -> str:
    if isinstance(value, bytes):
        payload = value
    else:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


class PlatformError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


class Mode(StrEnum):
    AUTO = 'auto'
    READONLY = "readonly"
    PLAN = "plan_only"
    WRITE = "approval_write"
    REVIEW = "ci_review"


class Actor(BaseModel):
    model_config = ConfigDict(frozen=True)
    user_id: str
    roles: frozenset[str] = frozenset({"developer"})
    projects: frozenset[str] = frozenset()

    def allows(self, project_id: str) -> bool:
        return "admin" in self.roles or project_id in self.projects or "*" in self.projects

    def require(self, role: str) -> None:
        if role not in self.roles and "admin" not in self.roles:
            raise PlatformError("role_denied", "当前账号没有执行此操作的权限。", 403)


LOCAL_ACTOR = Actor(user_id="local", roles=frozenset({"admin", "developer", "reviewer"}), projects=frozenset({"*"}))


MAX_TOKEN_BUDGET = 9_007_199_254_740_991
DEFAULT_PROJECT_TOKEN_BUDGET = 2_000_000


class Budget(BaseModel):
    max_tool_calls: int = Field(default=200, ge=1, le=2000)
    max_model_calls: int = Field(default=60, ge=1, le=500)
    max_tokens: int | None = Field(default=DEFAULT_PROJECT_TOKEN_BUDGET, ge=100, le=MAX_TOKEN_BUDGET)
    max_seconds: int = Field(default=3600, ge=5, le=3600)
    max_graph_steps: int = Field(default=200, ge=10, le=2000)
    max_cost: float | None = Field(default=None, gt=0)


class ProjectInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    root: str
    selection_id: str | None = Field(default=None, pattern=r"^selection_[0-9a-f]{32}$")
    enabled: bool = True
    allow_write: bool = False
    token_budget: int | None = Field(default=DEFAULT_PROJECT_TOKEN_BUDGET,ge=100,le=MAX_TOKEN_BUDGET)
    rules: list[str] = Field(default_factory=lambda: ["AGENTS.md", ".agent/rules.md"])
    knowledge_paths: list[str] = Field(default_factory=lambda: ["docs"])
    test_command: list[str] = Field(default_factory=lambda: ["python", "-m", "unittest", "discover", "-s", "tests"])
    sandbox_image: str = "python:3.12-slim"
    database_test_profile: Literal["none", "mysql"] = "none"
    version: str = "1"
    mcp_servers: list[str] = Field(default_factory=lambda: ["project_files"])
    team: str = "default"
    policy_rules: list[dict] = Field(default_factory=list)

    @field_validator("policy_rules")
    @classmethod
    def valid_policies(cls, value):
        from .policy import PolicyRule
        return [PolicyRule.model_validate(item).model_dump() for item in value]

    @field_validator("test_command")
    @classmethod
    def finite_command(cls, value):
        if not value or len(value) > 40 or any(not item or "\x00" in item or len(item) > 2000 for item in value):
            raise ValueError("测试命令必须是有长度限制的参数列表。")
        if value[0] not in {"python", "python3", "pytest", "mvn", "npm", "node", "gradle", "pwsh"}:
            raise ValueError("未登记的测试程序。")
        return value


class TaskInput(BaseModel):
    project_id: str
    user_input: str = Field(min_length=1, max_length=12_000)
    mode: Mode = Mode.READONLY
    session_id: str | None = None
    client_request_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{8,64}$")
    engine: Literal["model", "demo"] = "model"
    budget: Budget = Field(default_factory=Budget)
    base_ref: str | None = None
    head_ref: str | None = None
    policy_rules: list[dict] = Field(default_factory=list)

    @field_validator("policy_rules")
    @classmethod
    def valid_policies(cls, value):
        from .policy import PolicyRule
        return [PolicyRule.model_validate(item).model_dump() for item in value]

    @field_validator("base_ref", "head_ref")
    @classmethod
    def valid_ref(cls, value):
        return validate_git_ref(value)


class Evidence(BaseModel):
    source_id: str
    project_id: str
    task_id: str
    source_type: Literal["code", "document", "rule", "graph", "git", "remote_document", "database", "patch", "request"]
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    content_hash: str
    snippet: str
    version: str
    title: str = ""
    status: str = "active"
    topic: str = ""
    priority: int = 0


class Finding(BaseModel):
    claim: str
    source_ids: list[str] = Field(default_factory=list,description='fact 必须引用本轮实际提供的 source_id。用户消息事实可引用 source_type=request；源码事实引用实际读取的代码。')
    kind: Literal["fact", "inference", "unknown"] = "fact"


class AnswerDraft(BaseModel):
    answer: str
    findings: list[Finding] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)


class PowerShellArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    script: str = Field(min_length=1, max_length=6000)
    timeout_seconds: int = Field(default=30, ge=1, le=120)


class PlanDraft(BaseModel):
    goal: str
    files: list[str] = Field(min_length=0, max_length=20)
    steps: list[str] = Field(min_length=1, max_length=20)
    source_ids: list[str] = Field(default_factory=list,description='必须引用本轮实际提供的 source_id。用户需求也是合法的规划依据；空项目无需预先存在代码或规范文件。')
    risks: list[str] = Field(default_factory=list)
    test_plan: str
    verification_mode: Literal['project_tests', 'project_reset'] = Field(default='project_tests', description='正常开发使用 project_tests。仅在用户明确要求清空整个项目全部受管文件时使用 project_reset：全部文件 content=null，保留受保护配置及规则，平台核对文件清单，不运行已删除项目的构建。')
    rollback_plan: str
    database_change: bool | None = None
    requires_database: bool = False
    compatibility_change: bool | None = None
    powershell_commands: list[PowerShellArguments] = Field(default_factory=list, max_length=5)
    database_action: Literal['none', 'drop_project_database'] = Field(default='none', description='仅当用户明确要求删除本项目数据库时使用 drop_project_database。只删除数据库时 files=[]；执行前平台单独展示实际库名、表范围并等待人工确认，模型不能指定其他库。清空表数据、删除单表、清空代码均不等于删库。')

    @model_validator(mode='after')
    def valid_operation(self):
        if not self.files and self.database_action == 'none':
            raise ValueError('普通计划需要文件范围；仅删除数据库时可 files=[]。')
        if self.database_action == 'drop_project_database' and (self.requires_database or self.powershell_commands):
            raise ValueError('删库由平台独立操作，不得同时自动建库或通过 PowerShell 执行。')
        return self


class FileChange(BaseModel):
    path: str
    content: str | None = Field(max_length=256_000)


class TurnDraft(BaseModel):
    action:Literal['answer','plan','modify']=Field(description='answer 为普通提问；modify 为要求开发或修改，包括先给计划、等待确认后执行；plan 仅用于明确只看方案、不要求后续执行。modify 仍须平台人工审批，绝不立即写文件。')
    answer:AnswerDraft|None=None
    plan:PlanDraft|None=None

    @model_validator(mode='after')
    def matching_result(self):
        if self.action=='answer' and (self.answer is None or self.plan is not None):raise ValueError('回答应只提供 answer。')
        if self.action!='answer' and (self.plan is None or self.answer is not None):raise ValueError('计划或修改应只提供 plan。')
        return self


class PatchDraft(BaseModel):
    changes: list[FileChange] = Field(min_length=1, max_length=20)
    explanation: str


class ReviewIssue(BaseModel):
    severity: Literal["blocking", "warning", "suggestion"]
    path: str
    line: int = Field(ge=1)
    side: Literal['new', 'old'] = Field(default='new', description='new 为 Diff 中新增/修改后的行；删除相关问题使用 old 和删除前的行号。只能定位实际变更行。')
    title: str
    explanation: str
    source_ids: list[str] = Field(default_factory=list)


class ReviewDraft(BaseModel):
    issues: list[ReviewIssue] = Field(default_factory=list)
    summary: str


class CompletionDraft(BaseModel):
    message: str = Field(min_length=1,max_length=5000)
    next_steps: list[str] = Field(default_factory=list,max_length=8)


class ApprovalDecision(BaseModel):
    decision: Literal["approved", "rejected"]
    comment: str = Field(default="", max_length=2000)
    apply_to_project:bool=False
    database_name: str | None = Field(default=None, max_length=64)
    decision_revision: int | None = Field(default=None, ge=0)


class ReadArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    view: Literal['current', 'original'] = Field(default='current', description='默认读取待验证的当前副本；审查已删除文件时可用 original 读取原项目的删除前内容。')
    path: str
    start_line: int = Field(default=1, ge=1)
    limit: int = Field(default=120, ge=1, le=500)


class ToolResultReadArguments(BaseModel):
    model_config = ConfigDict(extra='forbid')
    artifact_id: str = Field(pattern=r'^artifact_[0-9a-f]{32}$')
    offset: int = Field(default=0, ge=0)
    length: int = Field(default=8000, ge=1, le=16000)


class SearchArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=300)
    limit: int = Field(default=12, ge=1, le=50)


class ListArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = "."
    limit: int = Field(default=100, ge=1, le=500)


class GitDiffArguments(ListArguments):
    base_ref: str | None = None
    head_ref: str | None = None

    @field_validator("base_ref", "head_ref")
    @classmethod
    def valid_ref(cls, value):
        return validate_git_ref(value)


def validate_git_ref(value):
    import re
    if value is not None and (value.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_./~^+-]{1,200}", value)):
        raise ValueError("Git 引用格式不正确。")
    return value
