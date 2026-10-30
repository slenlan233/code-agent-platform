from __future__ import annotations

import fnmatch
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Any

from .models import Actor, Mode, PlatformError


EXCLUDED = {".git", ".venv", "node_modules", "__pycache__", ".temp", ".idea", ".pytest_cache", "selenium_chrome_profile", "dist", "build", "target"}
SENSITIVE = {".ssh", "secrets", "credentials", ".aws", ".codex", ".agents"}
SECRET = re.compile(r"(?:sk-[A-Za-z0-9_-]{16,}|LTAI[A-Za-z0-9]{12,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)")


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: "[redacted]" if any(word in key.lower() for word in ("api_key", "password", "authorization", "access_token", "secret")) else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return SECRET.sub("[redacted]", value)
    return value


def relative_path(value: str) -> str:
    value = value.replace("\\", "/")
    parts = PurePosixPath(value).parts
    if not value or "\x00" in value or value.startswith("/") or any(part in {"..", ""} or ":" in part or part.endswith((" ", ".")) and part != "." for part in parts):
        raise PlatformError("path_denied", "路径必须位于已登记项目中。", 403)
    return PurePosixPath(value).as_posix()


def blocked(path: str) -> bool:
    parts = [part.lower() for part in PurePosixPath(path).parts]
    if any(part in EXCLUDED | SENSITIVE for part in parts):
        return True
    name = parts[-1] if parts else ""
    return (name.startswith(".env") and name != ".env.example") or name in {".netrc", ".npmrc", ".pypirc", ".git-credentials", "id_rsa", "id_ed25519", "credentials.json", "service-account.json", "prod.yml", "prod.yaml", "production.yml", "production.yaml"} or name.endswith((".pem", ".key", ".p12", ".pfx")) or any(fnmatch.fnmatch(path.lower(), pattern) for pattern in ("**/prod.yml", "**/production.yml", "**/prod.yaml", "**/production.yaml"))


def safe_path(root: Path, value: str, *, must_exist: bool = True) -> Path:
    value = relative_path(value)
    if blocked(value):
        raise PlatformError("sensitive_path", "该路径不在允许访问的项目范围内。", 403)
    root = root.resolve()
    current = root
    for part in PurePosixPath(value).parts:
        current = current / part
        if current.exists() or current.is_symlink():
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                raise PlatformError("link_denied", "不允许通过链接访问项目文件。", 403)
    result = current.resolve()
    if not result.is_relative_to(root):
        raise PlatformError("path_denied", "路径超出项目范围。", 403)
    if must_exist and not result.exists():
        raise PlatformError("not_found", "项目文件不存在。", 404)
    return result


def files(root: Path, *, limit: int = 4000):
    count = 0
    for directory, names, filenames in os.walk(root, followlinks=False):
        allowed = []
        for name in sorted(names):
            candidate = (Path(directory) / name).relative_to(root).as_posix()
            try:
                if not blocked(candidate) and safe_path(root, candidate).is_dir():
                    allowed.append(name)
            except PlatformError:
                continue
        names[:] = allowed
        for name in sorted(filenames):
            candidate = (Path(directory) / name).relative_to(root).as_posix()
            try:
                path = safe_path(root, candidate)
                if not path.is_file():
                    continue
                count += 1
                if count > limit:
                    raise PlatformError("scan_limit", "项目扫描达到文件数量上限，请缩小项目范围。")
                yield candidate, path
            except PlatformError as error:
                if error.code == "scan_limit":
                    raise


def require_active_record(row, label='项目'):
    if row.get('lifecycle', 'active') != 'active':
        raise PlatformError('record_inactive', label+'已归档或移入回收站，请先恢复后再操作。', 409)


def authorize(actor: Actor, project: dict, action: str, mode: str) -> str:
    if not actor.allows(project["id"]):
        raise PlatformError("project_denied", "没有该项目的访问权限。", 403)
    require_active_record(project)
    if not project.get("enabled", True):
        raise PlatformError("project_disabled", "该项目已关闭。", 403)
    if project.get("root_identity"):
        root=Path(project["root"])
        try:
            info=root.stat()
            changed=root.resolve()!=root or root.is_symlink() or {"device":info.st_dev,"inode":info.st_ino}!=project["root_identity"]
        except OSError:
            changed=True
        if changed:
            raise PlatformError("project_root_changed", "已选项目文件夹已被替换或移动，请重新登记。",409)
    readonly = {"read_file", "list_files", "search_code", "rag_search", "git_status", "git_diff", "graph_query"}
    if action in readonly:
        return "allow"
    if action in {"apply_patch", "run_tests", "export_patch", "run_powershell", "prepare_database", "drop_database"}:
        if mode != Mode.WRITE or not project.get("allow_write", False):
            raise PlatformError("mode_denied", "当前项目或任务模式不允许执行修改。", 403)
        return "ask"
    raise PlatformError("tool_denied", "该工具未在当前策略中登记。", 403)
