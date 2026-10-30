"""Expired observations and grants must never silently become new authority."""
import json
from pathlib import Path
import shutil
import subprocess
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from langchain_core.tools import tool

from code_agent_platform.api import create_app
from code_agent_platform.models import ApprovalDecision, Mode, PlatformError
import test_platform as fixtures
import test_database_deletion as database_fixtures


class SourceRefreshTests(unittest.TestCase):
    setUp = fixtures.PlatformTests.setUp
    task = fixtures.PlatformTests.task

    def database_source(self, *, legacy=False):
        self.settings.mcp_servers['mysql_readonly'].enabled = True
        project = self.platform.update_project(self.actor, self.project['id'], mcp_servers=['project_files', 'mysql_readonly'])
        task = self.task()
        retrieval = {'tool': 'mcp__mysql_readonly__mysql_execute_query',
                     'arguments': {'command': 'SELECT id, title FROM todo ORDER BY id LIMIT 20', 'database': 'fixture_db'}}
        item = {'path': 'mysql://mysql_readonly/fixture_db/query', 'text': '{"success": true, "data": [{"id": 1, "title": "keep"}]}'}
        source = self.platform.gateway.remote_evidence(task, project, 'mysql_readonly', item, 'database', retrieval=None if legacy else retrieval)
        self.platform.store.mutate('evidence', source['source_id'], lambda row: {**row, 'expires_epoch': time.time()-1})
        if legacy:
            self.platform.store.put('tool_call', {'task_id': task['id'], 'project_id': project['id'],
                'name': retrieval['tool'], 'input': retrieval['arguments'], 'output': {'evidence': [source]}, 'status': 'succeeded'})
        return task, project, retrieval, item, source

    def test_database_snapshot_is_requeried_and_only_identical_result_renews(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                task, project, retrieval, item, source = self.database_source(legacy=legacy)
                def query(*args):
                    fresh = self.platform.gateway.remote_evidence(task, project, 'mysql_readonly', item, 'database', retrieval=retrieval)
                    return {'evidence': [fresh]}
                with patch.object(self.platform.mcp, 'prepare'), patch.object(self.platform.gateway, 'invoke', side_effect=query) as invoke:
                    self.platform._validate_sources(task, [source['source_id']])
                invoke.assert_called_once_with(self.actor, task['id'], retrieval['tool'], retrieval['arguments'])
                self.assertGreater(self.platform.store.get('evidence', source['source_id'])['expires_epoch'], time.time())

    def test_changed_rows_and_query_failure_do_not_refresh_old_observation(self):
        task, project, retrieval, item, source = self.database_source()
        def changed(*args):
            fresh = self.platform.gateway.remote_evidence(task, project, 'mysql_readonly', {**item, 'text': '{"data": []}'}, 'database', retrieval=retrieval)
            return {'evidence': [fresh]}
        for callback in (changed, PlatformError('remote_query_failed', 'fixture unavailable')):
            with patch.object(self.platform.mcp, 'prepare'), patch.object(self.platform.gateway, 'invoke', side_effect=callback), self.assertRaises(PlatformError):
                self.platform._validate_sources(task, [source['source_id']])
            self.assertLess(self.platform.store.get('evidence', source['source_id'])['expires_epoch'], time.time())

    def test_write_sql_is_never_replayed_even_if_old_metadata_claims_query(self):
        task, _, retrieval, _, source = self.database_source()
        retrieval['arguments']['command'] = 'DELETE FROM todo'
        self.platform.store.mutate('evidence', source['source_id'], lambda row: {**row, 'retrieval': retrieval})
        with patch.object(self.platform.gateway, 'invoke') as invoke, self.assertRaises(PlatformError) as error:
            self.platform._validate_sources(task, [source['source_id']])
        self.assertEqual(error.exception.code, 'sql_readonly')
        invoke.assert_not_called()

    def test_mysql_gateway_records_replay_arguments_and_rejects_error_results(self):
        task, project, retrieval, _, _ = self.database_source()
        @tool
        def mysql_execute_query(command: str, database: str):
            """Execute one read-only query."""
            return {}
        self.platform.gateway.bind_mcp(task['id'], retrieval['tool'], mysql_execute_query, 'mysql_readonly',
            self.settings.mcp_servers['mysql_readonly'], self.platform.mcp)
        with patch.object(self.platform.mcp, 'call', return_value={'success': True, 'data': [{'id': 1}]}):
            result = self.platform.gateway.invoke(self.actor, task['id'], retrieval['tool'], retrieval['arguments'])
        saved = self.platform.store.get('evidence', result['evidence'][0]['source_id'])
        self.assertEqual(saved['retrieval'], retrieval)
        with patch.object(self.platform.mcp, 'call', return_value={'success': False, 'data': {'error': 'unavailable'}}), self.assertRaises(PlatformError) as error:
            self.platform.gateway.invoke(self.actor, task['id'], retrieval['tool'], retrieval['arguments'])
        self.assertEqual(error.exception.code, 'remote_query_failed')

    def test_phase_material_original_can_be_read_but_newly_denied_path_cannot(self):
        task = self.task()
        text = 'class Service {\n'+'// implementation\n'*1000+'public String important() { return "tail"; }\n}'
        saved = self.platform.gateway.archive_context_material(task, text, {'kind': 'generated_file', 'path': 'Service.java', 'source_ids': []})
        value = self.platform.gateway.invoke(self.actor, task['id'], 'read_tool_result', {'artifact_id': saved['id'], 'offset': len(text)-100, 'length': 100})
        self.assertEqual(value['content'], text[-100:])
        self.platform.update_project(self.actor, self.project['id'], policy_rules=[{
            'rule_id': 'deny-service', 'effect': 'deny', 'tools': ['read_file'], 'paths': ['Service.java']}])
        with self.assertRaises(PlatformError):
            self.platform.gateway.invoke(self.actor, task['id'], 'read_tool_result', {'artifact_id': saved['id']})


class ApprovalRefreshTests(unittest.TestCase):
    setUp = fixtures.PlatformTests.setUp
    task = fixtures.PlatformTests.task
    approve = fixtures.PlatformTests.approve

    def test_expired_pending_plan_and_diff_are_visible_and_renewal_never_approves(self):
        task = self.task(Mode.WRITE)
        self.platform.run(self.actor, task['id'])
        for kind in ('plan', 'diff'):
            current = self.platform.store.get('task', task['id'])
            approval_id = current['approval_id']
            before = self.platform.store.get('approval', approval_id)
            self.platform.store.mutate('approval', approval_id, lambda row: {**row, 'expires_epoch': time.time()-1})
            app = create_app(platform=self.platform)
            with TestClient(app) as client:
                headers = {'Authorization': 'Bearer '+self.platform.store.local_token()}
                public = client.get('/api/sessions/'+task['session_id'], headers=headers).json()['tasks'][0]
                self.assertTrue(public['approval']['expired'])
                self.assertEqual(public['approval']['kind'], kind)
                self.assertEqual(client.post('/api/approvals/'+approval_id+'/renew').status_code, 401)
                response = client.post('/api/approvals/'+approval_id+'/renew', headers=headers)
                self.assertEqual(response.status_code, 200, response.text)
                renewed = response.json()
                self.assertEqual(renewed['status'], 'pending')
                self.assertEqual(renewed['subject_hash'], before['subject_hash'])
                self.assertEqual(len(renewed['decision_history']), 1)
            self.assertFalse((Path(self.project['root'])/'tests/test_calculator.py').exists())
            with self.assertRaises(PlatformError) as stale:
                self.platform.decide(self.actor, approval_id, ApprovalDecision(decision='approved'))
            self.assertEqual(stale.exception.code, 'approval_revision_changed')
            self.platform.decide(self.actor, approval_id, ApprovalDecision(decision='approved', decision_revision=renewed['decision_revision']))
            decided = self.platform.store.get('approval', approval_id)
            self.assertAlmostEqual(decided['expires_epoch']-time.time(), self.settings.approval_seconds, delta=2)
            self.platform.run(self.actor, task['id'], resume=True)

    def test_renewal_rejects_changed_baseline_without_resetting_expiry(self):
        task = self.task(Mode.WRITE)
        current = self.platform.run(self.actor, task['id'])
        self.platform.store.mutate('approval', current['approval_id'], lambda row: {**row, 'expires_epoch': time.time()-1})
        (Path(self.project['root'])/'calculator.py').write_text('changed baseline', encoding='utf-8')
        with self.assertRaises(PlatformError):
            self.platform.renew_approval(self.actor, current['approval_id'])
        self.assertLess(self.platform.store.get('approval', current['approval_id'])['expires_epoch'], time.time())

    def test_expired_accepted_diff_reopens_confirmation_without_writing(self):
        task = self.task(Mode.WRITE)
        self.platform.run(self.actor, task['id'])
        self.approve(task['id'])
        ready = self.platform.store.get('task', task['id'])
        self.platform.decide(self.actor, ready['approval_id'], ApprovalDecision(decision='approved', apply_to_project=True))
        completed = self.platform.run(self.actor, task['id'], resume=True)
        self.platform.store.mutate('approval', completed['approval_id'], lambda row: {**row, 'expires_epoch': time.time()-1})
        renewed = self.platform.deliver_approved_patch(self.actor, task['id'])
        self.assertEqual(renewed['status'], 'awaiting_diff')
        self.assertEqual(renewed['patch_id'], completed['patch_id'])
        self.assertEqual(self.platform.store.get('approval', renewed['approval_id'])['status'], 'pending')
        self.assertEqual(renewed['delivery']['status'], 'awaiting_approval')
        self.assertFalse((Path(self.project['root'])/'tests/test_calculator.py').exists())

    @unittest.skipUnless(shutil.which('node'), 'Node required for UI interaction check')
    def test_expired_card_exposes_refresh_instead_of_execution_button(self):
        source = (Path(__file__).parents[1]/'src/code_agent_platform/web/turns.js').read_text(encoding='utf-8')
        script = """
const assert=require('node:assert/strict');
function node(tag,text,cls){return {tag,text,cls,children:[],append(...values){this.children.push(...values)},setAttribute(){}}}
function button(label,action,kind){return {...node('button',label,kind),action}}
let refreshed=null;function renewApproval(t){refreshed=t.id}
function openInspector(){};function decide(){throw Error('must not execute')}
"""+source+"""
for(const status of ['awaiting_plan','awaiting_diff','awaiting_database']){
 const task={id:'expired-turn',status,approval:{expired:true}};
 const box=humanApprovalCard(task);
 const buttons=box.children.flatMap(x=>x.children||[]).filter(x=>x.tag==='button');
 assert.equal(buttons[0].text,'刷新原审批内容');buttons[0].action();assert.equal(refreshed,task.id);
 assert.ok(!buttons.some(x=>x.text==='确认并开始'||x.text==='接受并写入项目'||x.text==='确认删除这个数据库'));
}
console.log('approval expiry cards verified');
"""
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)


