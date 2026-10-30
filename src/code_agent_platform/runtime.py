from __future__ import annotations

import os
import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .models import PlatformError, digest, identifier
from .security import files, redact, safe_path
from .git_support import git_environment, repository_info


class Workspace:
    def __init__(self, settings, *, namespace="workspaces"):
        self.settings = settings
        self.root = settings.data_dir / namespace
        self.root.mkdir(parents=True, exist_ok=True)

    def directory(self, task_id):
        if not re.fullmatch(r"task_[0-9a-f]{32}", task_id):
            raise PlatformError("workspace_id", "无效的工作区标识。")
        path = self.root / task_id
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise PlatformError("workspace_path", "工作区位置不合法。")
        return path

    def prepare(self, task, project, changes, *, allowed=None):
        base = self.directory(task["id"])
        if base.exists():
            self.cleanup(task["id"])
        base.mkdir()
        checkout = base / "checkout"
        kind = "snapshot"
        try:
            if not repository_info(project).get('revision'):
                raise subprocess.SubprocessError('No committed Git repository in selected project')
            mirror = base / "repository.git"
            subprocess.run(["git", "clone", "--bare", "--no-hardlinks", "--", project["root"], str(mirror)], check=True, capture_output=True, timeout=30,env=git_environment())
            subprocess.run(["git", "--git-dir", str(mirror), "-c", "core.hooksPath=" + str(base / "no-hooks"), "worktree", "add", "--detach", "--no-checkout", str(checkout), "HEAD"], check=True, capture_output=True, timeout=20,env=git_environment())
            kind = "git_worktree"
        except (subprocess.SubprocessError, OSError):
            checkout.mkdir(exist_ok=True)
        total = 0
        for name, source in files(Path(project["root"]), limit=self.settings.max_scan_files):
            if allowed is not None and not allowed(name, source):
                continue
            if source.stat().st_size > self.settings.max_file_bytes:
                continue
            total += source.stat().st_size
            if total > 50_000_000:
                raise PlatformError("workspace_size", "验证工作区超过 50 MB 限制。")
            target = checkout / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
        payload = base / "payload"
        payload.mkdir()
        for name, source in files(checkout, limit=self.settings.max_scan_files):
            target = payload / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
        for change in changes:
            target = safe_path(payload, change["path"], must_exist=False)
            if change["content"] is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(change["content"].encode("utf-8"))
        return {"path": str(payload), "kind": kind, "fingerprint": self.fingerprint(payload), "task_id":task["id"]}

    @staticmethod
    def fingerprint(root):
        return {name: digest(path.read_bytes()) for name, path in files(root) if path.stat().st_size < 1_000_000}

    def cleanup(self, task_id):
        path = self.directory(task_id)
        # 递归删除前确认绝对路径处于专用工作区内，且不是工作区根目录。
        if path.exists() and path.resolve().is_relative_to(self.root.resolve()) and path.resolve() != self.root.resolve() and not path.is_symlink():
            shutil.rmtree(path)


