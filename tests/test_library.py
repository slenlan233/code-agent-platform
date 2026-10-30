import json
import unittest
from pathlib import Path
from fastapi.testclient import TestClient

from code_agent_platform.api import create_app
from code_agent_platform.models import Actor, LOCAL_ACTOR, Mode, PlatformError, ProjectInput, TaskInput
from code_agent_platform.budgeting import project_token_usage
from code_agent_platform.storage import Store
import test_platform as fixtures


class LibraryTests(unittest.TestCase):
    setUp = fixtures.PlatformTests.setUp

    def conversation(self, title='History'):
        session = self.platform.conversations.create(self.actor, self.project['id'], title)
        task = self.platform.create_task(self.actor, TaskInput(project_id=self.project['id'], session_id=session['id'],
            user_input='Explain the calculator', engine='demo'))
        self.platform.run(self.actor, task['id'])
        return session, self.platform.store.get('task', task['id'])

    def test_archive_and_restore_project_preserves_files_database_records_and_history(self):
        session, task = self.conversation()
        root = Path(self.project['root']);before = (root/'calculator.py').read_bytes()
        (root/'.env').write_text('KEEP_DATABASE_CONNECTION=fixture', encoding='utf-8')
        self.platform.store.put('project_database', {'id':'business_'+self.project['id'], 'project_id':self.project['id'], 'status':'ready', 'database':'fixture_database'})
        result = self.platform.library.change(self.actor, 'project', self.project['id'], 'archive')
        self.assertEqual(result['lifecycle'], 'archived')
        self.assertFalse(any(p['id'] == self.project['id'] for p in self.platform.projects(self.actor)))
        archive = self.platform.library.list(self.actor)
        self.assertEqual(archive['projects'][0]['session_count'], 1)
        self.assertTrue(self.platform.library.session_detail(self.actor, session['id'])['messages'])
        self.assertEqual((root/'calculator.py').read_bytes(), before)
        self.assertEqual((root/'.env').read_text(), 'KEEP_DATABASE_CONNECTION=fixture')
        self.assertEqual(self.platform.store.list('project_database')[0]['status'], 'ready')
        self.platform.library.change(self.actor, 'project', self.project['id'], 'restore')
        self.assertIn(session['id'], [s['id'] for s in self.platform.conversations.list(self.actor, self.project['id'])])

    def test_archived_conversation_is_readonly_until_restored(self):
        session, task = self.conversation()
        self.platform.library.change(self.actor, 'session', session['id'], 'archive')
        self.assertEqual(self.platform.conversations.list(self.actor, self.project['id']), [])
        with self.assertRaises(PlatformError):self.platform.create_task(self.actor, TaskInput(project_id=self.project['id'], session_id=session['id'], user_input='Continue', engine='demo'))
        with self.assertRaises(PlatformError):self.platform.gateway.invoke(self.actor, task['id'], 'read_file', {'path':'calculator.py'})
        with self.assertRaises(PlatformError):self.platform.apply_patch(self.actor, task['id'])
        self.assertTrue(self.platform.library.session_detail(self.actor, session['id'])['read_only'])
        self.platform.library.change(self.actor, 'session', session['id'], 'restore')
        next_task = self.platform.create_task(self.actor, TaskInput(project_id=self.project['id'], session_id=session['id'], user_input='Continue', engine='demo'))
        self.assertEqual(next_task['session_id'], session['id'])

    def test_delete_conversation_is_recoverable_and_does_not_reset_project_usage(self):
        session, task = self.conversation()
        self.platform.store.mutate('task', task['id'], lambda row:{**row, 'usage':{**row['usage'], 'input_tokens':100, 'output_tokens':25}})
        before = project_token_usage(self.platform.store, self.project)['total_tokens']
        self.platform.library.change(self.actor, 'session', session['id'], 'delete')
        self.assertEqual(len(self.platform.library.list(self.actor, 'trash')['sessions']), 1)
        self.assertEqual(project_token_usage(self.platform.store, self.project)['total_tokens'], before)
        self.assertTrue(self.platform.store.list('message', task_id=task['id']))
        self.platform.library.change(self.actor, 'session', session['id'], 'restore')
        self.assertEqual(self.platform.conversations.list(self.actor, self.project['id'])[0]['id'], session['id'])

    def test_deleted_project_hides_children_and_restore_preserves_individual_archives(self):
        first, _ = self.conversation('Active child');second, _ = self.conversation('Archived child')
        self.platform.library.change(self.actor, 'session', second['id'], 'archive')
        self.platform.library.change(self.actor, 'project', self.project['id'], 'delete')
        self.assertEqual(len(self.platform.library.list(self.actor, 'trash')['projects']), 1)
        self.assertEqual(self.platform.library.list(self.actor)['sessions'], [])
        with self.assertRaises(PlatformError):self.platform.library.change(self.actor, 'session', second['id'], 'restore')
        self.platform.library.change(self.actor, 'project', self.project['id'], 'restore')
        self.assertEqual([s['id'] for s in self.platform.conversations.list(self.actor, self.project['id'])], [first['id']])
        self.assertEqual(self.platform.library.list(self.actor)['sessions'][0]['id'], second['id'])

    def test_deleting_an_archive_returns_to_archive_when_restored(self):
        session, _ = self.conversation()
        for kind, row_id in [('session',session['id']),('project',self.project['id'])]:
            self.platform.library.change(self.actor, kind, row_id, 'archive')
            self.platform.library.change(self.actor, kind, row_id, 'delete')
            again = self.platform.library.change(self.actor, kind, row_id, 'delete')
            self.assertEqual(again['deleted_from'], 'archived')
            restored = self.platform.library.change(self.actor, kind, row_id, 'restore')
            self.assertEqual(restored['lifecycle'], 'archived')

    def test_active_tasks_pending_approvals_and_delivery_block_lifecycle_changes(self):
        for status in ['queued','running','awaiting_plan','awaiting_diff','awaiting_database','waiting_environment']:
            session, task = self.conversation(status)
            self.platform.store.mutate('task', task['id'], lambda row:{**row, 'status':status})
            for kind, row_id in [('session',session['id']),('project',self.project['id'])]:
                with self.assertRaises(PlatformError) as blocked:self.platform.library.change(self.actor, kind, row_id, 'archive')
                self.assertEqual(blocked.exception.code, 'record_busy')
            self.platform.store.mutate('task', task['id'], lambda row:{**row, 'status':'completed'})
        session, task = self.conversation('Delivery')
        self.platform.store.mutate('task', task['id'], lambda row:{**row, 'delivery':{'status':'writing'}})
        with self.assertRaises(PlatformError):self.platform.library.change(self.actor, 'project', self.project['id'], 'delete')

    def test_live_manual_file_write_lease_cannot_be_archived(self):
        session, task = self.conversation()
        lease = self.platform.store.acquire(task['id'])
        try:
            with self.assertRaises(PlatformError) as blocked:self.platform.library.change(self.actor, 'session', session['id'], 'delete')
            self.assertEqual(blocked.exception.code, 'record_busy')
        finally:self.platform.store.release(task['id'], lease)

    def test_failed_database_cleanup_cannot_be_hidden_by_archiving(self):
        session, task = self.conversation()
        self.platform.store.mutate('task', task['id'], lambda row:{**row, 'status':'failed','database_operation':{'status':'accounts_deleted'}})
        with self.assertRaises(PlatformError) as blocked:self.platform.library.change(self.actor, 'project', self.project['id'], 'archive')
        self.assertEqual(blocked.exception.code, 'record_busy')

    def test_ownership_project_scope_and_disabled_permissions_are_enforced(self):
        session, _ = self.conversation()
        stranger = Actor(user_id='stranger', projects=frozenset({self.project['id']}))
        outsider = Actor(user_id='outsider', projects=frozenset({'another'}))
        for actor in [stranger, outsider]:
            with self.assertRaises(PlatformError):self.platform.library.session_detail(actor, session['id'])
            with self.assertRaises(PlatformError):self.platform.library.change(actor, 'session', session['id'], 'delete')
            self.assertEqual(self.platform.library.list(actor)['sessions'], [])
        self.platform.library.change(self.actor, 'project', self.project['id'], 'archive')
        self.assertEqual(self.platform.library.list(outsider)['projects'], [])
        with self.assertRaises(PlatformError):self.platform.library.change(stranger, 'project', self.project['id'], 'restore')

    def test_archived_history_remains_readable_when_folder_was_removed(self):
        session, _ = self.conversation()
        self.platform.library.change(self.actor, 'project', self.project['id'], 'archive')
        renamed = Path(self.project['root']).with_name('moved-demo')
        Path(self.project['root']).rename(renamed)
        self.assertTrue(self.platform.library.session_detail(self.actor, session['id'])['messages'])
        self.assertEqual(len(self.platform.library.project_detail(self.actor, self.project['id'])['sessions']), 1)

    def test_deleted_project_can_be_added_again_and_restore_rejects_duplicate_active_folder(self):
        self.platform.library.change(self.actor, 'project', self.project['id'], 'delete')
        other = self.platform.register_project(self.actor, ProjectInput(name='New record', root=self.project['root']))
        self.assertNotEqual(other['id'], self.project['id'])
        with self.assertRaises(PlatformError):self.platform.library.change(self.actor, 'project', self.project['id'], 'restore')

    def test_lifecycle_is_persistent_and_api_does_not_offer_permanent_purge(self):
        session, _ = self.conversation()
        client = TestClient(create_app(platform=self.platform))
        headers = {'Authorization':'Bearer '+self.platform.store.local_token()}
        response = client.post('/api/library/session/'+session['id']+'/archive', headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.get('/api/library', headers=headers).json()['sessions'][0]['id'], session['id'])
        self.assertEqual(client.get('/api/library/sessions/'+session['id'], headers=headers).status_code, 200)
        self.assertEqual(client.post('/api/library/session/'+session['id']+'/purge', headers=headers).status_code, 422)
        self.assertEqual(client.get('/api/library').status_code, 401)
        reopened = Store(self.settings.data_dir)
        try:self.assertEqual(reopened.get('session', session['id'])['lifecycle'], 'archived')
        finally:reopened.close()


if __name__ == '__main__':unittest.main()
