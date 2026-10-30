"""Disposable real MCP server used to verify session lifetime and environment isolation."""
import os
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("session-fixture", log_level="WARNING")
counter = 0


@mcp.tool()
def next_value() -> dict:
    """Increment a value held by this server process."""
    global counter
    counter += 1
    return {"value": counter, "pid": os.getpid(), "model_key_inherited": "AI_AGENT_API_KEY" in os.environ,
            "admin_enabled": "AI_AGENT_ENABLE_KNOWLEDGE_ADMIN" in os.environ}


@mcp.tool()
def forbidden_write() -> str:
    """This tool must never be exposed by the platform allowlist."""
    raise AssertionError("The platform must not call this tool.")


if __name__ == "__main__":
    mcp.run(transport="stdio")
