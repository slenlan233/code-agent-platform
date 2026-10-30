from .models import PlatformError, digest


def require_database_deletion(task, store):
    operation = store.get('database_operation', task.get('database_operation_id', ''))
    approval = store.get('approval', 'approval_'+digest([task['id'], 'database', operation['hash']])[:32])
    if operation['task_id'] != task['id'] or operation['status'] != 'completed' or approval['kind'] != 'database' or approval['subject_id'] != operation['id'] or approval['subject_hash'] != operation['hash'] or approval['status'] != 'approved':
        raise PlatformError('database_deletion_required', '数据库操作尚未完成对应审批和实际执行。', 403)


def require_ai_check(task,store):
    patch=store.get('patch',task['patch_id'])
    check=task.get('ai_check')
    if check is None and not task.get('ai_check_required'):
        # Historical completed tasks retain their recorded review; new tasks
        # always require an explicit check bound to the current patch.
        if task.get('review') and not any(i['severity']=='blocking' for i in task['review'].get('issues',[])):return
    if not check or check.get('status')!='passed' or check.get('patch_id')!=patch['id'] or check.get('patch_hash')!=patch['hash'] or any(i['severity']=='blocking' for i in (task.get('review') or {}).get('issues',[])):
        raise PlatformError('ai_check_required','当前改动尚未通过 AI 代码检查，不能确认、交付或写入。',403)
    verification=task.get('verification') or {}
    if verification.get('patch_id')!=patch['id'] or verification.get('status')!='passed' or verification.get('exit_code')!=0:
        raise PlatformError('verification_required','当前改动尚未通过实际测试。',403)
