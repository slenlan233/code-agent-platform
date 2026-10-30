import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import pymysql
from fastapi.testclient import TestClient
from code_agent_platform.api import create_app
from code_agent_platform.engine import DemoEngine
from code_agent_platform.intent import requests_database_deletion
from code_agent_platform.models import Actor, ApprovalDecision, FileChange, Mode, PatchDraft, PlanDraft, PlatformError, TaskInput, TurnDraft
from code_agent_platform.reset_verification import reset_paths
import test_platform as fixtures


class FakeMySQL:
    def __init__(self):
        self.schemas = {}
        self.users = {}
        self.calls = []
        self.fail_drop = None

    def connect(self, config, **options):
        database = self
        class Cursor:
            result = []
            def __enter__(self):return self
            def __exit__(self, *args):pass
            def execute(self, sql, params=()):
                database.calls.append((sql, params))
                self.result = []
                if sql.startswith('SELECT VERSION'): self.result = [('8.4.fixture',)]
                elif sql.startswith('SELECT SCHEMA_NAME'): self.result = [(params[0],)] if params[0] in database.schemas else []
                elif sql.startswith('SELECT TABLE_NAME'): self.result = [(name,) for name in database.schemas.get(params[0], [])]
                elif sql.startswith('CREATE DATABASE'): database.schemas.setdefault(sql.split('`')[1], [])
                elif sql.startswith('SELECT @@partial'): self.result = [(0,)]
                elif sql.startswith('CREATE USER'): database.users[params[0]] = params[2]
                elif sql.startswith('SELECT CURRENT_USER'): self.result = [(config['user'],)]
                elif sql.startswith('GRANT '): pass
                elif sql.startswith('DROP DATABASE'):
                    name = sql.split('`')[1]
                    if database.fail_drop == 'before':
                        database.fail_drop = None
                        raise pymysql.OperationalError('fixture disconnect before DDL')
                    database.schemas.pop(name, None)
                    if database.fail_drop == 'after':
                        database.fail_drop = None
                        raise pymysql.OperationalError('fixture disconnect after DDL')
                elif sql.startswith('DROP USER'): database.users.pop(params[0], None)
                else: raise AssertionError(sql)
            def fetchone(self):return self.result[0] if self.result else None
            def fetchall(self):return self.result
        class Connection:
            def __enter__(self):return self
            def __exit__(self, *args):pass
            def cursor(self):return Cursor()
        return Connection()


