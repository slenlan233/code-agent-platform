"""The original MCP client factory, shared with the governed platform."""
import os

from langchain_mcp_adapters.client import MultiServerMCPClient

BASE_ENV = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "COMSPEC", "PATHEXT", "PYTHONUTF8", "VIRTUAL_ENV"}


def child_environment(keys=(), values=None):
    allowed = BASE_ENV | set(keys)
    result = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    result.update(PYTHONUTF8="1", PYTHONUNBUFFERED="1")
    result.update(values or {})
    return result


def create_client(connections):
    return MultiServerMCPClient(connections, handle_tool_errors=False)


async def create_mcp_stdio_client(name, params):
    extra = ("ALIBABA_CLOUD_ACCESS_KEY_ID", "ALIBABA_CLOUD_ACCESS_KEY_SECRET", "BAILIAN_WORKSPACE_ID", "BAILIAN_INDEX_ID", "BAILIAN_CATEGORY_ID") if name in {"rag_tools", "self_rag_tools", "rag_self_tools"} else ()
    options = {"transport": "stdio", "env": child_environment(extra), **params}
    client = create_client({name: options})
    return client, await client.get_tools()
