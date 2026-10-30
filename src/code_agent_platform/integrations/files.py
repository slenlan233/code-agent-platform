"""Project file MCP service using the original LangChain FileManagementToolkit."""
import fnmatch
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from .file_toolkit import file_tools

from code_agent_platform.models import digest
from code_agent_platform.security import SECRET, files, safe_path

mcp = FastMCP("project-files", log_level="WARNING")


@mcp.tool()
def read_file(path: str, start_line: int = 1, limit: int = 120) -> dict:
    """Read an allowed project text file with content hash and line evidence."""
    root = Path(os.environ["MCP_PROJECT_ROOT"]).resolve()
    candidate = safe_path(root, path)
    if not candidate.is_file() or candidate.stat().st_size > 256_000 or start_line < 1 or not 1 <= limit <= 500:
        raise ValueError("文件或行范围超出限制。")
    raw = candidate.read_bytes()
    if b"\x00" in raw:
        raise ValueError("只支持文本文件。")
    toolkit = {tool.name: tool for tool in file_tools(root)}
    text = toolkit["read_file"].invoke({"file_path": candidate.relative_to(root).as_posix()})
    if not isinstance(text, str) or SECRET.search(text):
        raise ValueError("文件内容不允许返回。")
    lines = text.splitlines()
    if start_line > max(len(lines), 1):
        raise ValueError("起始行超出文件范围。")
    snippet = "\n".join(lines[start_line-1:start_line-1+limit])
    if len(snippet) > 32000:
        raise ValueError("片段过大，请缩小读取范围。")
    return {"path": path, "start_line": start_line, "end_line": min(len(lines), start_line+limit-1),
            "snippet": snippet, "content_hash": digest(raw), "status": "succeeded"}


@mcp.tool()
def find_files(pattern: str, path: str = ".") -> dict:
    """Find non-sensitive project paths matching a filename pattern."""
    if len(pattern) > 200:
        raise ValueError("匹配模式过长。")
    root = Path(os.environ["MCP_PROJECT_ROOT"]).resolve()
    folder = safe_path(root, path)
    result = [candidate.relative_to(root).as_posix() for name, candidate in files(folder)
              if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(candidate.name, pattern)]
    return {"files": result[:200], "truncated": len(result) > 200, "status": "succeeded"}


if __name__ == "__main__":
    mcp.run(transport="stdio")
