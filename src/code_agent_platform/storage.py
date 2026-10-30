from __future__ import annotations

import json
import os
import secrets
import socket
import time
from pathlib import Path
from threading import RLock

from sqlalchemy import JSON, Column, Float, Integer, MetaData, String, Table, create_engine, delete, insert, select, update

from .models import PlatformError, digest, identifier, timestamp
from .security import redact


TABLES = ("project", "project_selection", "project_database", "database_operation", "session", "message", "task", "event", "tool_call", "model_call", "evidence", "plan", "approval", "patch", "sandbox_run", "review_run", "command_run", "artifact", "evaluation", "feedback", "graph")


class Store:
    def __init__(self, data_dir: Path, database_url: str | None = None):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = data_dir
        self.lock = RLock()
        url = database_url or f"sqlite:///{(data_dir / 'platform.sqlite').as_posix()}"
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        self.engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30} if url.startswith("sqlite:") else {}, pool_pre_ping=True)
        metadata = MetaData()
        self.tables = {name: Table(f"agent_{name}", metadata,
            Column("id", String(100), primary_key=True), Column("project_id", String(100), index=True),
            Column("task_id", String(100), index=True), Column("status", String(60), index=True),
            Column("data", JSON, nullable=False), Column("revision", Integer, default=0, nullable=False),
            Column("lease_id", String(100), default=""), Column("lease_until", Float, default=0),
        ) for name in TABLES}
        metadata.create_all(self.engine)
        self.artifact_dir = data_dir / "artifacts"
        self.artifact_dir.mkdir(exist_ok=True)

    def put(self, kind: str, data: dict) -> dict:
        row = dict(data)
        row.setdefault("id", identifier(kind))
        row.setdefault("created_at", timestamp())
        row["updated_at"] = timestamp()
        table = self.tables[kind]
        values = {"id": row["id"], "project_id": row.get("project_id", row["id"] if kind == "project" else None), "task_id": row.get("task_id", row["id"] if kind == "task" else None), "status": row.get("status"), "data": row}
        with self.lock, self.engine.begin() as connection:
            existing = connection.execute(select(table.c.revision).where(table.c.id == row["id"])).first()
            if existing is None:
                connection.execute(insert(table).values(**values))
            else:
                connection.execute(update(table).where(table.c.id == row["id"]).values(**values, revision=existing[0] + 1))
        return row

    def get(self, kind: str, row_id: str) -> dict:
        with self.engine.connect() as connection:
            row = connection.execute(select(self.tables[kind].c.data).where(self.tables[kind].c.id == row_id)).first()
        if row is None:
            raise PlatformError("not_found", "记录不存在。", 404)
        return row[0]

    def list(self, kind: str, *, project_id: str | None = None, task_id: str | None = None) -> list[dict]:
        table = self.tables[kind]
        statement = select(table.c.data)
        if project_id is not None:
            statement = statement.where(table.c.project_id == project_id)
        if task_id is not None:
            statement = statement.where(table.c.task_id == task_id)
        with self.engine.connect() as connection:
            return [row[0] for row in connection.execute(statement)]

    def mutate(self, kind: str, row_id: str, function) -> dict:
        table = self.tables[kind]
        with self.lock, self.engine.begin() as connection:
            result = connection.execute(select(table.c.data, table.c.revision).where(table.c.id == row_id).with_for_update()).first()
            if result is None:
                raise PlatformError("not_found", "记录不存在。", 404)
            data, revision = result
            data = function(data)
            data["updated_at"] = timestamp()
            changed = connection.execute(update(table).where(table.c.id == row_id, table.c.revision == revision).values(data=data, status=data.get("status"), revision=revision + 1))
            if changed.rowcount != 1:
                raise PlatformError("concurrent_update", "任务状态已变化，请刷新重试。", 409)
            return data

    def event(self, task_id: str, kind: str, detail: dict) -> dict:
        task = self.get("task", task_id)
        return self.put("event", {"task_id": task_id, "project_id": task["project_id"], "kind": kind, "detail": redact(detail)})

    def acquire(self, task_id: str, *, kind="task") -> str:
        import psutil
        host = digest(socket.gethostname())[:12]
        token = f"{host}:{os.getpid()}:{psutil.Process().create_time()}:{identifier('lease')[6:]}"
        table = self.tables[kind]
        with self.engine.begin() as connection:
            saved = connection.execute(select(table.c.lease_id, table.c.lease_until).where(table.c.id == task_id).with_for_update()).first()
            if saved is None:
                raise PlatformError("not_found", "任务不存在。", 404)
            previous, until = saved
            stale = until < time.time()
            parts = (previous or "").split(":")
            if not stale and len(parts) == 4 and parts[0] == host:
                try:
                    stale = abs(psutil.Process(int(parts[1])).create_time() - float(parts[2])) > 0.01
                except psutil.NoSuchProcess:
                    stale = True
                except (psutil.AccessDenied, ValueError):
                    stale = False
            if not stale:
                raise PlatformError("task_busy", "任务正在执行，请等待或稍后恢复。", 409)
            changed = connection.execute(update(table).where(table.c.id == task_id, table.c.lease_id == previous).values(lease_id=token, lease_until=time.time() + 4000))
            if changed.rowcount != 1:
                raise PlatformError("task_busy", "任务正在执行，请等待或稍后恢复。", 409)
        return token

    def release(self, task_id: str, token: str, *, kind="task"):
        table = self.tables[kind]
        with self.engine.begin() as connection:
            connection.execute(update(table).where(table.c.id == task_id, table.c.lease_id == token).values(lease_id="", lease_until=0))

    def has_live_lease(self, row_id, *, kind='task'):
        import psutil
        table = self.tables[kind]
        with self.engine.connect() as connection:
            row = connection.execute(select(table.c.lease_id, table.c.lease_until).where(table.c.id == row_id)).first()
        if not row or not row[0] or row[1] < time.time():return False
        parts = row[0].split(':')
        if len(parts) == 4 and parts[0] == digest(socket.gethostname())[:12]:
            try:return abs(psutil.Process(int(parts[1])).create_time()-float(parts[2])) <= 0.01
            except (psutil.NoSuchProcess, ValueError):return False
            except psutil.AccessDenied:return True
        return True

    def artifact(self, task: dict, name: str, content: str, media_type: str = "text/plain") -> dict:
        # 名称只来自服务端常量，不将模型路径拼接到 artifact 目录。
        artifact_id = identifier("artifact")
        folder = self.artifact_dir / task["id"]
        folder.mkdir(exist_ok=True)
        data = content.encode("utf-8")
        target = folder / artifact_id
        target.write_bytes(data)
        return self.put("artifact", {"id": artifact_id, "task_id": task["id"], "project_id": task["project_id"], "name": name, "media_type": media_type, "sha256": digest(data), "size": len(data), "relative_path": target.relative_to(self.artifact_dir).as_posix()})

    def artifact_content(self, artifact_id: str) -> str:
        record = self.get("artifact", artifact_id)
        path = (self.artifact_dir / record["relative_path"]).resolve()
        if not path.is_relative_to(self.artifact_dir.resolve()) or path.is_symlink():
            raise PlatformError("artifact_denied", "无效的任务产物。", 403)
        data = path.read_bytes()
        if digest(data) != record["sha256"]:
            raise PlatformError("artifact_changed", "任务产物校验失败。", 409)
        return data.decode("utf-8")

    def local_token(self) -> str:
        path = self.data_dir / "access-token"
        if not path.exists():
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(secrets.token_urlsafe(32))
            except FileExistsError:
                pass
        return path.read_text(encoding="utf-8").strip()

    def close(self):
        self.engine.dispose()
