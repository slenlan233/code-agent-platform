import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from code_agent_platform.api import create_app
from code_agent_platform.engine import DemoEngine
from code_agent_platform.models import LOCAL_ACTOR,Mode,PlanDraft,PlatformError,ProjectInput,TaskInput
from code_agent_platform.storage import Store
from code_agent_platform.models import digest
import test_platform as fixtures


class ConversationTests(unittest.TestCase):
    setUp=fixtures.PlatformTests.setUp

    def test_followup_receives_prior_user_input_and_public_result_and_survives_reload(self):
        seen=[]
        class Engine(DemoEngine):
            def answer(engine,actor,task,context):
                seen.append(context['conversation'])
                return super().answer(actor,task,context)
        self.platform.engine_override=Engine()
        session=self.platform.conversations.create(LOCAL_ACTOR,self.project['id'],'连续开发')
        first=self.platform.create_task(LOCAL_ACTOR,TaskInput(project_id=self.project['id'],session_id=session['id'],user_input='先解释加法代码',engine='demo'))
        self.platform.run(LOCAL_ACTOR,first['id'])
        second=self.platform.create_task(LOCAL_ACTOR,TaskInput(project_id=self.project['id'],session_id=session['id'],user_input='继续解释它的测试',engine='demo'))
        self.platform.run(LOCAL_ACTOR,second['id'])
        self.assertEqual(seen[1]['recent_messages'][0]['content'],'先解释加法代码')
        self.assertIn('加法',seen[1]['recent_messages'][1]['content'])
        reopened=Store(self.settings.data_dir)
        try:
            messages=reopened.list('message',project_id=self.project['id'])
            self.assertEqual(len([m for m in messages if m['kind']=='user']),2)
            self.assertEqual({m['session_id'] for m in messages},{session['id']})
        finally:reopened.close()

    def test_same_send_id_is_idempotent_and_different_content_is_rejected(self):
        session=self.platform.conversations.create(LOCAL_ACTOR,self.project['id'])
        data=TaskInput(project_id=self.project['id'],session_id=session['id'],user_input='同一条消息',engine='demo',client_request_id='send-12345678')
        first=self.platform.create_task(LOCAL_ACTOR,data)
        self.assertEqual(self.platform.create_task(LOCAL_ACTOR,data)['id'],first['id'])
        with self.assertRaises(PlatformError) as error:self.platform.create_task(LOCAL_ACTOR,data.model_copy(update={'user_input':'不同内容'}))
        self.assertEqual(error.exception.code,'message_conflict')
        self.assertEqual(len(self.platform.store.list('task',project_id=self.project['id'])),1)

    def test_approval_pending_blocks_another_turn_until_resolved(self):
        session=self.platform.conversations.create(LOCAL_ACTOR,self.project['id'])
        data=TaskInput(project_id=self.project['id'],session_id=session['id'],user_input='补测试',mode=Mode.WRITE,engine='demo')
        task=self.platform.create_task(LOCAL_ACTOR,data)
        self.platform.run(LOCAL_ACTOR,task['id'])
        with self.assertRaises(PlatformError) as error:self.platform.create_task(LOCAL_ACTOR,data)
        self.assertEqual(error.exception.code,'conversation_busy')
        self.platform._set(task['id'],status='cancelled')
        next_task=self.platform.create_task(LOCAL_ACTOR,data.model_copy(update={'mode':Mode.READONLY}))
        self.assertEqual(next_task['session_id'],session['id'])

    def test_legacy_history_is_marked_and_has_no_fabricated_model_turns(self):
        task=self.platform.create_task(LOCAL_ACTOR,TaskInput(project_id=self.project['id'],user_input='老输入',engine='demo'))
        task.pop('conversation_version')
        self.platform.store.put('task',task)
        for m in self.platform.store.list('message',task_id=task['id']):
            self.platform.store.put('message',{**m,'legacy':True})
        result=self.platform.conversations.detail(LOCAL_ACTOR,task['session_id'])
        self.assertTrue(all(m['legacy'] for m in result['messages']))
        self.assertFalse(any(m['kind']=='model' for m in result['messages']))
        again=self.platform.conversations.detail(LOCAL_ACTOR,task['session_id'])
        self.assertEqual([m['id'] for m in result['messages']],[m['id'] for m in again['messages']])

    def test_empty_project_can_plan_from_bound_user_requirement(self):
        folder=self.root/'empty';folder.mkdir()
        project=self.platform.register_project(LOCAL_ACTOR,ProjectInput(name='Empty',root=str(folder),allow_write=True))
        class Engine(DemoEngine):
            def plan(engine,actor,task,context):
                assert context['evidence'][0]['source_type']=='request'
                return PlanDraft(goal='从零建立项目',files=['README.md'],steps=['生成说明'],source_ids=[context['evidence'][0]['source_id']],test_plan='只生成计划',rollback_plan='未修改文件',database_change=False,compatibility_change=False)
        self.platform.engine_override=Engine()
        task=self.platform.create_task(LOCAL_ACTOR,TaskInput(project_id=project['id'],user_input='新建一个简单项目',mode=Mode.PLAN))
        result=self.platform.run(LOCAL_ACTOR,task['id'])
        self.assertEqual(result['status'],'completed')
        self.platform._validate_sources(result,result['evidence_ids'])

    def test_request_reference_cannot_read_a_different_conversation(self):
        first=self.platform.create_task(LOCAL_ACTOR,TaskInput(project_id=self.project['id'],user_input='其他会话的内容',engine='demo'))
        second=self.platform.create_task(LOCAL_ACTOR,TaskInput(project_id=self.project['id'],user_input='当前会话',engine='demo'))
        evidence=self.platform.store.put('evidence',{'id':'source_cross_session','source_type':'request','task_id':second['id'],'project_id':self.project['id'],'content_hash':digest(first['user_input'].encode()),'metadata':{'request_task_id':first['id']}})
        with self.assertRaises(PlatformError) as error:self.platform._validate_sources(second,[evidence['id']])
        self.assertEqual(error.exception.code,'source_denied')

    def test_resuming_failed_turn_cannot_overlap_new_active_turn(self):
        session=self.platform.conversations.create(LOCAL_ACTOR,self.project['id'])
        data=TaskInput(project_id=self.project['id'],session_id=session['id'],user_input='测试串行恢复',engine='demo')
        first=self.platform.create_task(LOCAL_ACTOR,data)
        self.platform._set(first['id'],status='failed',error={'code':'fixture','message':'Fixture'})
        self.platform.create_task(LOCAL_ACTOR,data)
        with self.assertRaises(PlatformError) as error:self.platform.run(LOCAL_ACTOR,first['id'],resume=True)
        self.assertEqual(error.exception.code,'conversation_busy')
        self.assertEqual(self.platform.store.get('task',first['id'])['status'],'failed')

    def test_model_public_reply_is_saved_without_private_reasoning(self):
        from langchain_core.messages import AIMessage
        from code_agent_platform.engine import UsageMiddleware
        from types import SimpleNamespace
        task=self.platform.create_task(LOCAL_ACTOR,TaskInput(project_id=self.project['id'],user_input='测试公开回复',engine='demo'))
        message=AIMessage(content='这是公开回复',additional_kwargs={'reasoning_content':'PRIVATE_REASONING_FIXTURE'},usage_metadata={'input_tokens':10,'output_tokens':5,'total_tokens':15})
        UsageMiddleware(self.platform.gateway,task['id'],'context_agent').wrap_model_call(None,lambda request:SimpleNamespace(result=[message]))
        messages=self.platform.store.list('message',task_id=task['id'])
        self.assertTrue(any(m['kind']=='model' and m['content']=='这是公开回复' for m in messages))
        self.assertNotIn('PRIVATE_REASONING_FIXTURE',json.dumps(messages))


