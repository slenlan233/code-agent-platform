"""Managed MCP sessions: each session is opened, used and closed in one async task."""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from concurrent.futures import Future
from contextlib import AsyncExitStack
from threading import RLock, Thread

from langchain_mcp_adapters.tools import load_mcp_tools

from .integrations.mcp_client import child_environment, create_client
from .models import PlatformError, digest


def decoded_result(result):
    if isinstance(result, list):
        if any(item.get("type") != "text" for item in result if isinstance(item, dict)):
            raise PlatformError("mcp_output_type", "当前平台只接收结构化文本 MCP 结果。")
        result = "\n".join(item["text"] for item in result if isinstance(item, dict) and "text" in item)
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            result = {"status": "succeeded", "text": result}
    if len(json.dumps(result, ensure_ascii=False, default=str)) > 128000:
        raise PlatformError("mcp_output_limit", "MCP 结果过大，请缩小查询范围。")
    if isinstance(result, dict) and (result.get("success") is False or result.get("status") in {"failed", "error"}):
        raise PlatformError("mcp_execution_failed", "MCP 工具返回执行失败，请检查服务配置或查询参数。")
    return result


class SessionWorker:
    def __init__(self, name, config, connection):
        self.name, self.config, self.connection = name, config.model_copy(deep=True), connection
        self.binding_hash = digest({"profile": config.model_dump(), "connection": connection})
        self.ready = asyncio.get_running_loop().create_future()
        self.queue = asyncio.Queue()
        self.task = asyncio.create_task(self.run())

    async def run(self):
        current = None
        try:
            async with AsyncExitStack() as stack:
                client = create_client({self.name: self.connection})
                async with asyncio.timeout(self.config.timeout_seconds):
                    session = await stack.enter_async_context(client.session(self.name))
                    loaded = await load_mcp_tools(session, handle_tool_errors=False)
                tools = {tool.name: tool for tool in loaded if tool.name in self.config.allow_tools}
                missing = set(self.config.allow_tools) - tools.keys()
                if missing:
                    raise PlatformError("mcp_tools_missing", "MCP 服务未提供管理员登记的全部工具。")
                self.ready.set_result(tools)
                while True:
                    request = await self.queue.get()
                    if request is None:
                        break
                    name, arguments, current = request
                    if current.cancelled():
                        current = None
                        continue
                    try:
                        value = await asyncio.wait_for(tools[name].ainvoke(arguments), self.config.timeout_seconds)
                        value = decoded_result(value)
                        if not current.done():
                            current.set_result(value)
                    except Exception as error:
                        if not current.done():
                            current.set_exception(error if isinstance(error, PlatformError) else PlatformError("mcp_call_failed", "MCP 调用失败或超时，详细状态已记入工具审计。"))
                    current = None
        except BaseException:
            error = PlatformError("mcp_unavailable", "MCP 服务不可用，请检查启动配置和服务依赖。")
            if not self.ready.done():
                self.ready.set_exception(error)
            if current is not None and not current.done():
                current.set_exception(error)
        finally:
            while not self.queue.empty():
                item = self.queue.get_nowait()
                if item and not item[2].done():
                    item[2].set_exception(PlatformError("mcp_closed", "MCP 会话已关闭。"))

    async def call(self, name, arguments):
        await self.ready
        if self.task.done():
            raise PlatformError("mcp_closed", "MCP 会话已退出。")
        result = asyncio.get_running_loop().create_future()
        await self.queue.put((name, arguments, result))
        return await result

    async def close(self):
        await self.queue.put(None)
        try:
            await asyncio.wait_for(asyncio.shield(self.task), 10)
        except TimeoutError:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


