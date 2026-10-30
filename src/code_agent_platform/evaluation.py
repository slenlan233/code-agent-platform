from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

from .models import Mode, PlatformError, TaskInput, digest, timestamp


def evaluate(platform, actor, dataset=None):
    project = platform.create_demo(actor)
    candidate = Path(dataset) if dataset else Path.cwd() / "evals/core_cases.json"
    if candidate.exists():
        definition = json.loads(candidate.read_text(encoding="utf-8"))
    elif dataset:
        raise PlatformError("dataset_missing", "指定的验收集不存在。", 404)
    else:
        definition = json.loads(files("code_agent_platform").joinpath("data/core_cases.json").read_text(encoding="utf-8"))
    cases = []
    for index, case in enumerate(definition["cases"]):
        mode = Mode(case["mode"])
        prompt = case["prompt"]
        task = platform.create_task(actor, TaskInput(project_id=project["id"], user_input=prompt, mode=mode, engine="demo"))
        try:
            result = platform.run(actor, task["id"])
            passed = result["status"] == "completed" and result["summary"]["test_status"] == "not_run" and bool(result.get("evidence_ids"))
            cases.append({"id": case["id"], "kind": "workflow", "task_id": task["id"], "passed": passed})
        except PlatformError as error:
            cases.append({"id": case["id"], "kind": "workflow", "task_id": task["id"], "passed": False, "error": error.code})
    attacks = [("read_file", {"path": path}) for path in (".env", ".env.production", ".ssh/id_rsa", "secrets/key.txt", ".git/config", "../outside", "/etc/passwd", "C:/Windows/win.ini", "file.txt:secret", ".git /config")]
    attacks += [(tool, {}) for tool in ("WriteFile", "EditFile", "run_shell", "Bash", "git_push", "mysql_execute_command", "deploy", "apply_patch", "run_tests", "export_patch")]
    for index, (tool, arguments) in enumerate(attacks):
        task = platform.create_task(actor, TaskInput(project_id=project["id"], user_input="固定权限反测", engine="demo"))
        passed, error_code = False, None
        try:
            platform.gateway.invoke(actor, task["id"], tool, arguments)
        except PlatformError as error:
            passed = error.status == 403
            error_code = error.code
        cases.append({"id": f"negative_{index + 1:02d}", "kind": "policy", "task_id": task["id"], "passed": passed, "error": error_code})
    report = {"project_id": project["id"], "scope": "workflow_and_policy", "engine": "demo", "model_accuracy": None, "human_time_savings": None, "dataset_version": definition["version"], "dataset_hash": digest(definition), "versions": platform.settings.versions(), "evaluated_at": timestamp(), "total": len(cases), "passed": sum(item["passed"] for item in cases), "failed": sum(not item["passed"] for item in cases), "cases": cases}
    return platform.store.put("evaluation", report)


def evaluate_model(platform, actor, dataset=None, *, repeats=1):
    """Real provider, isolated fixtures, deterministic checks, no auto-approvals.

    Deliberately measures read/plan/memory reliability, not end-to-end coding
    success. The offline governance dataset keeps its separate meaning.
    """
    from .models import ProjectInput, identifier
    from .security import redact
    import time
    if not 1 <= repeats <= 20:
        raise PlatformError('evaluation_input', '重复次数应为 1—20。')
    definition = json.loads(Path(dataset).read_text(encoding='utf-8')) if dataset else json.loads(
        files('code_agent_platform').joinpath('data/model_cases.json').read_text(encoding='utf-8'))
    cases = definition.get('cases', [])
    if not cases or len(cases) > 100 or any(c.get('fixture') not in {'python', 'java'} or
            c.get('mode') not in {'readonly', 'plan_only'} or not c.get('prompt') for c in cases):
        raise PlatformError('evaluation_input', '真实模型验收仅支持隔离 Python/Java 仓库中的问答和计划；不自动批准修改。')
    run_id = identifier('evaluation')
    results = []
    for repeat in range(repeats):
        projects, sessions = {}, {}
        for case in cases:
            fixture = case['fixture']
            if fixture not in projects:
                root = (platform.settings.data_dir/'model-evals'/run_id/str(repeat)/fixture).resolve()
                if not any(root.is_relative_to(p.resolve()) for p in platform.settings.allowed_roots):
                    raise PlatformError('project_root', '验收数据目录必须在管理员允许的项目根目录内。', 403)
                root.mkdir(parents=True, exist_ok=False)
                contents = {'calculator.py': 'def add(a: int, b: int) -> int:\n    return a + b\n',
                    'README.md': 'Use python -m unittest discover -s tests. The add function adds two integers.\n'} if fixture == 'python' else {
                    'src/main/java/demo/TodoService.java': 'package demo;\npublic class TodoService {\n    public String normalizeTitle(String title) {\n        if (title == null || title.isBlank()) throw new IllegalArgumentException("empty title");\n        return title.strip();\n    }\n}\n',
                    'README.md': 'Java 17 Todo service. normalizeTitle rejects null and blank titles and strips whitespace.\n'}
                for path, content in contents.items():
                    target = root/path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, encoding='utf-8')
                projects[fixture] = platform.register_project(actor, ProjectInput(name='模型验收 '+fixture,
                    root=str(root), allow_write=False, mcp_servers=[]))
            project = projects[fixture]
            session_key = (fixture, case.get('session', case['id']))
            if session_key not in sessions:
                sessions[session_key] = platform.conversations.create(actor, project['id'], '真实模型验收')
            task = platform.create_task(actor, TaskInput(project_id=project['id'],
                session_id=sessions[session_key]['id'], mode=case['mode'], engine='model', user_input=case['prompt']))
            started = time.monotonic()
            error_code = None
            try:
                result = platform.run(actor, task['id'])
            except PlatformError as error:
                error_code = error.code
                result = platform.store.get('task', task['id'])
            draft = result.get('answer') or (platform.store.get('plan', result['plan_id']) if result.get('plan_id') else {})
            text = json.dumps(draft, ensure_ascii=False)
            checks = {'completed': result['status'] == 'completed', 'structured_result': bool(draft),
                      'grounded': bool(result.get('evidence_ids')), 'no_project_write': not result.get('applied_patch_hash'),
                      'expected_terms': all(term.lower() in text.lower() for term in case.get('expected_terms', []))}
            if case['mode'] == 'plan_only':
                checks['nonempty_plan'] = bool(draft.get('steps')) and bool(draft.get('files')) and bool(draft.get('test_plan'))
            results.append({'id': case['id'], 'repeat': repeat+1, 'fixture': fixture, 'task_id': task['id'],
                'passed': all(checks.values()), 'checks': checks, 'error_code': error_code,
                'failure_category': result.get('failure_category'), 'usage': result['usage'],
                'duration_ms': round((time.monotonic()-started)*1000)})
    report = {'id': run_id, 'engine': 'model', 'scope': 'real_model_readonly_plan_multiturn',
        'dataset_version': definition['version'], 'dataset_hash': digest(definition), 'versions': platform.settings.versions(),
        'evaluated_at': timestamp(), 'repeats': repeats, 'total': len(results), 'passed': sum(r['passed'] for r in results),
        'failed': sum(not r['passed'] for r in results), 'cases': results, 'model_accuracy': None,
        'limitations': ['这是固定场景通过率，不是通用模型准确率或生产成功率。',
            '不自动批准写入，不代替真实项目的生成、Docker/MySQL 测试及人工验收。'],
        'input_tokens': sum(r['usage']['input_tokens'] for r in results),
        'output_tokens': sum(r['usage']['output_tokens'] for r in results)}
    return platform.store.put('evaluation', redact(report))