class DockerRunner:
    def __init__(self, settings, *, check_cancelled=None):
        self.settings = settings
        self.check_cancelled = check_cancelled or (lambda task: None)

    def health(self):
        try:
            result = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, timeout=10)
            return {"available": result.returncode == 0, "version": result.stdout.decode(errors="replace").strip() if result.returncode == 0 else None}
        except (OSError, subprocess.TimeoutExpired):
            return {"available": False, "version": None}

    def cleanup_task(self, task_id):
        """Reap only resources carrying both our owner and this task's label."""
        if not re.fullmatch(r'task_[0-9a-f]{32}', task_id):
            raise PlatformError('task_id', '任务标识不正确。')
        removed = []
        try:
            for kind, listing in (('container', ['ps', '-aq']), ('network', ['network', 'ls', '-q'])):
                found = subprocess.run(['docker', *listing, '--filter', 'label=com.ai-agent.task='+task_id], capture_output=True, timeout=10)
                if found.returncode:
                    return {'verified': False, 'removed': removed, 'reason': 'Docker 不可用，无法核验残留测试资源。'}
                ids = found.stdout.decode().split()
                if len(ids) > 50:
                    return {'verified': False, 'removed': removed, 'reason': '任务资源数量异常，需要人工检查。'}
                for resource in ids:
                    inspected = subprocess.run(['docker', kind, 'inspect', resource], capture_output=True, timeout=10)
                    if inspected.returncode:
                        return {'verified': False, 'removed': removed, 'reason': '无法核验测试资源的归属。'}
                    detail = json.loads(inspected.stdout)[0]
                    labels = detail.get('Config', {}).get('Labels', {}) if kind == 'container' else detail.get('Labels', {})
                    if labels.get('com.ai-agent.owner') not in {'task-runtime', 'database-test'} or labels.get('com.ai-agent.task') != task_id:
                        return {'verified': False, 'removed': removed, 'reason': '资源归属不匹配，未执行删除。'}
                    command = ['rm', '-f', detail['Id']] if kind == 'container' else ['network', 'rm', detail['Id']]
                    result = subprocess.run(['docker', *command], capture_output=True, timeout=10)
                    if result.returncode:
                        return {'verified': False, 'removed': removed, 'reason': '部分测试资源未清理，请检查 Docker。'}
                    removed.append({'kind': kind, 'id': detail['Id']})
            return {'verified': True, 'removed': removed}
        except (OSError, subprocess.SubprocessError, ValueError, KeyError):
            return {'verified': False, 'removed': removed, 'reason': '测试资源清理未完成，请检查 Docker。'}

    def command(self, project, workspace, name):
        if project["sandbox_image"] not in self.settings.sandbox_images:
            raise PlatformError("image_denied", "该运行镜像未在平台中登记。", 403)
        mount = f"type=bind,source={workspace['path']},target=/workspace" + (",readonly" if workspace.get("read_only") else "")
        network=workspace.get('database_network','none')
        if network!='none' and (not self.settings.allow_mysql_test_network or project.get('database_test_profile')!='mysql' or not re.fullmatch(r'ai-agent-db-[0-9a-f]{32}',network)):
            raise PlatformError('network_denied','测试网络未获登记。',403)
        options=[]
        if network!='none':options=['--env-file',workspace['database_env_file'],'--env=HOME=/tmp']
        return ["docker", "run", "--name", name, "--pull=never", "--network="+network, "--cpus=2", "--memory="+('1g' if network!='none' else '512m'), "--pids-limit="+('256' if network!='none' else '128'), "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--user=65534:65534", "--tmpfs=/tmp:rw,nosuid,noexec,size=128m", "--mount", mount, "--workdir=/workspace", "--env=PYTHONDONTWRITEBYTECODE=1", *options, project["sandbox_image"], *project["test_command"]]

    def verify(self, task, project, workspace):
        if project.get('database_test_profile')=='mysql':
            from .database_runtime import verify_with_mysql
            return verify_with_mysql(self,task,project,workspace)
        return self._verify_container(task,project,workspace)

    def _verify_container(self,task,project,workspace):
        self.check_cancelled(task)
        if not self.health()["available"]:
            return {"status": "unavailable", "reason": "Docker 服务不可用，未在宿主机执行替代命令。", "exit_code": None, "stdout": "", "stderr": ""}
        image = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", project["sandbox_image"]], capture_output=True, timeout=10)
        if image.returncode:
            return {"status": "unavailable", "reason": "登记的运行镜像尚未准备好。", "exit_code": None, "stdout": "", "stderr": ""}
        image_digest = image.stdout.decode().strip()
        name = workspace.get('container_name') or "ai-agent-" + identifier("run").split("_", 1)[1]
        command = self.command(project, workspace, name)
        command[2:2] = ['--label', 'com.ai-agent.owner=task-runtime', '--label', 'com.ai-agent.task='+task['id']]
        command[command.index(project["sandbox_image"])] = image_digest
        started = time.monotonic()
        timeout = min(self.settings.sandbox_timeout, task["budget"]["max_seconds"])
        process = None
        timed_out = False
        output_limit = False
        try:
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                process = subprocess.Popen(command, stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL)
                while process.poll() is None:
                    self.check_cancelled(task)
                    if time.monotonic() - started > timeout:
                        timed_out = True
                        break
                    if os.fstat(stdout.fileno()).st_size + os.fstat(stderr.fileno()).st_size > 2_000_000:
                        output_limit = True
                        break
                    try:
                        process.wait(timeout=0.2)
                    except subprocess.TimeoutExpired:
                        pass
                if timed_out or output_limit:
                    subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=10)
                    process.kill()
                    process.wait(timeout=5)
                stdout.seek(0)
                stderr.seek(0)
                out = redact(stdout.read(200_000).decode("utf-8", errors="replace"))
                err = redact(stderr.read(200_000).decode("utf-8", errors="replace"))
                status = "passed" if process.returncode == 0 and not timed_out and not output_limit else "failed"
                match = re.search(r"Ran (\d+) tests?", out + err)
                if match and int(match.group(1)) == 0:
                    status = "failed"
                from .junit_reports import read_junit_reports
                report=read_junit_reports(workspace['path'])
                if report and (report['tests']<=report['skipped'] or report['failures'] or report['errors']):status='failed'
                return {"status": status, "exit_code": process.returncode, "stdout": out, "stderr": err, "timed_out": timed_out, "output_limit": output_limit, "duration_ms": round((time.monotonic() - started) * 1000), "test_count": report['tests'] if report else int(match.group(1)) if match else None, "test_report":report,"command": project["test_command"], "image": project["sandbox_image"], "image_digest": image_digest, "network": "mysql_internal" if workspace.get('database_network') else "none", "backend": "docker"}
        finally:
            try:
                subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                pass
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
