import sys
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("http-fixture", host="127.0.0.1", port=int(sys.argv[1]), log_level="WARNING")


@mcp.tool()
def ping(label: str) -> dict:
    """Return a label over an actual Streamable HTTP MCP connection."""
    return {"label": label, "transport": "http"}


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
