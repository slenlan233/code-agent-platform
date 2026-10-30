"""Verify an explicitly requested complete source reset without running code."""
from pathlib import Path

from .intent import requests_project_reset
from .models import PlatformError
from .security import files


def is_reset(task, plan):
    mode = plan.get('verification_mode')
    # Legacy approved reset plans already explicitly described a file-list check.
    return mode == 'project_reset' or mode is None and requests_project_reset(task['user_input'])


def reset_paths(settings, project):
    return {name for name, _ in files(Path(project['root']), limit=settings.max_scan_files)} - set(project['rules'])


def check_reset_plan(settings, task, project, plan):
    if not requests_project_reset(task['user_input']):
        raise PlatformError('reset_intent_required', '文件清空验证仅用于用户明确要求清空整个项目的任务。')
    if set(plan['files']) != reset_paths(settings, project) or plan.get('requires_database') or plan.get('database_change') and plan.get('database_action') != 'drop_project_database':
        raise PlatformError('reset_scope', '清空计划必须列出全部可管理文件；受保护配置和项目规则保留，数据库不在清空范围。')


def verify_reset(settings, task, project, plan, changes, workspace):
    check_reset_plan(settings, task, project, plan)
    expected = set(plan['files'])
    deleted = {item['path'] for item in changes if item['content'] is None}
    remaining = {name for name, _ in files(Path(workspace['path']), limit=settings.max_scan_files)} - set(project['rules'])
    passed = deleted == expected and len(changes) == len(expected) and not remaining
    reason = ('文件清空验证通过；未运行应用构建或集成测试。' if passed else
              '补丁不符合已批准的清空计划：所有批准文件必须以 content=null 删除，不得为通过构建而恢复空骨架或修改测试。')
    return {'status': 'passed' if passed else 'failed', 'exit_code': 0 if passed else 1,
            'backend': 'file_manifest', 'verification_kind': 'project_reset', 'network': 'none',
            'command': [], 'test_count': 0, 'checked_file_count': len(expected), 'deleted_file_count': len(deleted),
            'reason': reason, 'stdout': reason, 'stderr': '', 'duration_ms': 0}