class DatabaseDeletionTests(unittest.TestCase):
    setUp = fixtures.PlatformTests.setUp

    def provision(self, *, unrelated='KEEP_ME=unchanged\r\n'):
        self.db = FakeMySQL()
        for module in ('code_agent_platform.business_mysql.connect', 'code_agent_platform.database_deletion.connect'):
            mock = patch(module, side_effect=self.db.connect)
            mock.start()
            self.addCleanup(mock.stop)
        self.platform.business_mysql.private.write('mysql-instance', {'host': '127.0.0.1', 'port': 3306,
            'user': 'manager', 'password': 'fixture-admin', 'revision': 'fixture', 'auto_provision': True, 'server_version': '8.4.fixture'})
        self.env = Path(self.project['root'])/'.env'
        if unrelated:self.env.write_bytes(unrelated.encode())
        self.platform.business_mysql.ensure(self.actor, self.project['id'])
        self.record = self.platform.business_mysql.record(self.project)
        self.schema = self.record['database']
        self.db.schemas[self.schema] = ['todo', 'flyway_schema_history']
        self.db.schemas['unrelated_business'] = ['keep_me']
        self.db.users['other_project_user'] = 'untouched'
        self.platform.engine_override = self.engine()

    def engine(self, combined=False):
        names = sorted(reset_paths(self.settings, self.project)) if combined else []
        class Delete(DemoEngine):
            def plan(engine, actor, task, context):
                return PlanDraft(goal='Delete the current project database', files=names,
                    steps=['Show the registered database and wait for independent confirmation'],
                    test_plan='Verify database absence and local connection cleanup', rollback_plan='Database data has no file rollback',
                    database_action='drop_project_database', database_change=True,
                    source_ids=[e['source_id'] for e in context['evidence']])
            def respond(engine, actor, task, context):return TurnDraft(action='modify', plan=engine.plan(actor, task, context))
            def patch(engine, actor, task, context):
                return PatchDraft(changes=[FileChange(path=name, content=None) for name in names], explanation='Clear approved code')
        return Delete()

    def start(self, *, mode=Mode.WRITE, content='Delete the project database', combined=False):
        self.platform.engine_override = self.engine(combined)
        task = self.platform.create_task(self.actor, TaskInput(project_id=self.project['id'], user_input=content, mode=mode, engine='demo'))
        return self.platform.run(self.actor, task['id'])

    def confirm(self, task, *, decision='approved'):
        name = task['database_operation']['target']['database'] if task['status'] == 'awaiting_database' else None
        revision = self.platform.store.get('approval', task['approval_id']).get('decision_revision', 0)
        self.platform.decide(self.actor, task['approval_id'], ApprovalDecision(decision=decision, database_name=name, decision_revision=revision))
        return self.platform.run(self.actor, task['id'], resume=True)

    def test_auto_conversation_requires_specific_database_confirmation_then_cleans_only_owned_resources(self):
        self.provision()
        before = (Path(self.project['root'])/'calculator.py').read_bytes()
        task = self.start(mode=Mode.AUTO)
        self.assertEqual(task['status'], 'awaiting_database')
        self.assertIn(self.schema, self.db.schemas)
        self.assertEqual(task['database_operation']['target']['table_count'], 2)
        self.assertFalse(any('DROP ' in sql for sql, _ in self.db.calls))
        with self.assertRaises(PlatformError):
            self.platform.decide(self.actor, task['approval_id'], ApprovalDecision(decision='approved'))
        result = self.confirm(task)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['database_result']['status'], 'deleted')
        self.assertNotIn(self.schema, self.db.schemas)
        self.assertIn('unrelated_business', self.db.schemas)
        self.assertEqual(set(self.db.users), {'other_project_user'})
        self.assertEqual(self.env.read_bytes(), b'KEEP_ME=unchanged\r\n')
        self.assertEqual(before, (Path(self.project['root'])/'calculator.py').read_bytes())
        self.assertIsNone(self.platform.business_mysql.private.read('database-'+self.project['id']))
        self.assertEqual(self.platform.business_mysql.public(self.project)['status'], 'deleted')
        self.assertEqual(self.platform.store.list('database_operation', task_id=task['id'])[0]['status'], 'completed')
        self.assertFalse(self.platform.store.list('sandbox_run', task_id=task['id']))
        self.assertFalse(self.platform.store.list('patch', task_id=task['id']))

    def test_rejection_cancellation_and_plan_only_never_drop_the_database(self):
        self.provision()
        task = self.start()
        result = self.confirm(task, decision='rejected')
        self.assertEqual(result['status'], 'cancelled')
        task = self.start()
        self.platform._set(task['id'], status='cancelled')
        self.platform.run(self.actor, task['id'], resume=True)
        planned = self.start(mode=Mode.PLAN)
        self.assertEqual(planned['status'], 'completed')
        self.assertIn(self.schema, self.db.schemas)
        self.assertFalse(any('DROP ' in sql for sql, _ in self.db.calls))

    def test_changed_table_list_or_env_or_expired_approval_cannot_be_approved(self):
        self.provision()
        task = self.start()
        self.db.schemas[self.schema].append('new_table')
        with self.assertRaises(PlatformError):self.confirm(task)
        self.db.schemas[self.schema].remove('new_table')
        original = self.env.read_bytes()
        self.env.write_bytes(original+b'NEW_SETTING=changed\n')
        with self.assertRaises(PlatformError):self.confirm(task)
        self.env.write_bytes(original)
        self.platform.store.mutate('approval', task['approval_id'], lambda row:{**row, 'expires_epoch': time.time()-1})
        with self.assertRaises(PlatformError):self.confirm(task)
        self.assertIn(self.schema, self.db.schemas)

    def test_external_database_or_tampered_target_cannot_be_deleted(self):
        self.provision()
        for changes in [{'database':'mysql'}, {'database':'unrelated_business'}, {'migration_user':'root'}, {'instance_revision':'other'}]:
            self.platform.store.put('project_database', {**self.record, **changes})
            with self.assertRaises(PlatformError):self.platform.database_deletion.preview(self.actor, self.project['id'])
        self.platform.store.put('project_database', {**self.record, 'new_allocation': True})
        with self.assertRaises(PlatformError):self.platform.database_deletion.preview(self.actor, self.project['id'])
        self.assertFalse(any('DROP ' in sql for sql, _ in self.db.calls))

    def test_no_approval_or_non_admin_or_deny_policy_cannot_execute(self):
        self.provision()
        task = self.start()
        with self.assertRaises(PlatformError):self.platform.database_deletion.execute(self.actor, task, task['database_operation_id'])
        actor = Actor(user_id='developer', projects=frozenset({self.project['id']}))
        with self.assertRaises(PlatformError):self.platform.database_deletion.preview(actor, self.project['id'])
        self.platform.store.mutate('project', self.project['id'], lambda p:{**p, 'policy_rules':[{'rule_id':'no-drop','effect':'deny','tools':['drop_database']}]})
        self.platform.decide(self.actor, task['approval_id'], ApprovalDecision(decision='approved', database_name=self.schema))
        with self.assertRaises(PlatformError):self.platform.run(self.actor, task['id'], resume=True)
        self.assertIn(self.schema, self.db.schemas)

    def test_database_only_operation_is_independent_of_file_patch_permissions(self):
        self.provision()
        self.platform.store.mutate('project', self.project['id'], lambda p:{**p, 'policy_rules':[
            {'rule_id':'no-files','effect':'deny','tools':['apply_patch']}]})
        task = self.start()
        self.assertEqual(task['status'], 'awaiting_database')
        self.assertEqual(self.confirm(task)['status'], 'completed')

    def test_disconnect_after_drop_can_resume_cleanup_without_repeating_drop(self):
        self.provision(unrelated='')
        task = self.start()
        self.db.fail_drop = 'after'
        with self.assertRaises(PlatformError):self.confirm(task)
        self.assertNotIn(self.schema, self.db.schemas)
        result = self.platform.run(self.actor, task['id'], resume=True)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(sum(sql.startswith('DROP DATABASE') for sql, _ in self.db.calls), 1)
        self.assertFalse(self.env.exists())
        self.assertEqual(self.platform.run(self.actor, task['id'], resume=True)['status'], 'completed')

    def test_expiry_after_actual_drop_allows_cleanup_but_no_second_drop(self):
        self.provision()
        task = self.start()
        self.db.fail_drop = 'after'
        with self.assertRaises(PlatformError):self.confirm(task)
        self.platform.store.mutate('approval', task['approval_id'], lambda row:{**row, 'expires_epoch': time.time()-1})
        result = self.platform.run(self.actor, task['id'], resume=True)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(sum(sql.startswith('DROP DATABASE') for sql, _ in self.db.calls), 1)

    def test_server_restart_recovers_only_already_started_deletion_cleanup(self):
        self.provision()
        task = self.start()
        self.db.fail_drop = 'after'
        with self.assertRaises(PlatformError):self.confirm(task)
        self.platform._set(task['id'], status='running')
        with TestClient(create_app(platform=self.platform), client=('127.0.0.1', 45002)):
            deadline = time.monotonic()+10
            while time.monotonic() < deadline:
                if self.platform.store.get('task', task['id'])['status'] == 'completed':break
                time.sleep(0.05)
        self.assertEqual(self.platform.store.get('task', task['id'])['status'], 'completed')
        self.assertEqual(sum(sql.startswith('DROP DATABASE') for sql, _ in self.db.calls), 1)

    def test_ambiguous_or_recreated_database_is_not_dropped_again(self):
        self.provision()
        task = self.start()
        self.db.fail_drop = 'before'
        with self.assertRaises(PlatformError):self.confirm(task)
        with self.assertRaises(PlatformError) as blocked:self.platform.run(self.actor, task['id'], resume=True)
        self.assertEqual(blocked.exception.code, 'database_drop_uncertain')
        self.assertEqual(sum(sql.startswith('DROP DATABASE') for sql, _ in self.db.calls), 1)
        self.assertIn(self.schema, self.db.schemas)

    def test_new_allocation_gets_new_credentials_and_old_completed_operation_is_idempotent(self):
        self.provision()
        old_credentials = self.platform.business_mysql.private.read('database-'+self.project['id'])
        task = self.start()
        result = self.confirm(task)
        self.platform.business_mysql.ensure(self.actor, self.project['id'])
        self.assertIn(self.schema, self.db.schemas)
        self.assertNotEqual(old_credentials, self.platform.business_mysql.private.read('database-'+self.project['id']))
        self.platform.database_deletion.execute(self.actor, result, task['database_operation_id'])
        self.assertIn(self.schema, self.db.schemas)
        self.assertEqual(sum(sql.startswith('DROP DATABASE') for sql, _ in self.db.calls), 1)

    def test_combined_code_reset_has_distinct_database_and_file_approvals(self):
        self.provision()
        task = self.start(content='Clear this project and delete the project database', combined=True)
        self.assertEqual(task['status'], 'awaiting_plan')
        task = self.confirm(task)
        self.assertEqual(task['status'], 'awaiting_database')
        self.assertIn(self.schema, self.db.schemas)
        task = self.confirm(task)
        self.assertEqual(task['status'], 'awaiting_diff')
        self.assertNotIn(self.schema, self.db.schemas)
        self.assertTrue((Path(self.project['root'])/'calculator.py').exists())
        self.assertEqual(task['verification']['verification_kind'], 'project_reset')
        task = self.confirm(task)
        self.assertEqual(task['status'], 'completed')
        self.platform.apply_patch(self.actor, task['id'])
        self.assertFalse((Path(self.project['root'])/'calculator.py').exists())

    def test_api_requires_local_control_and_target_name_and_exposes_no_secrets(self):
        self.provision()
        task = self.start()
        app = create_app(platform=self.platform)
        headers = {'Authorization':'Bearer '+self.platform.store.local_token()}
        remote = TestClient(app, client=('192.0.2.1', 45000))
        response = remote.post('/api/approvals/'+task['approval_id'], headers=headers,
            json={'decision':'approved','database_name':self.schema})
        self.assertEqual(response.status_code, 403)
        local = TestClient(app, client=('127.0.0.1', 45001))
        response = local.get('/api/tasks/'+task['id'], headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('fixture-admin', response.text)
        self.assertNotIn(self.platform.business_mysql.private.read('database-'+self.project['id'])['application_password'], response.text)
        response = local.post('/api/approvals/'+task['approval_id'], headers=headers, json={'decision':'approved'})
        self.assertEqual(response.status_code, 403)
        self.assertIn(self.schema, self.db.schemas)

    def test_deleting_table_data_or_question_is_not_database_deletion_intent(self):
        for text in ['Delete the project database', '\u628a\u7528\u7684\u90a3\u4e2a\u6570\u636e\u5e93\u4e5f\u5220\u4e86', '\u5220\u9664\u5f53\u524d\u9879\u76ee\u6570\u636e\u5e93\uff0c\u4fdd\u7559\u5176\u4ed6\u6570\u636e\u5e93']:
            self.assertTrue(requests_database_deletion(text))
        for text in ['Clear the database', 'Delete all tables in the database', '\u5220\u9664\u6570\u636e\u5e93\u4e2d\u7684\u6240\u6709\u6570\u636e', '\u4e0d\u8981\u5220\u9664\u6570\u636e\u5e93', '\u80fd\u4e0d\u80fd\u5220\u9664\u6570\u636e\u5e93']:
            self.assertFalse(requests_database_deletion(text), text)

    def test_model_cannot_invent_database_deletion_for_an_unrelated_request(self):
        self.provision()
        with self.assertRaises(PlatformError) as error:
            self.start(content='Explain the project structure')
        self.assertEqual(error.exception.code, 'database_delete_intent')
        self.assertFalse(any('DROP ' in sql for sql, _ in self.db.calls))

    def test_cleanup_preserves_multiline_unrelated_values(self):
        self.provision(unrelated='KEEP_MULTI="first\nsecond"\nKEEP_EQUALS=a=b\n')
        task = self.start()
        self.confirm(task)
        self.assertEqual(self.env.read_bytes(), b'KEEP_MULTI="first\nsecond"\nKEEP_EQUALS=a=b\n')

    def test_manual_env_edit_after_drop_is_not_overwritten_by_cleanup(self):
        self.provision()
        task = self.start()
        self.db.fail_drop = 'after'
        with self.assertRaises(PlatformError):self.confirm(task)
        original = self.env.read_bytes()
        self.env.write_bytes(original+b'HUMAN_EDIT=keep\n')
        with self.assertRaises(PlatformError) as error:self.platform.run(self.actor, task['id'], resume=True)
        self.assertEqual(error.exception.code, 'database_config_changed')
        self.assertEqual(self.env.read_bytes(), original+b'HUMAN_EDIT=keep\n')
        self.assertEqual(sum(sql.startswith('DROP DATABASE') for sql, _ in self.db.calls), 1)


if __name__ == '__main__':unittest.main()
