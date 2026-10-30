from __future__ import annotations

import asyncio
import json
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError
from jsonschema import Draft202012Validator, ValidationError as JSONValidationError

from .models import Actor, Evidence, GitDiffArguments, ListArguments, PlatformError, ReadArguments, SearchArguments, ToolResultReadArguments, digest, identifier, timestamp
from .security import EXCLUDED, SECRET, authorize, blocked, files, redact, safe_path
from .policy import evaluate, enforce
from .git_support import git_output, repository_info


class Gateway:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.tools = {
            "read_file": (ReadArguments, self.read_file),
            "list_files": (ListArguments, self.list_files),
            "search_code": (SearchArguments, self.search_code),
            "rag_search": (SearchArguments, self.rag_search),
            "git_status": (ListArguments, self.git_status),
            "git_diff": (GitDiffArguments, self.git_diff),
            "read_tool_result": (ToolResultReadArguments, self.read_tool_result),
        }
        self.hooks = {"PreToolUse": [], "PostToolUse": []}
        for name in ("UserPromptSubmit", "SessionStart", "BeforePlan", "AfterPlan", "BeforeFileEdit", "AfterFileEdit", "BeforeCommand", "AfterCommand", "AfterTest", "BeforeCommit", "TaskCompleted", "SessionEnd"):
            self.hooks[name] = []
        self.mcp_tools = {}
        self.task_mcp = {}
        self.task_mcp_actions = {}
        self.mcp_runtime = None
        self.actor_resolver = None

    def current_actor(self, actor):
        current = self.actor_resolver(actor) if self.actor_resolver else actor
        if current is None:
            raise PlatformError("identity_revoked", "调用人的访问权限已撤销。", 403)
        return current

    def policy(self, actor, task, project, action, resources):
        if task.get('mode')=='auto':task={**task,'mode':'readonly'}
        session = self.store.get("session", task["session_id"]) if task.get("session_id") else {}
        layers = [("enterprise", self.settings.policy_rules), ("team", self.settings.team_policies.get(project.get("team", "default"), [])),
                  ("project", project.get("policy_rules", [])), ("user", self.settings.user_policies.get(actor.user_id, [])),
                  ("session", session.get("policy_rules", [])), ("task", task.get("policy_rules", []))]
        canonical = self.task_mcp_actions.get(task["id"], {}).get(action, "read_file" if action in self.mcp_tools or action == 'read_tool_result' else action)
        return evaluate(layers, actor, task, [action, canonical], resources or ["."])

    def source_allowed(self, actor, task, project, path, origin="read_file"):
        actor = self.current_actor(actor)
        project = self.store.get("project", project["id"])
        scope = task or {"id": "source_scope", "mode": "readonly"}
        authorize(actor, project, "read_file", scope["mode"])
        return all(self.policy(actor, scope, project, action, [path])["effect"] != "deny" for action in {"read_file", origin})

    def hook(self, kind, actor, task, payload):
        started = time.monotonic()
        status = "succeeded"
        try:
            if kind == "AfterTest" and payload.get("result", {}).get("status") == "passed" and payload["result"].get("exit_code") != 0:
                raise PlatformError("verification_evidence", "验证报告缺少成功退出码，不能报告通过。")
            if kind == "TaskCompleted" and payload.get("status") == "completed" and task["mode"] == "approval_write":
                from .checks import require_ai_check, require_database_deletion
                if task.get('database_operation_id'):
                    require_database_deletion(task, self.store)
                if not task.get('database_only'):
                    require_ai_check(task,self.store)
                    approval = self.store.get("approval", task["approval_id"])
                    if task.get("verification", {}).get("status") != "passed" or approval["kind"] != "diff" or approval["status"] != "approved":
                        raise PlatformError("completion_gate", "修改任务尚未完成最终审批和验证。", 403)
            for callback in self.hooks[kind]:
                safe_payload = deepcopy(payload)
                if kind in {"PreToolUse", "PostToolUse"}:
                    value = callback(actor, task, safe_payload["name"], safe_payload.get("arguments") if kind == "PreToolUse" else safe_payload.get("result"))
                else:
                    value = callback(actor, task, safe_payload)
                if value is False:
                    raise PlatformError("hook_rejected", "必需的执行检查未通过：" + kind, 403)
        except Exception as error:
            status = "failed"
            if isinstance(error, PlatformError):
                raise
            raise PlatformError("hook_failed", "执行检查失败：" + kind) from error
        finally:
            self.store.event(task["id"], "HookExecuted", {"hook": kind, "status": status, "callbacks": len(self.hooks[kind]),
                             "duration_ms": round((time.monotonic()-started)*1000), "operation": payload.get("operation"), "tool": payload.get("name")})

    def check_cancelled(self, task):
        task_id = task if isinstance(task, str) else task['id']
        row = self.store.get('task', task_id)
        if row.get('cancel_requested') or row['status'] in {'cancelling', 'cancelled'}:
            raise PlatformError('task_cancelled', '任务已请求停止，后续操作不再执行。', 409)

    def check_budget(self, task_id: str, *, tool=False, model=False):
        from .budgeting import project_token_usage
        current=self.store.get('task',task_id)
        project=self.store.get('project',current['project_id'])
        from .security import require_active_record
        require_active_record(project)
        if current.get('session_id'):
            require_active_record(self.store.get('session', current['session_id']), '对话')
        if project_token_usage(self.store,project)['exhausted']:
            raise PlatformError('project_token_budget','该项目已达到累计 token 总预算，请在 Token 用量中调整额度。')
        def reserve(task):
            budget = task["budget"]
            usage = task.setdefault("usage", {"tool_calls": 0, "model_calls": 0, "input_tokens": 0, "output_tokens": 0, "cost": None})
            if task.get('cancel_requested') or task.get("status") in {"cancelled", 'cancelling'}:
                raise PlatformError("task_cancelled", "任务已取消。", 409)
            if usage.get("unreported_calls"):
                raise PlatformError("usage_unreported", "模型没有返回完整用量，已停止后续执行；请检查供应商的用量返回配置。")
            budget_epoch = task.get("finished_epoch")
            if budget_epoch is None and task.get("finished_at"):
                budget_epoch = datetime.fromisoformat(task["finished_at"]).timestamp()
            pending_wait = 0
            if task.get('status', '').startswith('awaiting_') and task.get('approval_id'):
                approval = self.store.get('approval', task['approval_id'])
                if approval['status'] == 'pending':
                    pending_wait = max(0, time.time()-approval['waiting_since'])
            if (budget_epoch if budget_epoch is not None else time.time()) - task["started_epoch"] - task.get("approval_wait_seconds", 0) - pending_wait > budget["max_seconds"]:
                raise PlatformError("time_budget", "任务已达到执行时间预算。")
            if task.get('token_budget_source')!='project' and budget.get('max_tokens') is not None and usage["input_tokens"] + usage["output_tokens"] >= budget["max_tokens"]:
                raise PlatformError("token_budget", "任务已达到累计 token 预算。")
            if budget.get("max_cost") is not None:
                if usage["cost"] is None:
                    raise PlatformError("pricing_missing", "成本预算需要先配置模型计价。")
                if usage["cost"] >= budget["max_cost"]:
                    raise PlatformError("cost_budget", "任务已达到成本预算。")
            if tool:
                if usage["tool_calls"] >= budget["max_tool_calls"]:
                    raise PlatformError("tool_budget", "任务已达到工具调用次数上限。")
                usage["tool_calls"] += 1
            if model:
                if usage["model_calls"] >= budget["max_model_calls"]:
                    raise PlatformError("model_budget", "任务已达到模型调用次数上限。")
                usage["model_calls"] += 1
            task["usage"] = usage
            return task
        return self.store.mutate("task", task_id, reserve)

    def invoke(self, actor: Actor, task_id: str, name: str, arguments: dict):
        task = self.store.get("task", task_id)
        call = {"id": identifier("tool"), "task_id": task_id, "project_id": task["project_id"], "user_id": actor.user_id, "name": name, "input": redact(arguments), "status": "requested", "policy_version": self.settings.policy_version, "started_at": timestamp()}
        # 审计失败时不会继续执行工具。
        self.store.put("tool_call", call)
        started = time.monotonic()
        try:
            actor = self.current_actor(actor)
            project = self.store.get("project", task["project_id"])
            available = {**self.tools, **self.task_mcp.get(task_id, {})}
            action = "read_file" if name in self.mcp_tools or name in self.task_mcp.get(task_id, {}) or name == 'read_tool_result' else name
            call["permission"] = authorize(actor, project, action, task["mode"])
            if name not in available:
                raise PlatformError("tool_denied", "该工具尚未注册。", 403)
            schema, handler = available[name]
            if isinstance(schema, dict):
                Draft202012Validator(schema).validate(arguments)
                args = {key: value["default"] for key, value in schema.get("properties", {}).items() if "default" in value}
                args.update(arguments)
            else:
                args = schema.model_validate(arguments).model_dump()
            resources = [str(args[key]) for key in ("path", "file_path", "dir_path", "database") if args.get(key) is not None]
            decision = self.policy(actor, task, project, name, resources)
            call["policy_decision"] = decision
            enforce(decision)
            self.check_budget(task_id, tool=True)
            self.hook("PreToolUse", actor, task, {"name": name, "arguments": args})
            view = self.review_project(task, project) if name in {"read_file", "list_files", "search_code", "rag_search"} and not (name == 'read_file' and args.get('view') == 'original') else project
            result = handler(actor, task, view, **args)
            actor = self.current_actor(actor)
            project = self.store.get("project", task["project_id"])
            authorize(actor, project, action, task["mode"])
            enforce(self.policy(actor, task, project, name, resources))
            if SECRET.search(json.dumps(result, ensure_ascii=False, default=str)):
                raise PlatformError("secret_in_result", "工具结果包含疑似凭证，已阻止返回。", 403)
            self.hook("PostToolUse", actor, task, {"name": name, "result": result})
            sources = ([result] if isinstance(result, dict) and result.get("source_id") else result.get("evidence", []) if isinstance(result, dict) else [])
            ids = [item["source_id"] for item in sources if isinstance(item, dict) and item.get("source_id")]
            if ids:
                self.store.mutate("task", task_id, lambda row: {**row, "evidence_ids": list(dict.fromkeys([*row.get("evidence_ids", []), *ids]))})
            call.update(status="succeeded", output=redact(result))
            self.store.event(task_id, "PostToolUse", {"tool_call_id": call["id"], "name": name, "status": "succeeded"})
            return result
        except (ValidationError, JSONValidationError):
            error = PlatformError("invalid_arguments", "工具参数格式不正确。")
            call.update(status="failed", error={"code": error.code, "message": error.message})
            raise error from None
        except PlatformError as error:
            call.update(status="denied" if error.status == 403 else "failed", error={"code": error.code, "message": error.message})
            if error.status == 403:
                call["permission"] = "deny"
            raise
        except Exception as error:
            call.update(status="failed", error={"code": "tool_failed", "message": type(error).__name__})
            raise PlatformError("tool_failed", "工具执行失败，请查看任务记录。") from error
        finally:
            call.update(duration_ms=round((time.monotonic() - started) * 1000), finished_at=timestamp())
            self.store.put("tool_call", call)

    def archive_tool_result(self, task, name, arguments, content):
        binding = None
        if name.startswith('mcp__'):
            _, server, _ = name.split('__', 2)
            worker = self.mcp_runtime.workers.get((task['id'], server))
            project = self.store.get('project', task['project_id'])
            binding = worker.binding_hash if worker else self.mcp_runtime.binding_hash(server, project)
        registered = {**self.tools, **self.task_mcp.get(task['id'], {})}.get(name)
        if registered:
            schema = registered[0]
            if isinstance(schema, dict):
                arguments = {**{key: value['default'] for key, value in schema.get('properties', {}).items() if 'default' in value}, **arguments}
            else:
                arguments = schema.model_validate(arguments).model_dump()
        artifact = self.store.artifact(task, 'context-tool-result.txt', content)
        return self.store.mutate('artifact', artifact['id'], lambda row: {**row, 'context_archive': 'tool_result',
            'origin_tool': name, 'origin_arguments': redact(arguments), 'origin_binding_hash': binding})

    def archive_context_material(self, task, content, descriptor):
        artifact = self.store.artifact(task, 'context-material.txt', content)
        path = descriptor.get('path', '')
        args = {'path': path} if path and '://' not in path else {}
        return self.store.mutate('artifact', artifact['id'], lambda row: {**row, 'context_archive': 'phase_material',
            'origin_tool': 'read_file', 'origin_arguments': args, 'material': descriptor})

    def read_tool_result(self, actor, task, project, artifact_id, offset=0, length=8000):
        artifact = self.store.get('artifact', artifact_id)
        if artifact['task_id'] != task['id'] or artifact['project_id'] != project['id']:
            raise PlatformError('artifact_denied', '只能读取本轮已授权的归档材料或执行日志。', 403)
        if artifact.get('context_archive') not in {'tool_result', 'phase_material'}:
            origin = self.execution_log_source(task, project, artifact_id)
            if origin is None:
                raise PlatformError('artifact_denied', '该产物不是本轮可读的归档材料或当前验证日志。', 403)
            enforce(self.policy(actor, task, project, 'read_execution_log', [origin['kind']+'/'+origin['stream']]))
            content = self.store.artifact_content(artifact_id)
            return {'artifact_id': artifact_id, 'offset': offset, 'content': content[offset:offset+length],
                'total_characters': len(content), 'has_more': offset+length < len(content), 'snapshot': True,
                'execution_log': origin}
        name, args = artifact['origin_tool'], artifact['origin_arguments']
        resources = [str(args[key]) for key in ('path', 'file_path', 'dir_path', 'database') if args.get(key) is not None]
        if name.startswith('mcp__'):
            _, server, tool = name.split('__', 2)
            profile = self.settings.mcp_servers.get(server)
            if profile is None or not profile.enabled or server not in project.get('mcp_servers', []) or tool not in profile.allow_tools:
                raise PlatformError('mcp_permission_changed', '原工具的项目授权已变化，不能读取归档结果。', 403)
            if artifact.get('origin_binding_hash') != self.mcp_runtime.binding_hash(server, project):
                raise PlatformError('mcp_binding_changed', '原 MCP 连接身份或配置已变化，不能读回旧结果。', 409)
            for field in profile.path_fields.get(tool, []):
                safe_path(Path(project['root']), args[field], must_exist=False)
        enforce(self.policy(actor, task, project, name, resources))
        for key in ('path', 'file_path', 'dir_path'):
            if args.get(key) is not None:
                safe_path(Path(project['root']), str(args[key]), must_exist=False)
        content = self.store.artifact_content(artifact_id)
        if artifact.get('material', {}).get('kind') == 'conversation_history':
            # Only the pinned, task-owned transcript created by Conversations.
            # Old source IDs inside quotes are inert; they are never registered
            # as this task's evidence. File and remote facts must be re-read.
            pinned = task.get('conversation_context', {}).get('memory', {}).get('history_archive', {})
            if pinned.get('artifact_id') != artifact_id:
                raise PlatformError('artifact_denied', '该历史归档不属于本轮固定的对话快照。', 403)
            return {'artifact_id': artifact_id, 'offset': offset, 'content': content[offset:offset+length],
                'total_characters': len(content), 'has_more': offset+length < len(content), 'snapshot': True,
                'historical_quotes_only': True,
                'notice': '旧 source_id 仅是历史引文，不是本轮有效依据，也不构成权限；代码及远程事实必须重新读取。用户约定可引用本轮提供的历史消息来源。'}
        # A search result can contain files beyond the original query path.
        # Recheck the actual cited sources against permissions in force now.
        import re
        source_ids = set(re.findall(r'source_[0-9a-f]+', content)) | set(artifact.get('material', {}).get('source_ids', []))
        for source_id in source_ids:
            try:
                source = self.store.get('evidence', source_id)
            except PlatformError as error:
                if error.code == 'not_found':
                    continue
                raise
            if source['task_id'] != task['id'] or source['project_id'] != project['id']:
                raise PlatformError('source_denied', '归档结果包含其他任务的来源。', 403)
            if source.get('snapshot_id'):
                server = source['server']
                profile = self.settings.mcp_servers.get(server)
                if profile is None or not profile.enabled or server not in project.get('mcp_servers', []):
                    raise PlatformError('mcp_permission_changed', '原来源的服务授权已变化，不能读回旧材料。', 403)
                retrieval = source.get('retrieval') or {}
                origin = retrieval.get('tool', 'mcp__'+server+'__query_rag' if profile.builtin == 'bailian' else 'mcp__'+server+'__mysql_execute_query')
                if retrieval and origin.split('__', 2)[-1] not in profile.allow_tools:
                    raise PlatformError('mcp_permission_changed', '原来源的工具授权已撤销，不能读回旧材料。', 403)
                enforce(self.policy(actor, task, project, origin, [source['path']]))
                enforce(self.policy(actor, task, project, 'database_query' if profile.builtin == 'mysql' else 'rag_search' if profile.builtin == 'bailian' else 'external_read', [source['path']]))
                if self.mcp_runtime.binding_hash(source['server'], project) != source['binding_hash']:
                    raise PlatformError('mcp_binding_changed', '原来源的连接身份已变化，不能读回旧结果。', 409)
            elif source['source_type'] not in {'request', 'patch'} and not self.source_allowed(actor, task, project, source['path'], name):
                raise PlatformError('source_policy_denied', '归档来源已不在当前读取权限内。', 403)
        return {'artifact_id': artifact_id, 'offset': offset, 'content': content[offset:offset+length],
            'total_characters': len(content), 'has_more': offset+length < len(content), 'snapshot': True}

    def execution_log_source(self, task, project, artifact_id):
        """Only receipts bound to this task's current proposal/approved plan."""
        verification = task.get('verification') or {}
        if verification.get('id'):
            run = self.store.get('sandbox_run', verification['id'])
            if (run['task_id'] == task['id'] and run['project_id'] == project['id'] and
                    run['patch_id'] == task.get('patch_id') and run['status'] in {'passed', 'failed', 'unavailable'}):
                for stream in ('stdout', 'stderr'):
                    if run.get(stream+'_id') == artifact_id:
                        return {'kind': 'verification', 'run_id': run['id'], 'patch_id': run['patch_id'], 'stream': stream}
        if task.get('plan_id'):
            plan = self.store.get('plan', task['plan_id'])
            for run in self.store.list('command_run', task_id=task['id']):
                if run['project_id'] != project['id'] or run['plan_hash'] != plan['hash']:
                    continue
                for stream in ('stdout', 'stderr'):
                    if run.get(stream+'_id') == artifact_id:
                        return {'kind': 'command', 'run_id': run['id'], 'plan_hash': plan['hash'], 'stream': stream}
        return None

    def review_project(self, task, project):
        """Read a server-created proposed checkout without changing authorization scope."""
        if task.get("current_node") not in {"CODE_REVIEW","GENERATE_PATCH"} or not task.get("review_patch_id"):
            return project
        patch = self.store.get("patch", task["review_patch_id"])
        if patch["id"] != task.get("patch_id") or patch["task_id"] != task["id"] or patch["project_id"] != project["id"]:
            raise PlatformError("review_patch", "审查副本不属于当前任务。", 403)
        from .runtime import Workspace
        base = Workspace(self.settings, namespace="review-workspaces").directory(task["id"])
        checkout = safe_path(base, "payload")
        changes = json.loads(self.store.artifact_content(patch["artifact_id"]))["changes"]
        return {**project, "root": str(checkout), "_review_patch": patch["id"], "_review_paths": {item["path"] for item in changes}}

    def evidence(self, task, project, path: str, *, start_line=1, limit=120, kind="code", metadata=None, actor=None, origin="read_file"):
        actor = actor or Actor.model_validate(task["actor"])
        if not self.source_allowed(actor, task, project, path, origin):
            raise PlatformError("source_policy_denied", "来源文件不在当前策略允许的读取范围内。", 403)
        candidate = safe_path(Path(project["root"]), path)
        if not candidate.is_file() or candidate.stat().st_size > self.settings.max_file_bytes:
            raise PlatformError("file_limit", "文件类型或大小超出读取限制。")
        raw = candidate.read_bytes()
        if b"\x00" in raw:
            raise PlatformError("binary_file", "只支持读取文本文件。")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise PlatformError("encoding", "当前只支持 UTF-8 项目文件。") from None
        if SECRET.search(text):
            raise PlatformError("secret_in_file", "文件包含疑似凭证，已阻止读取。", 403)
        lines = text.splitlines()
        if start_line > max(len(lines), 1):
            raise PlatformError("line_range", "起始行超出文件范围。")
        end = min(len(lines), start_line + limit - 1)
        content_hash = digest(raw)
        metadata = dict(metadata or {})
        if path in project.get("_review_paths", set()):
            kind = "patch"
            metadata["patch_id"] = project["_review_patch"]
        source_id = "source_" + digest([task["id"], path, start_line, end, content_hash])[:24]
        evidence = Evidence(source_id=source_id, task_id=task["id"], project_id=project["id"], source_type=kind, path=path, start_line=start_line, end_line=max(start_line, end), content_hash=content_hash, snippet="\n".join(lines[start_line - 1:end]), version=str(metadata.get("version", content_hash)), title=metadata.get("title", path), status=metadata.get("status", "active"), topic=metadata.get("topic", ""), priority=metadata.get("priority", 0))
        self.store.put("evidence", {"id": source_id, **evidence.model_dump(), "metadata": metadata})
        return evidence.model_dump()

    def read_file(self, actor, task, project, path, start_line=1, limit=120, view='current'):
        return self.evidence(task, project, path, start_line=start_line, limit=limit, actor=actor)

    def list_files(self, actor, task, project, path=".", limit=100):
        root = Path(project["root"])
        subroot = safe_path(root, path)
        if not subroot.is_dir():
            raise PlatformError("not_directory", "该路径不是目录。")
        result = []
        for _, item in files(subroot, limit=self.settings.max_scan_files):
            if not self.source_allowed(actor, task, project, item.relative_to(root).as_posix(), "list_files"):
                continue
            result.append(item.relative_to(root).as_posix())
            if len(result) >= limit:
                break
        return {"files": result, "limit": limit}

    def search_code(self, actor, task, project, query, limit=12):
        result = []
        for name, path in files(Path(project["root"]), limit=self.settings.max_scan_files):
            self._time_check(task)
            if not self.source_allowed(actor, task, project, name, "search_code"):
                continue
            if path.stat().st_size > self.settings.max_file_bytes:
                continue
            try:
                content = path.read_text(encoding="utf-8-sig")
            except (UnicodeError, OSError):
                continue
            if SECRET.search(content):
                continue
            matches = [index + 1 for index, line in enumerate(content.splitlines()) if query.casefold() in line.casefold()]
            if matches:
                result.append(self.evidence(task, project, name, start_line=max(1, matches[0] - 4), limit=32, actor=actor, origin="search_code"))
            if len(result) >= limit:
                break
        return {"evidence": result}

    def _time_check(self, task):
        if time.time() - task["started_epoch"] - task.get("approval_wait_seconds", 0) > task["budget"]["max_seconds"]:
            raise PlatformError("time_budget", "任务已达到执行时间预算。")

    def rag_search(self, actor, task, project, query, limit=12):
        from .retrieval import fuse_rankings, search_documents
        result = search_documents(self, task, project, query, limit, actor=actor)
        if re_search_relations(query) and "graph_query" in self.tools:
            try:
                graph = self.store.get("graph", "graph_" + project["id"])
            except PlatformError:
                return result
            names = sorted({item["name"] for item in graph["nodes"] if item["type"] in {"Function", "Table"}}, key=len, reverse=True)
            symbol = next((name for name in names if name in query), None)
            if symbol:
                args = {"symbol": symbol, "direction": "callers" if any(word in query for word in ("影响", "调用方", "impact", "caller")) else "callees", "depth": 4}
                graph_result = self.tools["graph_query"][1](actor, task, project, **args)
                result["evidence"] = fuse_rankings(graph_result.get("evidence", []), result["evidence"])[:limit]
                result["method"] = "bm25+graph_rrf"
                result["graph_status"] = graph_result["status"]
        return result

    @staticmethod
    def git(project, arguments):
        return git_output(project,arguments)

    def git_status(self, actor, task, project, path=".", limit=100):
        safe_path(Path(project["root"]), path)
        repository=repository_info(project)
        if not repository['available']:return {**repository,'status':''}
        parts = self.git(project, ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--", path]).split("\x00")
        visible, index = [], 0
        while index < len(parts):
            entry = parts[index]
            index += 1
            if not entry:
                continue
            code, name = entry[:2], entry[3:]
            resources = [name]
            if "R" in code or "C" in code:
                if index < len(parts) and parts[index]:
                    resources.append(parts[index])
                    index += 1
            try:
                for item in resources:
                    safe_path(Path(project["root"]), item, must_exist=False)
                if all(self.source_allowed(actor, task, project, item, "git_status") for item in resources):
                    visible.append(code + " " + name)
            except PlatformError as error:
                if error.code == "identity_revoked":
                    raise
            if len(visible) >= limit:
                break
        return {**repository,"status": "\n".join(visible)}

    def git_diff(self, actor, task, project, path=".", limit=100, base_ref=None, head_ref=None):
        root = Path(project["root"])
        safe_path(root, path)
        refs = [value for value in (base_ref, head_ref) if value is not None]
        repository=repository_info(project)
        if (task['mode']=='ci_review' or refs) and not repository.get('revision'):
            raise PlatformError('git_baseline_required','比较 Git 变更需要当前项目有可用的 Git 提交；普通新建项目不需要。')
        if not repository['available']:return {**repository,'diff':'','truncated':False}
        names = self.git(project, ["diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", *refs, "--", path]).split("\x00")
        candidates = []
        for name in names:
            if name:
                if not self.source_allowed(actor, task, project, name, "git_diff"):
                    continue
                try:
                    candidate = safe_path(root, name, must_exist=False)
                    if not candidate.exists() or candidate.stat().st_size <= self.settings.max_file_bytes:
                        candidates.append(name)
                except PlatformError:
                    continue
        if not candidates:
            return {**repository,"diff": ""}
        selected = candidates[:min(limit, 50)]
        return {**repository,"diff": self.git(project, ["diff", "--no-ext-diff", "--no-textconv", *refs, "--", *selected]), "truncated": len(selected) < len(candidates), "base_ref": base_ref, "head_ref": head_ref}

    def register_mcp(self, name, tool, *, read_only=False, resource_fields=()):
        # 仅由服务端装配可信 MCP 工具；仓库文件与模型不能调用此接口。
        if name in self.tools:
            raise ValueError("duplicate tool")
        if not read_only:
            raise ValueError("Only explicitly registered read-only MCP tools are supported.")
        schema = tool.get_input_schema()
        def execute(actor, task, project, **arguments):
            for field in resource_fields:
                safe_path(Path(project["root"]), arguments[field])
            return asyncio.run(tool.ainvoke(arguments))
        self.mcp_tools[name] = tool
        self.tools[name] = (schema, execute)

    def bind_mcp(self, task_id, alias, tool, server, config, runtime):
        config = config.model_copy(deep=True)
        project = self.store.get("project", self.store.get("task", task_id)["project_id"])
        binding_hash = runtime.binding_hash(server, project)
        schema = deepcopy(tool.args_schema) if isinstance(tool.args_schema, dict) else tool.get_input_schema().model_json_schema()
        def check_refs(value):
            if isinstance(value, dict):
                if "$ref" in value and not value["$ref"].startswith("#/"):
                    raise PlatformError("mcp_schema", "MCP 参数结构不能引用外部资源。")
                for child in value.values():
                    check_refs(child)
            elif isinstance(value, list):
                for child in value:
                    check_refs(child)
        check_refs(schema)
        schema["additionalProperties"] = False
        def execute(actor, task, project, **arguments):
            current = self.settings.mcp_servers.get(server)
            if server not in project.get("mcp_servers", ["project_files"]) or current is None or not current.enabled or current != config or tool.name not in current.allow_tools:
                raise PlatformError("mcp_permission_changed", "该项目的 MCP 工具授权已变化。", 403)
            if runtime.binding_hash(server, project) != binding_hash:
                raise PlatformError("mcp_binding_changed", "MCP 连接配置已变化，需要重新建立会话。", 409)
            for field in config.path_fields.get(tool.name, []):
                safe_path(Path(project["root"]), arguments[field])
            if config.builtin == "mysql" and tool.name == "mysql_execute_query":
                from .integrations.sql_policy import readonly_sql
                try:
                    readonly_sql(arguments["command"], arguments.get("params"))
                except ValueError:
                    raise PlatformError("sql_readonly", "数据库工具仅允许受限的单条只读查询。", 403) from None
            result = runtime.call(task["id"], server, tool.name, arguments)
            if runtime.binding_hash(server, project) != binding_hash:
                raise PlatformError("mcp_binding_changed", "查询过程中 MCP 连接配置已变化，请重新检索。", 409)
            if SECRET.search(json.dumps(result, ensure_ascii=False, default=str)):
                raise PlatformError("secret_in_result", "MCP 结果包含疑似凭证，已阻止返回。", 403)
            if config.builtin == "project_files" and tool.name == "read_file":
                local = self.evidence(task, project, arguments["path"], start_line=arguments.get("start_line", 1), limit=arguments.get("limit", 120), actor=actor)
                if result.get("content_hash") != local["content_hash"]:
                    raise PlatformError("source_changed", "MCP 读取后文件已变化，请重新检索。", 409)
                return local
            if config.builtin == "project_files" and tool.name == "find_files":
                result["files"] = [path for path in result.get("files", []) if self.source_allowed(actor, task, project, path, "list_files")]
            if config.builtin == "bailian":
                retrieval = {'tool': alias, 'arguments': dict(arguments)}
                result["evidence"] = [self.remote_evidence(task, project, server, item, "remote_document", retrieval=retrieval) for item in result.get("evidence", [])]
            elif config.builtin == "mysql":
                if not isinstance(result, dict) or result.get('success') is not True:
                    raise PlatformError('remote_query_failed', 'MySQL 只读查询失败，未将错误信息登记为有效依据。', 409)
                retrieval = {'tool': alias, 'arguments': dict(arguments)}
                result = {"result": result, "evidence": [self.remote_evidence(task, project, server, {"path": "mysql://"+server+"/"+str(arguments.get("database") or "catalog")+"/"+digest(arguments)[:16], "text": json.dumps(result, ensure_ascii=False, default=str), "title": tool.name}, "database", retrieval=retrieval)]}
            return result
        self.task_mcp.setdefault(task_id, {})[alias] = (schema, execute)
        canonical = "read_file" if config.builtin == "project_files" and tool.name == "read_file" else "list_files" if config.builtin == "project_files" else "rag_search" if config.builtin == "bailian" else "database_query" if config.builtin == "mysql" else "external_read"
        self.task_mcp_actions.setdefault(task_id, {})[alias] = canonical

    def remote_evidence(self, task, project, server, item, kind, *, retrieval=None):
        text = item.get("text", "")
        content_hash = digest(text.encode("utf-8"))
        source_id = "source_" + digest([task["id"], server, item["path"], content_hash])[:24]
        artifact = self.store.artifact(task, "remote-source.json", json.dumps(item, ensure_ascii=False), "application/json")
        evidence = Evidence(source_id=source_id, task_id=task["id"], project_id=project["id"], source_type=kind,
                            path=item["path"], start_line=1, end_line=max(1, len(text[:16000].splitlines())), content_hash=content_hash,
                            snippet=text[:16000], version=str(item.get("version", content_hash)), title=item.get("title", item["path"]))
        row = {"id": source_id, **evidence.model_dump(), "server": server, "snapshot_id": artifact["id"],
               "expires_epoch": time.time()+self.settings.remote_evidence_seconds, "binding_hash": self.mcp_runtime.binding_hash(server, project)}
        if retrieval is not None:
            row['retrieval'] = retrieval
        self.store.put("evidence", row)
        return evidence.model_dump()

    def controlled(self, actor, task_id, name, approval_id, subject_hash, callback):
        """仅供编排器调用；写入工具不进入模型可见工具列表。"""
        task = self.store.get("task", task_id)
        record = {"id": identifier("tool"), "task_id": task_id, "project_id": task["project_id"], "name": name, "user_id": actor.user_id, "input": {"approval_id": approval_id, "subject_hash": subject_hash}, "status": "requested", "policy_version": self.settings.policy_version}
        self.store.put("tool_call", record)
        started = time.monotonic()
        try:
            actor = self.current_actor(actor)
            project = self.store.get("project", task["project_id"])
            authorize(actor, project, name, task["mode"])
            if name in {"apply_patch", "run_tests"}:
                actor.require("developer")
            plan = self.store.get("plan", task["plan_id"])
            resources = [self.store.get('database_operation', task['database_operation_id'])['target']['database']] if name == 'drop_database' else plan['files']
            decision = self.policy(actor, task, project, name, resources)
            record["policy_decision"] = decision
            enforce(decision)
            approval = self.store.get("approval", approval_id)
            if name == 'drop_database' and (approval['kind'] != 'database' or approval['subject_id'] != task.get('database_operation_id')):
                raise PlatformError('database_approval_required', '删除数据库需要对应的独立审批，计划或文件审批不能代替。', 403)
            cleanup_started = name == 'drop_database' and self.store.get('database_operation', task['database_operation_id'])['status'] in {'dropping', 'database_deleted', 'accounts_deleted'}
            if approval["task_id"] != task_id or approval["subject_hash"] != subject_hash or approval["status"] != "approved":
                raise PlatformError("approval_required", "该操作没有有效的对应审批。", 403)
            if approval['expires_epoch'] < time.time() and not cleanup_started:
                raise PlatformError('approval_expired', '原操作审批已过期，请重新确认原计划；已生成内容保留。', 409)
            self.check_budget(task_id, tool=True)
            self.hook("PreToolUse", actor, task, {"name": name, "arguments": record["input"]})
            result = callback()
            self.hook("PostToolUse", actor, task, {"name": name, "result": result})
            record.update(status="succeeded", permission="allow", output=redact(result))
            return result
        except Exception as error:
            record.update(status="denied" if isinstance(error, PlatformError) and error.status == 403 else "failed", permission="deny", error={"code": getattr(error, "code", "execution_failed"), "message": getattr(error, "message", type(error).__name__)})
            raise
        finally:
            record["duration_ms"] = round((time.monotonic() - started) * 1000)
            self.store.put("tool_call", record)

    def langchain_tools(self, actor: Actor, task_id: str):
        from langchain_core.tools import StructuredTool
        descriptions = {"read_file": "读取已登记项目中的 UTF-8 文件并返回带路径、行号和 source_id 的证据。", "list_files": "列出当前项目中允许访问的文件。", "search_code": "按字面关键词搜索当前项目代码，返回证据引用。", "rag_search": "检索当前项目生效文档和业务规则，返回版本、来源与冲突。", "git_status": "只读查看项目 Git 状态。", "git_diff": "只读查看允许访问的项目代码 Diff。", "graph_query": "查询当前项目的代码调用链。"}
        descriptions["run_powershell"] = "在隔离的 PowerShell 7 中执行计划里已经审批的有限命令；命令和超时必须完全匹配 powershell_commands。返回真实 stdout/stderr 和退出码。"
        descriptions['read_tool_result'] = '按 artifact_id 分页读取本轮归档材料，或 test_failures/verification 中当前验证的 stdout_id、stderr_id 完整日志，以及本轮已批准命令的输出。offset、length 按字符计数。不能读取任意内部产物或其他任务记录；返回历史快照。'
        if self.mcp_runtime is not None:
            self.mcp_runtime.prepare(actor, self.store.get("task", task_id))
        result = []
        def make_call(tool_name):
            def call(**arguments):
                try:return self.invoke(actor, task_id, tool_name, arguments)
                except PlatformError as error:
                    # Ordinary lookup mistakes are visible to the model and remain
                    # failed in audit. Permission, cancellation and budget errors
                    # still stop execution at the authoritative gateway.
                    if error.code not in {'not_found','not_directory','line_range','encoding','binary_file','file_limit','git_query_failed','git_unavailable'}:raise
                    return {'status':'unavailable','error':{'code':error.code,'message':error.message},'retryable':True}
            return call
        for name, (schema, _) in {**self.tools, **self.task_mcp.get(task_id, {})}.items():
            task = self.store.get("task", task_id)
            if task.get("review_patch_id") and task.get("current_node") in {"CODE_REVIEW","GENERATE_PATCH"}:
                # MCP file servers still point at the original repository.
                # Review uses the native readers against the proposed checkout.
                if name == "run_powershell" or self.task_mcp_actions.get(task_id, {}).get(name) in {"read_file", "list_files"}:
                    continue
                if name in {"read_file", "list_files", "search_code"}:
                    descriptions[name] += " 当前阶段读取尚未写回的补丁副本，包含新建文件和上次生成的内容。"
            if name == "run_powershell":
                task = self.store.get("task", task_id)
                if task["mode"] != "approval_write" or not task.get("approved_files"):
                    continue
            result.append(StructuredTool.from_function(func=make_call(name), name=name, description=descriptions.get(name, "由服务端登记的只读 MCP 工具。"), args_schema=schema))
        return result


def re_search_relations(query):
    return any(word in query.lower() for word in ("调用", "影响", "call", "impact"))