class ConversationApiTests(unittest.TestCase):
    def setUp(self):
        fixtures.PlatformTests.setUp(self)
        self.settings.allow_local_project_selection=True
        self.app=create_app(platform=self.platform)
        self.client=self.enterContext(TestClient(self.app,client=('127.0.0.1',50000)))
        self.headers={'Authorization':'Bearer '+self.platform.store.local_token()}

    def test_session_endpoints_enforce_auth_owner_and_project(self):
        self.assertEqual(self.client.get('/api/sessions',params={'project_id':self.project['id']}).status_code,401)
        r=self.client.post('/api/sessions',json={'project_id':self.project['id'],'title':'我的对话'},headers=self.headers)
        self.assertEqual(r.status_code,201)
        sid=r.json()['id']
        users={'other-token':{'user_id':'other','roles':['developer'],'projects':[self.project['id']]}}
        with patch.dict(os.environ,{'AI_AGENT_USERS_JSON':json.dumps(users)}):
            other={'Authorization':'Bearer other-token'}
            self.assertEqual(self.client.get('/api/sessions/'+sid,headers=other).status_code,403)
            self.assertEqual(self.client.post('/api/sessions/'+sid+'/messages',json={'content':'窥视','mode':'readonly','engine':'demo','client_request_id':'send-12345678'},headers=other).status_code,403)
        renamed=self.client.patch('/api/sessions/'+sid,json={'title':'更新的名称'},headers=self.headers)
        self.assertEqual(renamed.json()['title'],'更新的名称')
        self.assertEqual(self.client.get('/api/sessions/'+sid+'?limit=0',headers=self.headers).status_code,422)

    def test_created_folder_grants_only_new_child_and_does_not_overwrite_existing(self):
        parent=self.root/'parent';parent.mkdir()
        response=self.client.post('/api/project-folders/create',json={'parent':str(parent),'name':'NewProject'},headers=self.headers)
        self.assertEqual(response.status_code,201,response.text)
        selected=response.json()
        self.assertEqual(Path(selected['root']).resolve(),(parent/'NewProject').resolve())
        again=self.client.post('/api/project-folders/create',json={'parent':str(parent),'name':'NewProject'},headers=self.headers)
        self.assertEqual(again.status_code,409)
        added=self.client.post('/api/projects',json={'name':'New','root':selected['root'],'selection_id':selected['selection_id']},headers=self.headers)
        self.assertEqual(added.status_code,201)
        self.assertEqual(added.json()['access_source'],'local_selection')
        self.assertEqual(self.settings.allowed_roots,[self.root])
        for name in ('../escape','CON','a/b','name.'):
            denied=self.client.post('/api/project-folders/create',json={'parent':str(parent),'name':name},headers=self.headers)
            self.assertEqual(denied.status_code,400)

    def test_remote_control_cannot_create_a_folder(self):
        remote=TestClient(self.app,client=('203.0.113.2',50000))
        response=remote.post('/api/project-folders/create',json={'parent':str(self.root),'name':'Denied'},headers=self.headers)
        self.assertEqual(response.status_code,403)
        self.assertFalse((self.root/'Denied').exists())