class DatabaseApprovalRefreshTests(unittest.TestCase):
    setUp = fixtures.PlatformTests.setUp
    provision = database_fixtures.DatabaseDeletionTests.provision
    engine = database_fixtures.DatabaseDeletionTests.engine
    start = database_fixtures.DatabaseDeletionTests.start
    confirm = database_fixtures.DatabaseDeletionTests.confirm

    def test_expired_database_grant_reopens_target_before_any_drop(self):
        self.provision()
        pending = self.start()
        self.platform.decide(self.actor, pending['approval_id'], ApprovalDecision(decision='approved', database_name=self.schema))
        self.platform.store.mutate('approval', pending['approval_id'], lambda row: {**row, 'expires_epoch': time.time()-1})
        renewed = self.platform.run(self.actor, pending['id'], resume=True)
        self.assertEqual(renewed['status'], 'awaiting_database')
        self.assertIn(self.schema, self.db.schemas)
        self.assertFalse(any(sql.startswith('DROP ') for sql, _ in self.db.calls))
        self.assertEqual(self.confirm(renewed)['status'], 'completed')

    def test_plan_expiry_after_database_delete_never_repeats_destructive_operation(self):
        self.provision()
        platform = self.platform
        original = platform._delete_database
        def delete_then_expire(state):
            result = original(state)
            platform.store.mutate('approval', state['plan_approval'], lambda row: {**row, 'expires_epoch': time.time()-1})
            return result
        pending = self.start(content='Clear this project and delete the project database', combined=True)
        pending = self.confirm(pending)
        self.assertEqual(pending['status'], 'awaiting_database')
        with patch.object(platform, '_delete_database', side_effect=delete_then_expire):
            renewed = self.confirm(pending)
        self.assertEqual(renewed['status'], 'awaiting_plan')
        self.assertNotIn(self.schema, self.db.schemas)
        drops = len([sql for sql, _ in self.db.calls if sql.startswith('DROP DATABASE')])
        ready = self.confirm(renewed)
        self.assertEqual(ready['status'], 'awaiting_diff', str({key: ready.get(key) for key in ('error', 'verification', 'review')}))
        self.assertEqual(len([sql for sql, _ in self.db.calls if sql.startswith('DROP DATABASE')]), drops)


if __name__ == '__main__':
    unittest.main()
