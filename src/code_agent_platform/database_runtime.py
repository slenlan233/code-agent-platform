"""Task-owned MySQL and a private Docker network for real backend tests."""
import json
import re
import secrets
import subprocess
import tempfile
import time
from pathlib import Path

from .models import PlatformError,identifier


def docker(arguments,*,timeout=20,check=True):
    result=subprocess.run(['docker',*arguments],capture_output=True,timeout=timeout)
    if check and result.returncode:
        raise PlatformError('database_runtime','数据库测试环境操作失败，请查看已脱敏的验证报告。')
    return result


def scrub(value,credentials):
    if isinstance(value,dict):return {key:scrub(item,credentials) for key,item in value.items()}
    if isinstance(value,list):return [scrub(item,credentials) for item in value]
    if isinstance(value,str):
        for secret in credentials:value=value.replace(secret,'[redacted]')
    return value


class MySQLTestEnvironment:
    def __init__(self,settings,task,check_cancelled=None):
        self.settings,self.task=settings,task
        self.check_cancelled=check_cancelled or (lambda task: None)
        self.run_id=identifier('run').split('_',1)[1]
        self.network='ai-agent-db-'+self.run_id
        self.container='ai-agent-mysql-it-'+self.run_id[:12]
        self.application='ai-agent-java-it-'+self.run_id[:12]
        self.credentials=[secrets.token_urlsafe(24) for _ in range(3)]
        self.directory=None
        self.network_created=False
        self.container_created=False
        self.cleanup={'database_removed':False,'network_removed':False}

    def start(self):
        self.check_cancelled(self.task)
        if not self.settings.allow_mysql_test_network:
            raise PlatformError('network_denied','MySQL 集成测试网络尚未启用。',403)
        if not re.fullmatch(r'task_[0-9a-f]{32}',self.task['id']):
            raise PlatformError('task_id','任务标识不正确。')
        image=docker(['image','inspect','--format','{{.Id}}',self.settings.mysql_test_image],check=False)
        if image.returncode:
            raise PlatformError('database_image_missing','请先准备管理员登记的 MySQL 测试镜像。',409)
        self.image_digest=image.stdout.decode().strip()
        base=(self.settings.data_dir/'database-runs').resolve()
        base.mkdir(parents=True,exist_ok=True)
        self.directory=tempfile.TemporaryDirectory(prefix=self.run_id+'-',dir=base)
        folder=Path(self.directory.name).resolve()
        if folder==base or not folder.is_relative_to(base):
            raise PlatformError('database_directory','数据库运行目录不合法。')
        self.folder=folder
        root_password,migration_password,app_password=self.credentials
        root_env=folder/'mysql.env'
        root_env.write_text('MYSQL_ROOT_PASSWORD='+root_password+'\nMYSQL_DATABASE=agent_it\n',encoding='utf-8')
        self.app_env=folder/'application.env'
        self.app_env.write_text('\n'.join([
            'APP_JDBC_URL=jdbc:mysql://mysql:3306/agent_it?useSSL=false&allowPublicKeyRetrieval=true&serverTimezone=UTC&characterEncoding=UTF-8',
            'APP_DB_USER=app_writer','APP_DB_PASSWORD='+app_password,
            'MIGRATION_DB_USER=schema_migrator','MIGRATION_DB_PASSWORD='+migration_password,
        ])+'\n',encoding='utf-8')
        init=folder/'init'
        init.mkdir()
        sql=init/'001-users.sql'
        sql.write_text(
            f"CREATE USER 'schema_migrator'@'%' IDENTIFIED BY '{migration_password}';\n"
            "GRANT SELECT,INSERT,UPDATE,DELETE,CREATE,ALTER,DROP,INDEX,REFERENCES ON `agent\\_it`.* TO 'schema_migrator'@'%';\n"
            f"CREATE USER 'app_writer'@'%' IDENTIFIED BY '{app_password}';\n"
            "GRANT SELECT,INSERT,UPDATE,DELETE ON `agent\\_it`.* TO 'app_writer'@'%';\n",encoding='utf-8')
        for secret_file in (root_env,self.app_env):secret_file.chmod(0o600)
        # The private parent is 0700 on POSIX; the MySQL entrypoint, after
        # dropping privileges, still needs to read this explicit bind mount.
        init.chmod(0o755)
        sql.chmod(0o644)
        labels=['--label','com.ai-agent.owner=database-test','--label','com.ai-agent.run='+self.run_id,'--label','com.ai-agent.task='+self.task['id']]
        self.network_created=True
        docker(['network','create','--internal','--opt','com.docker.network.bridge.gateway_mode_ipv4=isolated',*labels,self.network])
        self.container_created=True
        docker(['run','-d','--name',self.container,*labels,'--pull=never','--network',self.network,'--network-alias','mysql','--cpus=1','--memory=768m','--pids-limit=128',
                '--tmpfs','/var/lib/mysql:rw,nosuid,size=512m','--env-file',str(root_env),'--mount',f'type=bind,source={init},target=/docker-entrypoint-initdb.d,readonly',self.image_digest])
        deadline=time.monotonic()+self.settings.mysql_test_startup_seconds
        query='MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql --protocol=TCP -h127.0.0.1 -uroot --batch --skip-column-names -e "SELECT VERSION()"'
        while time.monotonic()<deadline:
            self.check_cancelled(self.task)
            result=docker(['exec',self.container,'sh','-c',query],check=False,timeout=8)
            if result.returncode==0:
                self.server_version=result.stdout.decode(errors='replace').strip()
                return self
            state=docker(['inspect','--format','{{.State.Running}}',self.container],check=False)
            if state.returncode==0 and state.stdout.strip()==b'false':
                raise PlatformError('database_start_failed','MySQL 测试容器启动失败。')
            time.sleep(0.5)
        raise PlatformError('database_start_timeout','MySQL 测试数据库未在限定时间内就绪。')

    def workspace(self,workspace):
        return {**workspace,'database_network':self.network,'database_env_file':str(self.app_env),'container_name':self.application}

    def info(self):
        return {'engine':'mysql','schema':'agent_it','migration_user':'schema_migrator','application_user':'app_writer','application_permissions':['SELECT','INSERT','UPDATE','DELETE'],
                'migration_permissions':['SELECT','INSERT','UPDATE','DELETE','CREATE','ALTER','DROP','INDEX','REFERENCES'],
                'server_version':getattr(self,'server_version',None),'image_digest':getattr(self,'image_digest',None),'container_name':self.container,'application_container':self.application,
                'network_name':self.network,'network_internal':True,'gateway_mode':'isolated','published_ports':False,'lifecycle':'per_task'}

    def close(self):
        try:
            if self.container_created:
                self.cleanup['database_removed']=self._remove_owned('container',self.container)
            else:self.cleanup['database_removed']=True
            if self.network_created:
                self.cleanup['network_removed']=self._remove_owned('network',self.network)
            else:self.cleanup['network_removed']=True
        finally:
            if self.directory:
                base=(self.settings.data_dir/'database-runs').resolve()
                target=Path(self.directory.name).resolve()
                if target!=base and target.is_relative_to(base):self.directory.cleanup()

    def _remove_owned(self,kind,name):
        result=docker([kind,'inspect',name],check=False)
        if result.returncode:return False
        detail=json.loads(result.stdout)[0]
        labels=detail.get('Config',{}).get('Labels',{}) if kind=='container' else detail.get('Labels',{})
        if labels.get('com.ai-agent.owner')!='database-test' or labels.get('com.ai-agent.run')!=self.run_id:return False
        args=['rm','-f',detail['Id']] if kind=='container' else ['network','rm',detail['Id']]
        return docker(args,check=False).returncode==0


