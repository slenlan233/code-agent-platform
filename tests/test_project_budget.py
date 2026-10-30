import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from pydantic import ValidationError

from code_agent_platform.api import create_app
from code_agent_platform.models import Actor,Budget,Mode,PlatformError,ProjectInput,TaskInput
import test_platform as fixtures


class ProjectBudgetTests(unittest.TestCase):
    setUp=fixtures.PlatformTests.setUp
    task=fixtures.PlatformTests.task

    def usage(self,input_tokens,output_tokens,cache=0):
        return {'input_tokens':input_tokens,'output_tokens':output_tokens,'cache_read_tokens':cache,'uncached_input_tokens':input_tokens-cache,'model_calls':1,'tool_calls':0,'cost':None}

    def test_selected_allowance_is_preserved_and_can_exceed_fifty_million(self):
        self.assertEqual(ProjectInput(name='new',root=str(self.root)).token_budget,2_000_000)
        self.assertEqual(self.task()['budget']['max_tokens'],2_000_000)
        self.platform.update_project(self.actor,self.project['id'],token_budget=100_000_000)
        task=self.task()
        self.assertEqual(task['budget']['max_tokens'],100_000_000)
        self.assertEqual(task['token_budget_source'],'project')
        for model,data in ((Budget,{'max_tokens':99}),(ProjectInput,{'name':'invalid','root':str(self.root),'token_budget':99})):
            with self.assertRaises(ValidationError):model.model_validate(data)

    def test_unlimited_projects_still_count_usage_and_keep_explicit_turn_limits(self):
        self.platform.update_project(self.actor,self.project['id'],token_budget=None)
        task=self.task();self.platform._set(task['id'],usage=self.usage(70_000_000,30_000_000))
        data=self.platform.token_budget(self.actor,self.project['id'])
        self.assertTrue(data['unlimited']);self.assertFalse(data['exhausted'])
        self.assertIsNone(data['remaining_tokens']);self.assertEqual(data['total_tokens'],100_000_000)
        self.platform.gateway.check_budget(task['id'],model=True)
        bounded=self.task(budget=Budget(max_tokens=100))
        self.platform._set(bounded['id'],usage=self.usage(100,1))
        with self.assertRaises(PlatformError) as error:self.platform.gateway.check_budget(bounded['id'])
        self.assertEqual(error.exception.code,'token_budget')

    def test_increased_default_call_limits_do_not_remove_call_gates(self):
        budget=Budget()
        self.assertEqual((budget.max_model_calls,budget.max_tool_calls,budget.max_graph_steps,budget.max_seconds),(60,200,200,3600))
        task=self.task(budget=Budget(max_model_calls=1,max_tool_calls=1))
        self.platform.gateway.check_budget(task['id'],model=True,tool=True)
        with self.assertRaises(PlatformError) as error:self.platform.gateway.check_budget(task['id'],model=True)
        self.assertEqual(error.exception.code,'model_budget')

    def test_project_usage_combines_chats_and_cache_is_not_added_twice(self):
        self.platform.update_project(self.actor,self.project['id'],token_budget=100)
        first,second=self.task(),self.task()
        self.platform._set(first['id'],usage=self.usage(60,10,40))
        self.platform._set(second['id'],usage=self.usage(20,10,10))
        result=self.platform.token_budget(self.actor,self.project['id'])
        self.assertEqual(result['total_tokens'],100)
        self.assertEqual(result['cache_read_tokens'],50)
        self.assertEqual(result['remaining_tokens'],0)
        self.assertEqual(result['task_count'],2)
        with self.assertRaises(PlatformError) as exhausted:self.platform.gateway.check_budget(second['id'],model=True)
        self.assertEqual(exhausted.exception.code,'project_token_budget')

    def test_explicit_smaller_turn_limit_is_respected(self):
        task=self.task(budget=Budget(max_tokens=100))
        self.platform._set(task['id'],usage=self.usage(80,25))
        self.assertEqual(task['token_budget_source'],'task')
        with self.assertRaises(PlatformError) as exhausted:self.platform.gateway.check_budget(task['id'])
        self.assertEqual(exhausted.exception.code,'token_budget')

    def test_increasing_budget_preserves_plan_scope_approval_and_all_usage(self):
        task=self.task(Mode.WRITE)
        pending=self.platform.run(self.actor,task['id'])
        plan=self.platform.store.get('plan',pending['plan_id'])
        original_version=self.platform.project(self.actor,self.project['id'])['version']
        original_usage=pending['usage']
        self.platform.update_project(self.actor,self.project['id'],token_budget=50_000_000)
        current=self.platform.store.get('task',task['id'])
        self.assertEqual(current['usage'],original_usage)
        self.assertEqual(current['approval_id'],pending['approval_id'])
        self.assertEqual(current['budget']['max_tokens'],50_000_000)
        self.assertEqual(self.platform.project(self.actor,self.project['id'])['version'],original_version)
        self.platform._check_baseline(current,plan)

    def test_legacy_stopped_turn_can_follow_project_without_resetting_counters(self):
        task=self.task(budget=Budget(max_tokens=100_000))
        original=self.usage(70_215,40_507,37_376)
        self.platform._set(task['id'],status='failed',usage=original,error={'code':'token_budget','message':'fixture'})
        self.platform.update_project(self.actor,self.project['id'],token_budget=50_000_000)
        result=self.platform.update_task_token_budget(self.actor,task['id'],follow_project=True)
        self.assertEqual(result['usage'],original)
        self.assertEqual(result['budget']['max_tokens'],50_000_000)
        self.platform.gateway.check_budget(task['id'])
        self.assertEqual(self.platform.token_budget(self.actor,self.project['id'])['total_tokens'],110_722)

    def test_project_allowance_always_applies_even_with_larger_explicit_task_budget(self):
        self.platform.update_project(self.actor,self.project['id'],token_budget=100)
        task=self.task(budget=Budget(max_tokens=50_000_000))
        self.platform._set(task['id'],usage=self.usage(80,25))
        with self.assertRaises(PlatformError) as exhausted:self.platform.gateway.check_budget(task['id'])
        self.assertEqual(exhausted.exception.code,'project_token_budget')

    def test_budget_api_enforces_valid_values_auth_and_keeps_project_version(self):
        with TestClient(create_app(platform=self.platform)) as client:
            path='/api/projects/'+self.project['id']
            headers={'Authorization':'Bearer '+self.platform.store.local_token()}
            self.assertEqual(client.patch(path,json={'token_budget':50_000_000}).status_code,401)
            self.assertEqual(client.patch(path,json={'token_budget':99},headers=headers).status_code,422)
            response=client.patch(path,json={'token_budget':50_000_000},headers=headers)
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()['token_budget'],50_000_000)
            self.assertEqual(response.json()['version'],self.project['version'])
            self.assertEqual(client.get(path+'/token-budget',headers=headers).json()['limit'],50_000_000)
            task=self.task()
            response=client.patch('/api/tasks/'+task['id']+'/token-budget',json={'follow_project':True},headers=headers)
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()['token_budget_source'],'project')
        other=Actor(user_id='developer',projects=frozenset({self.project['id']}))
        with self.assertRaises(PlatformError):self.platform.update_project(other,self.project['id'],token_budget=50_000_000)

    def test_null_budget_is_explicit_unlimited_and_omitted_budget_keeps_value(self):
        headers={'Authorization':'Bearer '+self.platform.store.local_token()}
        with TestClient(create_app(platform=self.platform)) as client:
            path='/api/projects/'+self.project['id']
            self.assertEqual(client.patch(path,json={'token_budget':1000},headers=headers).status_code,200)
            self.assertEqual(client.patch(path,json={'enabled':True},headers=headers).json()['token_budget'],1000)
            version=self.platform.project(self.actor,self.project['id'])['version']
            response=client.patch(path,json={'token_budget':None},headers=headers)
            self.assertEqual(response.status_code,200)
            self.assertIsNone(response.json()['token_budget'])
            self.assertEqual(response.json()['version'],version)
            self.assertTrue(client.get(path+'/token-budget',headers=headers).json()['unlimited'])
            task=self.task()
            response=client.patch('/api/tasks/'+task['id']+'/token-budget',json={'follow_project':True},headers=headers)
            self.assertEqual(response.status_code,200)
            self.assertIsNone(response.json()['budget']['max_tokens'])
            self.assertEqual(client.patch('/api/tasks/'+task['id']+'/token-budget',json={},headers=headers).status_code,400)


if __name__=='__main__':unittest.main()
