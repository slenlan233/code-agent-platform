from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MCPServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    builtin: Literal["project_files", "mysql", "bailian"] | None = None
    transport: Literal["stdio", "http"] = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    env_keys: list[str] = Field(default_factory=list)
    headers_env: dict[str, str] = Field(default_factory=dict)
    allow_tools: list[str] = Field(min_length=1, max_length=50)
    path_fields: dict[str, list[str]] = Field(default_factory=dict)
    required: bool = False
    read_only: Literal[True] = True
    timeout_seconds: int = Field(default=20, ge=1, le=120)

    @model_validator(mode="after")
    def valid_connection(self):
        if self.transport == "stdio" and not (self.builtin or self.command):
            raise ValueError("stdio 服务需要管理员指定启动程序。")
        if self.transport == "http":
            target = urlparse(self.url or "")
            if target.scheme not in {"http", "https"} or not target.hostname or target.username or target.password or target.query:
                raise ValueError("HTTP MCP 地址不能包含凭证或查询参数。")
        return self


def default_servers():
    return {
        "project_files": MCPServerConfig(builtin="project_files", allow_tools=["read_file", "find_files"], path_fields={"read_file": ["path"], "find_files": ["path"]}),
        "mysql_readonly": MCPServerConfig(builtin="mysql", enabled=False, allow_tools=["mysql_list_databases", "mysql_list_tables", "mysql_describe_tables", "mysql_execute_query"], env_keys=["MYSQL_HOST", "MYSQL_PORT", "MYSQL_READ_USER", "MYSQL_READ_PASSWORD", "MYSQL_DATABASES"]),
        "bailian": MCPServerConfig(builtin="bailian", enabled=False, allow_tools=["query_rag"], env_keys=["ALIBABA_CLOUD_ACCESS_KEY_ID", "ALIBABA_CLOUD_ACCESS_KEY_SECRET", "BAILIAN_WORKSPACE_ID", "BAILIAN_INDEX_ID"]),
    }
