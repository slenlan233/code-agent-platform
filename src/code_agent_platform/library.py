"""Record lifecycle; project files, databases and audit history are preserved."""
from .models import PlatformError, timestamp
from .conversations import is_active


def state(row):
    return row.get('lifecycle', 'active')


class WorkspaceLibrary:
    def __init__(self, platform):
        self.platform, self.store = platform, platform.store

    def project_record(self, actor, project_id):
        actor = self.platform.gateway.current_actor(actor)
        project = self.store.get('project', project_id)
        if not actor.allows(project_id):
            raise PlatformError('project_denied', '没有该项目的访问权限。', 403)
        if not project.get('enabled', True) and 'admin' not in actor.roles:
            raise PlatformError('project_disabled', '该项目已关闭。', 403)
        return project

    def session_record(self, actor, session_id):
        actor = self.platform.gateway.current_actor(actor)
        session = self.store.get('session', session_id)
        self.project_record(actor, session['project_id'])
        if session['owner_id'] != actor.user_id and 'admin' not in actor.roles:
            raise PlatformError('session_denied', '该对话不属于当前用户。', 403)
        return session

    def project_summary(self, actor, project):
        sessions = self.sessions(actor, project['id'])
        return {key: project.get(key) for key in ('id', 'name', 'root', 'created_at', 'updated_at', 'archived_at', 'deleted_at', 'deleted_from') } | {
            'kind': 'project', 'lifecycle': state(project), 'session_count': len([s for s in sessions if s['lifecycle'] != 'deleted'])}

    def sessions(self, actor, project_id):
        actor = self.platform.gateway.current_actor(actor)
        self.project_record(actor, project_id)
        rows = []
        for session in self.store.list('session', project_id=project_id):
            if session['owner_id'] != actor.user_id and 'admin' not in actor.roles or session.get('hidden_in_list'):
                continue
            description = self.platform.conversations.describe(session)
            rows.append({**description, 'kind': 'session', 'lifecycle': state(session)})
        return sorted(rows, key=lambda row: row['updated_at'], reverse=True)

    def list(self, actor, view='archived'):
        actor = self.platform.gateway.current_actor(actor)
        wanted = 'deleted' if view == 'trash' else 'archived'
        projects, sessions = [], []
        for project in self.store.list('project'):
            if not actor.allows(project['id']) or not project.get('enabled', True) and 'admin' not in actor.roles:
                continue
            if state(project) == wanted:
                projects.append(self.project_summary(actor, project))
            if state(project) == 'deleted':
                continue
            for session in self.sessions(actor, project['id']):
                if session['lifecycle'] == wanted:
                    sessions.append({**session, 'project_name': project['name'], 'project_lifecycle': state(project)})
        return {'projects': sorted(projects, key=lambda p:p['updated_at'], reverse=True),
                'sessions': sorted(sessions, key=lambda s:s['updated_at'], reverse=True)}

    def project_detail(self, actor, project_id):
        project = self.project_record(actor, project_id)
        return {'project': self.project_summary(actor, project), 'sessions': self.sessions(actor, project_id)}

    def session_detail(self, actor, session_id, *, before=None):
        session = self.session_record(actor, session_id)
        project = self.project_record(actor, session['project_id'])
        result = self.platform.conversations.detail(actor, session_id, allow_inactive=True, limit=300, before=before)
        result['project'] = self.project_summary(actor, project)
        result['read_only'] = True
        return result

    def change(self, actor, kind, row_id, action):
        actor = self.platform.gateway.current_actor(actor)
        actor.require('admin' if kind == 'project' else 'developer')
        row = self.project_record(actor, row_id) if kind == 'project' else self.session_record(actor, row_id)
        project_id = row_id if kind == 'project' else row['project_id']
        with self.platform._project_write_lock(project_id):
            row = self.project_record(actor, row_id) if kind == 'project' else self.session_record(actor, row_id)
            project = self.project_record(actor, project_id)
            if kind == 'session' and state(project) == 'deleted':
                raise PlatformError('restore_project_first', '所属项目在回收站，请先恢复项目。', 409)
            tasks = self.store.list('task', project_id=project_id) if kind == 'project' else self.platform.conversations.tasks(row)
            if any(is_active(task) or self.store.has_live_lease(task['id']) or (task.get('database_operation') or {}).get('status') in {'dropping','database_deleted','accounts_deleted'} for task in tasks):
                raise PlatformError('record_busy', '仍有执行中、等待审批、正在写入或数据库清理未完成的任务，请先处理后再操作。', 409)
            previous = state(row)
            if action == 'archive':
                if previous == 'deleted':
                    raise PlatformError('record_deleted', '此记录在回收站，请先恢复。', 409)
                target = 'archived'
            elif action == 'delete':
                target = 'deleted'
            else:
                target = row.get('deleted_from', 'active') if previous == 'deleted' else 'active'
                if target not in {'active', 'archived'}:target = 'active'
            if previous == target:
                return row
            if kind == 'project' and target == 'active':
                if any(other['id'] != row_id and other['root'] == row['root'] and state(other) == 'active' for other in self.store.list('project')):
                    raise PlatformError('project_exists', '此文件夹已有工作项目，请先处理现有项目记录。', 409)
            fields = {'lifecycle': target}
            if action == 'archive':fields['archived_at'] = timestamp()
            if action == 'delete':fields.update(deleted_at=timestamp(), deleted_from=previous)
            result = self.store.mutate(kind, row_id, lambda old:{**old, **fields})
            self.store.put('event', {'project_id':project_id, 'kind':'RecordLifecycleChanged',
                'detail':{'record_kind':kind, 'record_id':row_id, 'previous':previous, 'lifecycle':target, 'actor_id':actor.user_id}})
            return result
