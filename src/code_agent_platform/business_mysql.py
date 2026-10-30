"""Provision project-owned persistent MySQL databases outside the model tool layer."""
import os
import re
import secrets
import stat
import tempfile
from pathlib import Path

import pymysql
from dotenv import dotenv_values

from .models import LOCAL_ACTOR,PlatformError,digest,identifier,timestamp
from .private_store import PrivateStore

ENV_KEYS=('APP_JDBC_URL','APP_DB_USER','APP_DB_PASSWORD','MIGRATION_DB_USER','MIGRATION_DB_PASSWORD')


def connect(config,**override):
    return pymysql.connect(host=config['host'],port=config['port'],user=config['user'],password=config['password'],connect_timeout=5,read_timeout=8,write_timeout=8,autocommit=True,**override)


class BusinessMySQL:
    def __init__(self,platform):
        self.platform,self.store,self.settings=platform,platform.store,platform.settings
        self.private=PrivateStore(self.settings.data_dir)

    def instance(self):
        value=self.private.read('mysql-instance')
        if not value:return {'configured':False,'auto_provision':False}
        return {key:value[key] for key in ('host','port','user','revision','server_version','auto_provision')}|{'configured':True,'password_configured':True,'schema_prefix':'agent_app_','protection':'Windows 用户加密' if os.name=='nt' else '本地文件权限'}

    def configure(self,actor,host,port,user,password,auto_provision=True):
        actor.require('admin')
        if actor.user_id!=LOCAL_ACTOR.user_id:raise PlatformError('local_control_required','请在本机配置数据库实例。',403)
        if host not in {'127.0.0.1','localhost','::1'}:raise PlatformError('mysql_host','当前自动建库配置支持本机 MySQL 实例。')
        config={'host':host,'port':port,'user':user,'password':password,'auto_provision':auto_provision,'revision':identifier('mysql')}
        try:
            with connect(config) as connection:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT VERSION()')
                    config['server_version']=cursor.fetchone()[0]
            self.private.write('mysql-instance',config)
        except (pymysql.MySQLError,OSError,ValueError):
            raise PlatformError('mysql_configuration','连接校验或本地凭证保存失败，请核对实例地址和账号。') from None
        return self.instance()

    @staticmethod
    def env(project):
        root=Path(project['root']).resolve()
        target=root/'.env'
        if target.exists() or target.is_symlink():
            info=target.lstat()
            if target.is_symlink() or getattr(info,'st_file_attributes',0)&getattr(stat,'FILE_ATTRIBUTE_REPARSE_POINT',0x400) or not target.is_file() or info.st_size>256000:
                raise PlatformError('database_config_path','本地数据库配置必须是项目内的普通文件。',403)
        return target

    def record(self,project):
        try:return self.store.get('project_database','business_'+project['id'])
        except PlatformError as error:
            if error.code!='not_found':raise
            return None

    @staticmethod
    def write_env(target, content):
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        try:
            temporary.chmod(0o600)
            os.replace(temporary, target)
            target.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)

    def public(self,project):
        record=self.record(project)
        target=self.env(project)
        if not record:
            values=dotenv_values(target) if target.exists() else {}
            if all(values.get(key) for key in ENV_KEYS):
                return {'status':'external','ready':True,'managed':False,'env_file':'.env','message':'沿用项目已有的本地业务数据库配置，不覆盖。'}
            return {'status':'not_prepared','ready':False,'managed':False,'env_file':'.env','auto_available':self.instance().get('auto_provision',False)}
        result={key:record.get(key) for key in ('status','host','port','database','application_user','migration_user','error')}
        result.update(ready=record['status']=='ready',managed=True,env_file='.env')
        if record['status'] == 'deleted':
            result.update(deleted_at=record.get('deleted_at'), auto_available=self.instance().get('auto_provision', False))
        if result['ready']:
            values=dotenv_values(target) if target.exists() else {}
            if digest({k:values.get(k) for k in ENV_KEYS})!=record.get('env_hash'):
                result.update(status='config_changed',ready=False,error='本地数据库连接已变更，平台不会覆盖，请检查项目配置。')
        return result

    def contract(self,project):
        public=self.public(project)
        base={'provider':'mysql','env_file':'.env','environment_variables':list(ENV_KEYS),'passwords':'由平台注入本地配置，不向模型或提示词提供','persistent':True}
        if public.get('status')=='external':return {**base,'source':'existing_project_configuration'}
        record=self.record(project)
        if record and record['status'] == 'deleted':
            record = None
        config=self.instance()
        if not record and not config.get('configured'):return {**base,'source':'not_configured'}
        suffix=project['id'].removeprefix('project_')
        return {**base,'source':'platform_managed','host':record['host'] if record else config['host'],'port':record['port'] if record else config['port'],'database':record['database'] if record else 'agent_app_'+suffix,'application_user':record['application_user'] if record else 'agp_'+suffix[:20]+'_app','migration_user':record['migration_user'] if record else 'agp_'+suffix[:20]+'_mig','instance_revision':record.get('instance_revision') if record else config.get('revision')}

    def ensure(self,actor,project_id):
        project=self.platform.project(actor,project_id,write=True)
        lease=self.store.acquire(project_id,kind='project')
        try:return self._ensure(project)
        finally:self.store.release(project_id,lease,kind='project')

    def _ensure(self,project):
        public=self.public(project)
        if public['ready']:return public
        if public['status']=='config_changed':raise PlatformError('database_config_changed',public['error'],409)
        config=self.private.read('mysql-instance')
        if not config or not config.get('auto_provision'):
            raise PlatformError('mysql_instance_missing','尚未配置自动建库实例。请在项目详情的“业务数据库”中配置 MySQL。',409)
        target=self.env(project)
        before=target.read_bytes() if target.exists() else b''
        values=dotenv_values(target) if target.exists() else {}
        record=self.record(project)
        if record and record['status'] == 'deleting':
            raise PlatformError('database_deleting', '数据库删除尚未完成，请先恢复清理任务。', 409)
        if record and record['status'] == 'deleted':
            record = None
        if not record and any(key in values for key in ENV_KEYS):
            raise PlatformError('database_config_exists','项目已有部分数据库配置，请补齐它；平台不会覆盖现有连接。',409)
        secret_name='database-'+project['id']
        credentials=self.private.read(secret_name) if record else None
        suffix=project['id'].removeprefix('project_')
        if not re.fullmatch(r'[0-9a-f]{32}',suffix):raise PlatformError('project_id','项目标识无效。')
        if record and not credentials:raise PlatformError('database_secret_missing','数据库配置凭证缺失，请恢复本地平台数据。',409)
        if not record:
            credentials={'application_password':secrets.token_urlsafe(24),'migration_password':secrets.token_urlsafe(24)}
            self.private.write(secret_name,credentials)
            record={'id':'business_'+project['id'],'project_id':project['id'],'status':'preparing','database':'agent_app_'+suffix,'application_user':'agp_'+suffix[:20]+'_app','migration_user':'agp_'+suffix[:20]+'_mig','host':config['host'],'port':config['port'],'instance_revision':config['revision'],'created_at':timestamp(),'allocation_id':identifier('allocation'),'new_allocation':True}
            self.store.put('project_database',record)
        schema=record['database']
        try:
            with connect(config) as connection:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME=%s',(schema,))
                    exists=cursor.fetchone() is not None
                    if exists and record.get('new_allocation'):
                        raise PlatformError('database_collision','新项目的数据库名称已被占用，未接管现有数据库。',409)
                    # Mark ownership intent before issuing idempotent DDL, for crash recovery.
                    record=self.store.put('project_database',{**record,'new_allocation':False})
                    cursor.execute(f'CREATE DATABASE IF NOT EXISTS `{schema}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci')
                    cursor.execute('SELECT @@partial_revokes')
                    partial=cursor.fetchone()[0]
                    grant_schema=schema if partial else schema.replace('_','\\_')
                    for role,permissions in [('application','SELECT,INSERT,UPDATE,DELETE'),('migration','SELECT,INSERT,UPDATE,DELETE,CREATE,ALTER,DROP,INDEX,REFERENCES')]:
                        user=record[role+'_user']; password=credentials[role+'_password']
                        cursor.execute('CREATE USER IF NOT EXISTS %s@%s IDENTIFIED BY %s WITH MAX_USER_CONNECTIONS 8',(user,'localhost',password))
                        # An existing account is trusted only if its saved project credential works.
                        with connect({**config,'user':user,'password':password}) as probe:
                            with probe.cursor() as check:check.execute('SELECT CURRENT_USER()');check.fetchone()
                        cursor.execute(f'GRANT {permissions} ON `{grant_schema}`.* TO %s@%s',(user,'localhost'))
            env_values={'APP_JDBC_URL':f"jdbc:mysql://{record['host']}:{record['port']}/{schema}?useSSL=false&allowPublicKeyRetrieval=true&serverTimezone=UTC&characterEncoding=UTF-8",'APP_DB_USER':record['application_user'],'APP_DB_PASSWORD':credentials['application_password'],'MIGRATION_DB_USER':record['migration_user'],'MIGRATION_DB_PASSWORD':credentials['migration_password']}
            # Do not replace unrelated environment settings or an intervening edit.
            if (target.read_bytes() if target.exists() else b'')!=before:raise PlatformError('database_config_changed','项目本地配置在准备期间发生变化，未覆盖。',409)
            block=('\n# Platform-managed project MySQL connection\n'+'\n'.join(k+'='+v for k,v in env_values.items())+'\n').encode()
            with tempfile.NamedTemporaryFile(dir=target.parent,delete=False) as handle:
                temporary=Path(handle.name);handle.write(before+block)
            try:
                temporary.chmod(0o600);os.replace(temporary,target);target.chmod(0o600)
            finally:temporary.unlink(missing_ok=True)
            self.store.put('project_database',{**record,'status':'ready','env_hash':digest(env_values),'error':None})
            return self.public(project)
        except (pymysql.MySQLError,OSError) as error:
            self.store.put('project_database',{**record,'status':'failed','error':'数据库准备失败，请检查实例账号的建库和授权权限。'})
            raise PlatformError('database_prepare_failed','数据库准备失败，请检查实例账号的建库和授权权限。',409) from None
