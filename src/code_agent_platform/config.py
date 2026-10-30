from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse
from typing import Literal

from pydantic import BaseModel, Field

from .models import PlatformError, digest
from .mcp_config import MCPServerConfig, default_servers
from .policy import PolicyRule
from .context_management import ContextManagementSettings
from .generation import GenerationSettings, generation_hash, request_limits


class Settings(BaseModel):
    data_dir: Path
    allowed_roots: list[Path]
    allow_local_project_selection: bool = False
    database_url: str | None = None
    model_name: str = "deepseek-flash"
    model_url: str = "http://127.0.0.1:11434/v1"
    model_key_env: str = "AI_AGENT_API_KEY"
    allow_remote_model: bool = False
    model_timeout: int = Field(default=60, ge=1, le=300)
    model_provider: Literal["auto", "openai", "deepseek"] = "auto"
    model_thinking: Literal["enabled", "disabled"] = "enabled"
    reasoning_effort: Literal["low", "high", "max"] = "low"
    model_max_output_tokens: int = Field(default=8192, ge=256, le=393216)
    generation: GenerationSettings | None = None
    powershell_image: str = "mcr.microsoft.com/dotnet/sdk:8.0"
    input_price_per_million: float | None = None
    output_price_per_million: float | None = None
    price_version: str = "unconfigured"
    sandbox_images: list[str] = Field(default_factory=lambda: ["python:3.12-slim", "maven:3.9-eclipse-temurin-17", "mcr.microsoft.com/dotnet/sdk:8.0"])
    sandbox_timeout: int = Field(default=120, ge=1, le=600)
    allow_mysql_test_network: bool = False
    mysql_test_image: str = "mysql:8.4"
    mysql_test_startup_seconds: int = Field(default=90, ge=5, le=180)
    approval_seconds: int = Field(default=3600, ge=1, le=86400)
    max_file_bytes: int = 256_000
    max_scan_files: int = 4000
    policy_version: str = "policy-v1"
    prompt_version: str = "prompt-v1"
    config_version: str = "config-v1"
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=default_servers)
    remote_evidence_seconds: int = Field(default=900, ge=30, le=86400)
    policy_rules: list[PolicyRule] = Field(default_factory=list)
    team_policies: dict[str, list[PolicyRule]] = Field(default_factory=dict)
    user_policies: dict[str, list[PolicyRule]] = Field(default_factory=dict)
    context_management: ContextManagementSettings = Field(default_factory=ContextManagementSettings)

    @classmethod
    def load(cls, filename: str | Path | None = None):
        # 只有控制面加载本地配置；仓库中的文档不能覆盖这些字段。
        from dotenv import load_dotenv
        load_dotenv(Path.cwd() / ".env", override=False)
        values = json.loads(Path(filename).read_text(encoding="utf-8")) if filename else {}
        if "mcp_servers" in values:
            profiles = {name: profile.model_dump() for name, profile in default_servers().items()}
            for name, profile in values["mcp_servers"].items():
                profiles[name] = {**profiles.get(name, {}), **profile}
            values["mcp_servers"] = profiles
        if os.getenv("AI_AGENT_MYSQL_ENABLED"):
            profiles = values.setdefault("mcp_servers", {name:profile.model_dump() for name,profile in default_servers().items()})
            profiles["mysql_readonly"]["enabled"] = os.environ["AI_AGENT_MYSQL_ENABLED"].lower() == "true"
        values.setdefault("data_dir", os.getenv("AI_AGENT_DATA_DIR", str(Path.cwd() / ".temp" / "platform")))
        values.setdefault("allowed_roots", [str(Path.cwd())])
        for key, env in {"database_url": "AI_AGENT_DATABASE_URL", "model_name": "AI_AGENT_MODEL", "model_url": "AI_AGENT_BASE_URL", "model_provider": "AI_AGENT_MODEL_PROVIDER", "model_thinking": "AI_AGENT_THINKING"}.items():
            if os.getenv(env):
                values[key] = os.environ[env]
        if os.getenv("AI_AGENT_ALLOW_REMOTE"):
            values["allow_remote_model"] = os.environ["AI_AGENT_ALLOW_REMOTE"].lower() == "true"
        if os.getenv("AI_AGENT_LOCAL_PROJECT_SELECTION"):
            values["allow_local_project_selection"] = os.environ["AI_AGENT_LOCAL_PROJECT_SELECTION"].lower() == "true"
        result = cls.model_validate(values)
        result.data_dir = result.data_dir.resolve()
        result.allowed_roots = [item.resolve() for item in result.allowed_roots]
        return result

    def versions(self) -> dict:
        # Request compression and generation profiles do not change approved
        # file/DB scopes. Audit them separately from permission configuration.
        public = self.model_dump(mode="json", exclude={"database_url", "data_dir", "context_management", "generation"})
        return {"config": self.config_version, "config_hash": digest(public), "policy": self.policy_version, "prompt": self.prompt_version, "model": self.model_name, "pricing": self.price_version, "context": digest(self.context_management.model_dump()), "generation": generation_hash(self)}

    def model(self):
        from .cancellation import CancellableChatOpenAI
        host = urlparse(self.model_url).hostname
        if not self.allow_remote_model and host not in {"localhost", "127.0.0.1", "::1"}:
            raise PlatformError("remote_model_disabled", "远程模型未启用，请由管理员配置模型访问范围。")
        key = os.getenv(self.model_key_env)
        if not key and host not in {"localhost", "127.0.0.1", "::1"}:
            raise PlatformError("model_not_configured", f"请配置环境变量 {self.model_key_env}。")
        limits = request_limits(self)
        common = dict(model=self.model_name, base_url=self.model_url, api_key=key or "local-model", timeout=limits['timeout'], max_retries=0, max_tokens=limits['max_tokens'], use_responses_api=False)
        if self.model_provider == "deepseek" or self.model_provider == "auto" and host == "api.deepseek.com":
            from .deepseek_model import DeepSeekModel
            return DeepSeekModel(**common, reasoning_effort=self.reasoning_effort, extra_body={"thinking": {"type": self.model_thinking}})
        return CancellableChatOpenAI(**common)
