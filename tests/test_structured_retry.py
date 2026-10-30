import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
from code_agent_platform.engine import ModelEngine
from code_agent_platform.models import PlatformError,ReviewDraft
from code_agent_platform.engine import scoped_patch_type
from code_agent_platform.models import PatchDraft,FileChange,PlanDraft,AnswerDraft,Finding
from langchain_core.messages import AIMessage


class StructuredRetryTests(unittest.TestCase):
    def run_engine(self,responses):
        gateway=Mock()
        gateway.store.get.return_value={'rules':[]}
        settings=SimpleNamespace(model=Mock(return_value=object()))
        agent=Mock()
        agent.invoke.side_effect=responses
        task={'id':'fixture','project_id':'fixture-project','user_input':'Review','budget':{'max_model_calls':4}}
        return ModelEngine(settings,gateway),agent,task

    def test_resumed_phase_uses_registered_rules_instead_of_missing_or_untrusted_context_list(self):
        import json
        engine,agent,task=self.run_engine([{'structured_response':ReviewDraft(summary='reviewed')}])
        engine.gateway.store.get.return_value={'rules':['TEAM_RULES.md']}
        with patch('code_agent_platform.engine.build_agent',return_value=agent):
            engine.run(None,task,{'project_rules':['untrusted.md']},ReviewDraft,'Review')
        prompt=json.loads(agent.invoke.call_args.args[0]['messages'][0][1])
        self.assertEqual(prompt['context']['project_rules'],['TEAM_RULES.md'])

    def test_empty_structured_turn_retries_once_with_existing_messages(self):
        previous=[('user','original evidence'),('assistant','incomplete')]
        engine,agent,task=self.run_engine([{'messages':previous,'structured_response':None},{'structured_response':ReviewDraft(summary='reviewed')}])
        with patch('code_agent_platform.engine.build_agent',return_value=agent):
            result=engine.run(None,task,{},ReviewDraft,'Review')
        self.assertEqual(result.summary,'reviewed')
        self.assertEqual(agent.invoke.call_count,2)
        self.assertEqual(agent.invoke.call_args.args[0]['messages'][:2],previous)

    def test_two_missing_structured_turns_stop_with_clear_failure(self):
        engine,agent,task=self.run_engine([{'structured_response':None},{'structured_response':None}])
        with patch('code_agent_platform.engine.build_agent',return_value=agent),self.assertRaises(PlatformError) as error:
            engine.run(None,task,{},ReviewDraft,'Review')
        self.assertEqual(error.exception.code,'model_structure_missing')
        self.assertEqual(agent.invoke.call_count,2)

    def test_truncated_output_is_not_replayed_as_another_oversized_turn(self):
        truncated = AIMessage(content='', response_metadata={'finish_reason': 'length'})
        engine,agent,task=self.run_engine([{'messages':[truncated],'structured_response':None}])
        with patch('code_agent_platform.engine.build_agent',return_value=agent),self.assertRaises(PlatformError) as error:
            engine.run(None,task,{},ReviewDraft,'Review')
        self.assertEqual(error.exception.code,'model_output_truncated')
        self.assertEqual(agent.invoke.call_count,1)

    def test_worker_disables_inherited_checkpoint_and_revalidates_model_data(self):
        response=PatchDraft(changes=[FileChange(path='a.py',content='x')],explanation='fixture')
        engine,agent,task=self.run_engine([{'structured_response':response}])
        with patch('code_agent_platform.engine.build_agent',return_value=agent) as build:
            result=engine.run(None,task,{},scoped_patch_type(['a.py']),'Return patch')
        self.assertIs(build.call_args.kwargs['memory'],False)
        self.assertEqual(result.changes[0].path,'a.py')

    def plan(self,sources):
        return PlanDraft(goal='Create a new app',files=['README.md'],steps=['Write documentation'],source_ids=sources,test_plan='Review README',rollback_plan='Discard proposed changes')

    def test_missing_plan_citations_are_repaired_in_same_turn_using_real_user_request(self):
        engine,agent,task=self.run_engine([{'structured_response':self.plan([])},{'structured_response':self.plan(['request-source'])}])
        context={'evidence':[{'source_id':'request-source','source_type':'request','path':'用户需求'}]}
        with patch('code_agent_platform.engine.build_agent',return_value=agent):result=engine.run(None,task,context,PlanDraft,'Plan')
        self.assertEqual(result.source_ids,['request-source'])
        self.assertEqual(agent.invoke.call_count,2)
        self.assertIn('request-source',agent.invoke.call_args.args[0]['messages'][-1][1])

    def test_fact_citations_are_repaired_without_accepting_the_uncited_result(self):
        missing=AnswerDraft(answer='Port occupied',findings=[Finding(claim='Log states occupied')])
        fixed=AnswerDraft(answer='Port occupied',findings=[Finding(claim='Log states occupied',source_ids=['log-request'])])
        engine,agent,task=self.run_engine([{'structured_response':missing},{'structured_response':fixed}])
        context={'evidence':[{'source_id':'log-request','source_type':'request','path':'用户提供日志'}]}
        with patch('code_agent_platform.engine.build_agent',return_value=agent):result=engine.run(None,task,context,AnswerDraft,'Answer')
        self.assertEqual(result.findings[0].source_ids,['log-request'])
        self.assertEqual(agent.invoke.call_count,2)

    def test_unknown_citations_cannot_escape_bounded_automatic_repair(self):
        engine,agent,task=self.run_engine([{'structured_response':self.plan(['invented'])}]*3)
        with patch('code_agent_platform.engine.build_agent',return_value=agent),self.assertRaises(PlatformError) as error:
            engine.run(None,task,{},PlanDraft,'Plan')
        self.assertEqual(error.exception.code,'model_evidence_invalid')
        self.assertEqual(agent.invoke.call_count,3)

    def test_automatic_repair_remains_subject_to_cancellation(self):
        engine,agent,task=self.run_engine([{'structured_response':self.plan([])},{'structured_response':self.plan(['valid'])}])
        engine.gateway.check_budget.side_effect=[{},PlatformError('task_cancelled','Stopped',409)]
        with patch('code_agent_platform.engine.build_agent',return_value=agent),self.assertRaises(PlatformError) as error:
            engine.run(None,task,{},PlanDraft,'Plan')
        self.assertEqual(error.exception.code,'task_cancelled')
        self.assertEqual(agent.invoke.call_count,1)


if __name__=='__main__':unittest.main()
