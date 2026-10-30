"""Approval-bound deletion of a single platform-owned project database."""
import base64
import io
import time

from dotenv.parser import parse_stream

from .business_mysql import ENV_KEYS, connect
from .models import LOCAL_ACTOR, PlatformError, digest, timestamp


class DatabaseDeletion:
    def __init__(self, platform):
        self.platform = platform
        self.store = platform.store
        self.mysql = platform.business_mysql

    @staticmethod
    def require_actor(actor):
        actor.require('admin')
        if actor.user_id != LOCAL_ACTOR.user_id:
            raise PlatformError('local_control_required', '删除项目数据库需要本机管理员确认。', 403)

    def owned(self, actor, project_id):
        self.require_actor(actor)
        project = self.platform.project(actor, project_id, write=True)
        record = self.mysql.record(project)
        config = self.mysql.private.read('mysql-instance')
        suffix = project_id.removeprefix('project_')
        import re
        expected = {'database': 'agent_app_'+suffix, 'application_user': 'agp_'+suffix[:20]+'_app',
                    'migration_user': 'agp_'+suffix[:20]+'_mig'}
        if not re.fullmatch('[0-9a-f]{32}', suffix) or not record or record.get('project_id') != project_id or record.get('new_allocation', True) or any(record.get(k) != v for k, v in expected.items()):
            raise PlatformError('database_not_owned', '仅能删除平台为当前项目创建的专属数据库，不能删除外部配置库或其他数据库。', 403)
        if not config or any(record.get(k) != config.get(k) for k in ('host', 'port')) or record.get('instance_revision') != config.get('revision'):
            raise PlatformError('database_instance_changed', '数据库实例配置已变化，请重新核对目标。', 409)
        return project, record, config

    @staticmethod
    def tables(config, schema):
        with connect(config) as connection:
            with connection.cursor() as cursor:
                cursor.execute('SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME=%s', (schema,))
                exists = cursor.fetchone() is not None
                cursor.execute('SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA=%s ORDER BY TABLE_NAME', (schema,))
                names = [row[0] for row in cursor.fetchall()]
        return exists, names

    @staticmethod
    def clean_env(raw):
        output = []
        for binding in parse_stream(io.StringIO(raw.decode('utf-8'))):
            if binding.key in ENV_KEYS or binding.original.string.strip() == '# Platform-managed project MySQL connection':
                continue
            output.append(binding.original.string)
        return ''.join(output).encode('utf-8')

    def preview(self, actor, project_id):
        project, record, config = self.owned(actor, project_id)
        if record['status'] != 'ready' or not self.mysql.public(project)['ready']:
            raise PlatformError('database_not_ready', '当前数据库连接未就绪或已变更，不能生成删除确认。', 409)
        credentials = self.mysql.private.read('database-'+project_id)
        if not credentials:
            raise PlatformError('database_secret_missing', '项目凭证缺失，无法确认数据库归属。', 409)
        raw = self.mysql.env(project).read_bytes()
        exists, tables = self.tables(config, record['database'])
        if not exists:
            raise PlatformError('database_missing', '记录中的数据库已经不存在，请先核对项目连接。', 409)
        target = {key: record[key] for key in ('host', 'port', 'database', 'application_user', 'migration_user')}
        target.update(tables=tables, table_count=len(tables), scope='当前项目专属数据库及全部表、数据、两个专属账号和本地连接配置',
                      project_id=project_id, project_name=project['name'], env_file='.env', reversible=False)
        binding = {'project_version': project['version'], 'root': project['root'], 'root_identity': project.get('root_identity'),
                   'allocation': record.get('allocation_id', record['created_at']), 'instance_revision': config['revision'],
                   'credentials_hash': digest(credentials), 'env_hash': digest(raw), 'cleaned_env_hash': digest(self.clean_env(raw))}
        return {'target': target, 'binding': binding, 'hash': digest([target, binding])}

    def prepare(self, actor, task):
        preview = self.preview(actor, task['project_id'])
        operation_id = 'db_delete_'+digest([task['id'], preview['hash']])[:32]
        try:
            return self.store.get('database_operation', operation_id)
        except PlatformError as error:
            if error.code != 'not_found':
                raise
        return self.store.put('database_operation', {'id': operation_id, 'task_id': task['id'],
            'project_id': task['project_id'], 'status': 'prepared', **preview})

    def validate_pending(self, actor, operation):
        if self.preview(actor, operation['project_id'])['hash'] != operation['hash']:
            raise PlatformError('database_target_changed', '库、表清单、账号或本地连接已变化，需要重新发起删除确认。', 409)

    @staticmethod
    def public(operation):
        return {key: operation.get(key) for key in ('id', 'status', 'target', 'result', 'error')}

    def execute(self, actor, task, operation_id):
        operation = self.store.get('database_operation', operation_id)
        if operation['task_id'] != task['id'] or operation['project_id'] != task['project_id']:
            raise PlatformError('database_operation_scope', '删除操作不属于当前任务。', 403)
        project, record, config = self.owned(actor, task['project_id'])
        if operation['status'] == 'completed':
            return operation['result']
        approval_id = 'approval_'+digest([task['id'], 'database', operation['hash']])[:32]
        approval = self.store.get('approval', approval_id)
        if approval['status'] != 'approved' or approval['subject_id'] != operation_id or approval['expires_epoch'] < time.time() and operation['status'] == 'prepared' or approval.get('database_name') != operation['target']['database']:
            raise PlatformError('database_approval_required', '删除数据库需要有效的目标库确认。', 403)
        allocation = record.get('allocation_id', record['created_at'])
        if allocation != operation['binding']['allocation']:
            raise PlatformError('database_allocation_changed', '项目数据库已重新创建，旧操作不能再次删除。', 409)
        lease = self.store.acquire(project['id'], kind='project')
        secret_name = 'dbdelete-'+operation_id.removeprefix('db_delete_')
        try:
            operation = self.store.get('database_operation', operation_id)
            if operation['status'] == 'prepared':
                self.validate_pending(actor, operation)
                raw = self.mysql.env(project).read_bytes()
                self.mysql.private.write(secret_name, {'before': base64.b64encode(raw).decode(),
                    'after': base64.b64encode(self.clean_env(raw)).decode()})
                operation = self.store.put('database_operation', {**operation, 'status': 'dropping'})
                self.store.put('project_database', {**record, 'status': 'deleting', 'deletion_operation_id': operation_id})
                self.platform.gateway.check_budget(task['id'])
                with connect(config) as connection:
                    with connection.cursor() as cursor:
                        cursor.execute('DROP DATABASE `'+record['database']+'`')
                operation = self.store.put('database_operation', {**operation, 'status': 'database_deleted'})
            exists, _ = self.tables(config, record['database'])
            if exists:
                # Never repeat an ambiguous DDL or delete a subsequently recreated database.
                raise PlatformError('database_drop_uncertain', '数据库仍存在或已被重新创建；已停止，不会重复删库。请核对后重新发起操作。', 409)
            if operation['status'] == 'dropping':
                operation = self.store.put('database_operation', {**operation, 'status': 'database_deleted'})
            if operation['status'] == 'database_deleted':
                with connect(config) as connection:
                    with connection.cursor() as cursor:
                        for role in ('application', 'migration'):
                            cursor.execute('DROP USER IF EXISTS %s@%s', (record[role+'_user'], 'localhost'))
                operation = self.store.put('database_operation', {**operation, 'status': 'accounts_deleted'})
            self.platform.project(actor, project['id'], write=True)
            saved = self.mysql.private.read(secret_name)
            if not saved:
                raise PlatformError('database_cleanup_missing', '连接配置清理记录缺失，请核对本地配置；不会再次删库。', 409)
            before, after = (base64.b64decode(saved[key]) for key in ('before', 'after'))
            path = self.mysql.env(project)
            current = path.read_bytes() if path.exists() else b''
            expected_after = after if after.strip() else b''
            if current == before:
                if after.strip():
                    self.mysql.write_env(path, after)
                else:
                    path.unlink(missing_ok=True)
            elif current != expected_after:
                raise PlatformError('database_config_changed', '数据库已删除，但本地配置期间有人工修改；未覆盖修改，请核对配置后重试清理。', 409)
            self.mysql.private.delete('database-'+project['id'])
            self.store.put('project_database', {**record, 'status': 'deleted', 'deleted_at': timestamp(),
                'deletion_operation_id': operation_id, 'env_hash': None, 'error': None})
            result = {'status': 'deleted', 'operation_id': operation_id, 'host': record['host'], 'port': record['port'],
                      'database': record['database'], 'accounts_removed': True, 'connection_removed': True,
                      'deleted_table_count': operation['target']['table_count']}
            self.store.put('database_operation', {**operation, 'status': 'completed', 'result': result, 'error': None})
            self.mysql.private.delete(secret_name)
            self.store.event(task['id'], 'DatabaseDeletionCompleted', result)
            return result
        except Exception as error:
            current = self.store.get('database_operation', operation_id)
            self.store.put('database_operation', {**current, 'error': {'code': getattr(error, 'code', 'database_delete_failed'),
                'message': getattr(error, 'message', '数据库删除或配置清理未完成，请核对状态后重试。')}})
            if isinstance(error, PlatformError):
                raise
            raise PlatformError('database_delete_failed', '数据库删除或配置清理未完成，请核对状态后重试。', 409) from None
        finally:
            self.store.release(project['id'], lease, kind='project')