def verify_with_mysql(runner,task,project,workspace):
    environment=MySQLTestEnvironment(runner.settings,task,check_cancelled=runner.check_cancelled)
    result=None
    try:
        if not runner.health()['available']:
            return {'status':'unavailable','reason':'Docker 服务不可用。','exit_code':None,'stdout':'','stderr':''}
        environment.start()
        result=runner._verify_container(task,project,environment.workspace(workspace))
        result['database']=environment.info()
    except (PlatformError,subprocess.SubprocessError,OSError) as error:
        if isinstance(error, PlatformError) and error.code == 'task_cancelled':
            raise
        logs=''
        if environment.container_created:
            try:
                output=docker(['logs','--tail','60',environment.container],check=False)
                logs=(output.stdout+output.stderr).decode('utf-8',errors='replace')[-12000:]
            except (OSError,subprocess.SubprocessError):pass
        result={'status':'unavailable' if getattr(error,'code','')=='database_image_missing' else 'failed','reason':getattr(error,'message','数据库集成测试执行失败。'),
                'exit_code':None,'stdout':'','stderr':logs,'backend':'docker','network':'mysql_internal','database':environment.info()}
    finally:
        try:environment.close()
        except (OSError,subprocess.SubprocessError,PlatformError):pass
        if result is not None:
            result['database_cleanup']=dict(environment.cleanup)
            if result['status']=='passed' and not all(environment.cleanup.values()):
                result.update(status='failed',reason='数据库测试资源未完全清理，不能报告验收完成。')
    return scrub(result,environment.credentials)
