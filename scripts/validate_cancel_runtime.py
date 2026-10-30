"""Opt-in real Docker cancellation smoke test; no model or user project writes."""
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time

from code_agent_platform.config import Settings
from code_agent_platform.models import PlatformError, identifier
from code_agent_platform.runtime import DockerRunner


def main():
    root = Path(__file__).resolve().parents[1]
    settings = Settings(data_dir=root/'.temp/cancel-proof', allowed_roots=[root])
    stop = threading.Event()
    task = {'id': identifier('task'), 'budget': {'max_seconds': 120}}
    def check(_):
        if stop.is_set():
            raise PlatformError('task_cancelled', 'Smoke test stopped', 409)
    runner = DockerRunner(settings, check_cancelled=check)
    if not runner.health()['available']:
        raise RuntimeError('Docker is unavailable')
    result = {}
    with tempfile.TemporaryDirectory(prefix='cancel-payload-', dir=root/'.temp') as folder:
        project = {'sandbox_image': 'python:3.12-slim', 'database_test_profile': 'none',
                   'test_command': ['python', '-c', 'import time; print("ready", flush=True); time.sleep(120)']}
        def execute():
            try:
                result.update(runner._verify_container(task, project, {'path': folder}))
            except PlatformError as error:
                result['error_code'] = error.code
        worker = threading.Thread(target=execute)
        worker.start()
        try:
            deadline = time.monotonic()+25
            while time.monotonic() < deadline:
                state = subprocess.run(['docker', 'ps', '-q', '--filter', 'label=com.ai-agent.task='+task['id']], capture_output=True, timeout=5)
                if state.returncode == 0 and state.stdout.strip():
                    break
                time.sleep(0.2)
            else:
                raise RuntimeError('Owned test container did not start')
            started = time.monotonic()
            stop.set()
            worker.join(20)
            if worker.is_alive():
                raise RuntimeError('Cancellation did not finish')
            result['stop_duration_ms'] = round((time.monotonic()-started)*1000)
        finally:
            stop.set()
            worker.join(20)
            result['cleanup'] = runner.cleanup_task(task['id'])
    result.update(task_id=task['id'], backend='real_local_docker', passed=result.get('error_code') == 'task_cancelled' and result['cleanup']['verified'])
    (root/'.temp/reliability-docker-cancel-proof.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
    if not result['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
