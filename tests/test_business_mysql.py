import json,unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from code_agent_platform.api import create_app
from code_agent_platform.models import LOCAL_ACTOR,Actor,Mode,PlatformError,PlanDraft
import test_platform as fixtures


class BusinessMySQLTests(unittest.TestCase):
    setUp=fixtures.PlatformTests.setUp
    task=fixtures.PlatformTests.task
    approve=fixtures.PlatformTests.approve

    def test_private_store_roundtrip_is_not_plaintext_and_contract_has_no_password(self):
        config={'host':'127.0.0.1','port':3306,'user':'fixture_manager','password':'fixture-very-secret','revision':'fixture','auto_provision':True,'server_version':'fixture'}
        private=self.platform.business_mysql.private
        private.write('mysql-instance',config)
        self.assertEqual(private.read('mysql-instance'),config)
        self.assertNotIn(config['password'],private.path('mysql-instance').read_text())
        self.assertNotIn(config['password'],json.dumps(self.platform.business_mysql.instance()))
        contract=self.platform.business_mysql.contract(self.project)
        self.assertEqual(contract['source'],'platform_managed')
        self.assertIn('APP_DB_PASSWORD',contract['environment_variables'])
        self.assertNotIn(config['password'],json.dumps(contract))

    def test_existing_complete_local_connection_is_preserved(self):
        target=Path(self.project['root'])/'.env'
        content='APP_JDBC_URL=jdbc:mysql://localhost/old\nAPP_DB_USER=old\nAPP_DB_PASSWORD=unchanged\nMIGRATION_DB_USER=old_mig\nMIGRATION_DB_PASSWORD=unchanged_mig\n'
        target.write_text(content)
        result=self.platform.business_mysql.ensure(self.actor,self.project['id'])
        self.assertEqual(result['status'],'external')
        self.assertEqual(target.read_text(),content)
        self.assertNotIn('unchanged',json.dumps(result))

    def test_incomplete_connection_is_never_silently_overwritten(self):
        self.platform.business_mysql.private.write('mysql-instance',{'auto_provision':True,'host':'127.0.0.1','port':3306,'user':'fixture','revision':'fixture','server_version':'fixture'})
        target=Path(self.project['root'])/'.env'
        target.write_text('APP_DB_PASSWORD=keep-this\n')
        with self.assertRaises(PlatformError) as blocked:self.platform.business_mysql.ensure(self.actor,self.project['id'])
        self.assertEqual(blocked.exception.code,'database_config_exists')
        self.assertEqual(target.read_text(),'APP_DB_PASSWORD=keep-this\n')

    def test_database_is_prepared_after_plan_approval_before_patch_without_prompt_credentials(self):
        from code_agent_platform.engine import DemoEngine
        class NeedsDatabase(DemoEngine):
            def plan(engine,actor,task,context):
                result=super().plan(actor,task,context)
                return result.model_copy(update={'requires_database':True})
        self.platform.engine_override=NeedsDatabase()
        task=self.task(Mode.WRITE)
        with patch.object(self.platform.business_mysql,'ensure',return_value={'ready':True}) as provision:
            self.platform.run(self.actor,task['id'])
            provision.assert_not_called()
            result=self.approve(task['id'])
            provision.assert_called_once_with(self.actor,self.project['id'])
        self.assertEqual(result['status'],'awaiting_diff')

    def test_instance_api_is_local_admin_only_and_never_returns_secret(self):
        client=TestClient(create_app(platform=self.platform),client=('127.0.0.1',40123))
        headers={'Authorization':'Bearer '+self.platform.store.local_token()}
        data={'host':'127.0.0.1','port':3306,'user':'fixture','password':'very-secret'}
        with patch.object(self.platform.business_mysql,'configure',return_value={'configured':True,'password_configured':True}) as configure:
            response=client.put('/api/mysql-instance',json=data,headers=headers)
            self.assertEqual(response.status_code,200)
            self.assertNotIn('very-secret',response.text)
            self.assertEqual(configure.call_args.args[4],'very-secret')
        remote=TestClient(create_app(platform=self.platform),client=('192.0.2.1',40123))
        self.assertEqual(remote.put('/api/mysql-instance',json=data,headers=headers).status_code,403)


if __name__=='__main__':unittest.main()
