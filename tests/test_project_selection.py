import json
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from code_agent_platform.api import create_app
from code_agent_platform.config import Settings
from code_agent_platform.models import Actor,LOCAL_ACTOR,PlatformError,ProjectInput,TaskInput
from code_agent_platform.project_folders import browse_folders
import test_platform as fixtures


class SelectionTests(unittest.TestCase):
    def setUp(self):
        fixtures.PlatformTests.setUp(self)
        self.settings.allow_local_project_selection=True
        self.settings.allowed_roots=[Path(self.project['root'])]
        self.chosen=self.root/'chosen-project'
        self.chosen.mkdir()
        (self.chosen/'example.py').write_text('value = 42\n',encoding='utf-8')
        self.other=self.root/'other-project'
        self.other.mkdir()
        (self.other/'private.py').write_text('outside = True\n',encoding='utf-8')

    def select(self):
        return self.platform.select_local_project(LOCAL_ACTOR,str(self.chosen))

    def register(self,choice,root=None):
        return self.platform.register_project(LOCAL_ACTOR,ProjectInput(name='selected',root=str(root or self.chosen),selection_id=choice['id']))

    def test_external_root_requires_selection_even_when_feature_enabled(self):
        with self.assertRaises(PlatformError) as error:
            self.platform.register_project(LOCAL_ACTOR,ProjectInput(name='outside',root=str(self.chosen)))
        self.assertEqual(error.exception.code,'project_root')
        roots=list(self.settings.allowed_roots)
        listing=browse_folders(self.settings,local=True)
        self.assertTrue(listing['local_selection'])
        self.assertEqual(self.settings.allowed_roots,roots)

    def test_selection_grants_only_exact_root_and_cannot_read_sibling(self):
        choice=self.select()
        project=self.register(choice)
        self.assertEqual(project['access_source'],'local_selection')
        self.assertEqual(self.platform.store.get('project_selection',choice['id'])['status'],'consumed')
        task=self.platform.create_task(LOCAL_ACTOR,TaskInput(project_id=project['id'],user_input='inspect'))
        result=self.platform.gateway.invoke(LOCAL_ACTOR,task['id'],'read_file',{'path':'example.py'})
        self.assertIn('value = 42',result['snippet'])
        with self.assertRaises(PlatformError):
            self.platform.gateway.invoke(LOCAL_ACTOR,task['id'],'read_file',{'path':'../other-project/private.py'})
        self.assertEqual(self.settings.allowed_roots,[Path(self.project['root'])])

    def test_selection_is_bound_to_path_and_single_use(self):
        choice=self.select()
        with self.assertRaises(PlatformError) as error:self.register(choice,self.other)
        self.assertEqual(error.exception.code,'folder_selection_invalid')
        self.register(choice)
        with self.assertRaises(PlatformError) as error:self.register(choice)
        self.assertEqual(error.exception.code,'folder_selection_invalid')

    def test_expired_selection_does_not_register_project(self):
        choice=self.select()
        self.platform.store.mutate('project_selection',choice['id'],lambda row:{**row,'expires_epoch':time.time()-1})
        with self.assertRaises(PlatformError) as error:self.register(choice)
        self.assertEqual(error.exception.code,'folder_selection_invalid')

    def test_feature_disabled_and_non_local_admin_cannot_grant(self):
        self.settings.allow_local_project_selection=False
        with self.assertRaises(PlatformError):self.select()
        self.settings.allow_local_project_selection=True
        with self.assertRaises(PlatformError):
            self.platform.select_local_project(Actor(user_id='other-admin',roles={'admin'},projects={'*'}),str(self.chosen))

    def test_volume_root_and_sensitive_folder_cannot_be_projects(self):
        with self.assertRaises(PlatformError) as error:
            self.platform.select_local_project(LOCAL_ACTOR,str(Path(self.chosen.anchor)))
        self.assertEqual(error.exception.code,'folder_volume')
        sensitive=self.chosen/'.ssh'
        sensitive.mkdir()
        with self.assertRaises(PlatformError):self.platform.select_local_project(LOCAL_ACTOR,str(sensitive))

    def test_replacing_selected_directory_revokes_its_read_access(self):
        project=self.register(self.select())
        task=self.platform.create_task(LOCAL_ACTOR,TaskInput(project_id=project['id'],user_input='inspect'))
        replacement=self.root/'original-folder'
        self.assertTrue(self.chosen.resolve().is_relative_to(self.root.resolve()))
        self.assertTrue(replacement.resolve().is_relative_to(self.root.resolve()))
        self.chosen.rename(replacement)
        self.chosen.mkdir()
        (self.chosen/'example.py').write_text('replacement = True\n',encoding='utf-8')
        with self.assertRaises(PlatformError) as error:
            self.platform.gateway.invoke(LOCAL_ACTOR,task['id'],'read_file',{'path':'example.py'})
        self.assertEqual(error.exception.code,'project_root_changed')

    def test_api_accepts_explicit_selection_from_local_control_only(self):
        app=create_app(platform=self.platform)
        token=self.platform.store.local_token()
        headers={'Authorization':'Bearer '+token}
        with TestClient(app,client=('127.0.0.1',50000)) as client:
            selected=client.post('/api/project-selections',json={'path':str(self.chosen)},headers=headers)
            self.assertEqual(selected.status_code,201)
            payload={'name':'chosen','root':str(self.chosen),'selection_id':selected.json()['selection_id']}
            registered=client.post('/api/projects',json=payload,headers=headers)
            self.assertEqual(registered.status_code,201)
            listed=client.get('/api/project-folders',headers=headers)
            self.assertTrue(listed.json()['local_selection'])
            self.assertEqual(client.post('/api/project-selections',json={'path':str(self.other)}).status_code,401)

    def test_remote_client_cannot_browse_or_issue_selection(self):
        app=create_app(platform=self.platform)
        headers={'Authorization':'Bearer '+self.platform.store.local_token()}
        with TestClient(app,client=('198.51.100.10',50000)) as client:
            self.assertEqual(client.get('/api/project-folders',headers=headers).status_code,403)
            self.assertEqual(client.post('/api/project-selections',json={'path':str(self.chosen)},headers=headers).status_code,403)

    def test_settings_loads_explicit_local_and_mysql_switches(self):
        with patch.dict(os.environ,{'AI_AGENT_LOCAL_PROJECT_SELECTION':'true','AI_AGENT_MYSQL_ENABLED':'true'}):
            settings=Settings.load()
        self.assertTrue(settings.allow_local_project_selection)
        self.assertTrue(settings.mcp_servers['mysql_readonly'].enabled)


if __name__=='__main__':unittest.main()