class MCPRuntime:
    def __init__(self, platform):
        self.platform = platform
        self.loop = None
        self.thread = None
        self.workers = {}
        self.lock = RLock()

    def _start(self):
        with self.lock:
            if self.thread is None:
                self.loop = asyncio.new_event_loop()
                def run():
                    asyncio.set_event_loop(self.loop)
                    self.loop.run_forever()
                    self.loop.close()
                self.thread = Thread(target=run, name="mcp-sessions", daemon=True)
                self.thread.start()

    def _sync(self, coroutine, timeout=140):
        self._start()
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return future.result(timeout)
        except TimeoutError:
            future.cancel()
            raise PlatformError("mcp_timeout", "MCP 会话等待超时。") from None

    def connection(self, config, project):
        if config.transport == "http":
            headers = {key: os.environ[value] for key, value in config.headers_env.items() if value in os.environ}
            return {"transport": "http", "url": config.url, "headers": headers}
        modules = {"project_files": "files", "mysql": "mysql", "bailian": "bailian"}
        command = sys.executable if config.builtin or config.command in {"python", "python3"} else config.command
        if not Path(command).is_absolute():
            raise PlatformError("mcp_command", "自定义 MCP 启动程序需要使用绝对路径，或使用当前环境的 python。")
        args = ["-I", "-m", "code_agent_platform.integrations." + modules[config.builtin]] if config.builtin else config.args
        env = child_environment(config.env_keys, {"MCP_PROJECT_ROOT": project["root"], "MCP_PROJECT_ID": project["id"]})
        # Admin write profiles can never be inherited by platform tool servers.
        env.pop("AI_AGENT_ENABLE_LEGACY_DB_WRITES", None)
        env.pop("AI_AGENT_ENABLE_KNOWLEDGE_ADMIN", None)
        return {"transport": "stdio", "command": command, "args": args, "env": env, "cwd": str(self.platform.settings.data_dir)}

    def binding_hash(self, name, project):
        config = self.platform.settings.mcp_servers[name]
        connection = self.connection(config, project)
        return digest({"profile": config.model_dump(), "connection": connection})

    async def _prepare(self, actor, task):
        project = self.platform.project(actor, task["project_id"])
        result = []
        for name in project.get("mcp_servers", ["project_files"]):
            config = self.platform.settings.mcp_servers.get(name)
            if config is None or not config.enabled:
                result.append({"server": name, "status": "disabled", "tools": []})
                continue
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,40}", name):
                raise PlatformError("mcp_name", "MCP 服务名称不正确。")
            key = (task["id"], name)
            worker = self.workers.get(key)
            if worker is not None and (worker.binding_hash != self.binding_hash(name, project) or worker.task.done()):
                await worker.close()
                self.workers.pop(key)
                worker = None
            if worker is None:
                worker = self.workers[key] = SessionWorker(name, config, self.connection(config, project))
            try:
                tools = await worker.ready
                bound = []
                for tool_name, tool in tools.items():
                    alias = "mcp__" + name + "__" + tool_name
                    if len(alias) > 64 or not re.fullmatch(r"[A-Za-z0-9_-]+", alias):
                        raise PlatformError("mcp_tool_name", "MCP 工具名称不符合模型接口限制。")
                    self.platform.gateway.bind_mcp(task["id"], alias, tool, name, config, self)
                    bound.append(alias)
                status = {"server": name, "status": "connected", "tools": bound, "transport": config.transport}
            except PlatformError as error:
                status = {"server": name, "status": "unavailable", "tools": [], "error": error.code}
                if config.required:
                    raise
            result.append(status)
        return result

    def prepare(self, actor, task):
        result = self._sync(self._prepare(actor, task))
        self.platform._set(task["id"], tool_connections=result)
        self.platform.store.event(task["id"], "MCPConnections", {"servers": result})
        return result

    async def _call(self, task_id, server, tool_name, arguments):
        worker = self.workers.get((task_id, server))
        if worker is None:
            raise PlatformError("mcp_not_connected", "该任务尚未建立对应 MCP 会话。")
        return await worker.call(tool_name, arguments)

    def call(self, task_id, server, tool_name, arguments):
        return self._sync(self._call(task_id, server, tool_name, arguments))

    async def _close_task(self, task_id):
        selected = [key for key in self.workers if key[0] == task_id]
        for key in selected:
            await self.workers.pop(key).close()

    def close_task(self, task_id):
        if self.thread is not None:
            self._sync(self._close_task(task_id))
        self.platform.gateway.task_mcp.pop(task_id, None)
        self.platform.gateway.task_mcp_actions.pop(task_id, None)

    def close(self):
        if self.thread is not None:
            for task_id in list({key[0] for key in self.workers}):
                self.close_task(task_id)
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=10)
            self.thread = None
