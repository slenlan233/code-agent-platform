import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from code_agent_platform.database_runtime import MySQLTestEnvironment,scrub,verify_with_mysql
from code_agent_platform.junit_reports import read_junit_reports
from code_agent_platform.models import LOCAL_ACTOR,PlatformError,ProjectInput
from code_agent_platform.runtime import DockerRunner
import test_platform as fixtures


class DatabaseRuntimeTests(unittest.TestCase):
    setUp=fixtures.PlatformTests.setUp
    task=fixtures.PlatformTests.task

    def test_network_profile_needs_explicit_administrator_switch(self):
        with self.assertRaises(PlatformError) as error:
            self.platform.update_project(LOCAL_ACTOR,self.project['id'],database_test_profile='mysql')
        self.assertEqual(error.exception.code,'network_denied')
        self.settings.allow_mysql_test_network=True
        project=self.platform.update_project(LOCAL_ACTOR,self.project['id'],database_test_profile='mysql')
        self.assertEqual(project['database_test_profile'],'mysql')

    def test_runtime_network_rejects_host_or_unregistered_networks(self):
        runner=DockerRunner(self.settings)
        for network in ('host','bridge','someone-elses-network'):
            with self.assertRaises(PlatformError):runner.command(self.project,{'path':'C:/fixture','database_network':network},'fixture')

    def test_mysql_network_preserves_resource_and_privilege_limits(self):
        self.settings.allow_mysql_test_network=True
        project={**self.project,'database_test_profile':'mysql'}
        network='ai-agent-db-'+'a'*32
        command=DockerRunner(self.settings).command(project,{'path':'C:/fixture','database_network':network,'database_env_file':'C:/private/fixture.env'},'fixture')
        self.assertIn('--network='+network,command)
        self.assertIn('--read-only',command)
        self.assertIn('--cap-drop=ALL',command)
        self.assertIn('--memory=1g',command)
        self.assertIn('--env-file',command)
        self.assertNotIn('--network=host',command)

    def test_generated_passwords_are_removed_from_nested_reports(self):
        value={'stdout':'a-private-password','database':{'details':['a-private-password']}}
        self.assertNotIn('a-private-password',json.dumps(scrub(value,['a-private-password'])))

    def test_junit_counts_errors_skips_and_test_names(self):
        folder=self.root/'target/surefire-reports'
        folder.mkdir(parents=True)
        (folder/'TEST-fixture.xml').write_text('<testsuite name="fixture" tests="3" failures="1" errors="0" skipped="1"><testcase name="pass"/><testcase name="fail"><failure/></testcase><testcase name="skip"><skipped/></testcase></testsuite>',encoding='utf-8')
        result=read_junit_reports(self.root)
        self.assertEqual((result['tests'],result['failures'],result['skipped']),(3,1,1))
        self.assertEqual([row['status'] for row in result['suites'][0]['cases']],['passed','failed','skipped'])

    def test_owned_resource_cleanup_refuses_foreign_container(self):
        environment=MySQLTestEnvironment(self.settings,self.task())
        foreign=SimpleNamespace(returncode=0,stdout=json.dumps([{'Id':'foreign','Config':{'Labels':{'com.ai-agent.owner':'not-ours'}}}]).encode())
        with patch('code_agent_platform.database_runtime.docker',return_value=foreign) as cli:
            self.assertFalse(environment._remove_owned('container','foreign'))
            self.assertEqual(cli.call_count,1)

    def test_database_failure_still_triggers_cleanup_and_redaction(self):
        runner=DockerRunner(self.settings)
        self.settings.allow_mysql_test_network=True
        task=self.task()
        with patch.object(runner,'health',return_value={'available':True}),patch('code_agent_platform.database_runtime.MySQLTestEnvironment') as factory:
            env=factory.return_value
            env.credentials=['private-fixture']
            env.container_created=False
            env.cleanup={'database_removed':True,'network_removed':True}
            env.start.side_effect=PlatformError('database_start_failed','private-fixture')
            env.info.return_value={}
            result=verify_with_mysql(runner,task,self.project,{'path':str(self.root)})
            env.close.assert_called_once()
            self.assertEqual(result['status'],'failed')
            self.assertNotIn('private-fixture',json.dumps(result))


if __name__=='__main__':unittest.main()
