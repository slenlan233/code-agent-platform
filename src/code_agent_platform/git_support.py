"""Optional, project-scoped Git inspection for ordinary coding tasks."""
import os
import subprocess
from pathlib import Path

from .models import PlatformError


def git_environment():
    # Inherited GIT_DIR/WORK_TREE/CONFIG_* must not redirect the selected project.
    return {**{k:v for k,v in os.environ.items() if not k.upper().startswith('GIT_')},
            'GIT_TERMINAL_PROMPT':'0','GIT_CONFIG_NOSYSTEM':'1','LC_ALL':'C'}


def git_run(project,arguments,*,timeout=20):
    try:
        return subprocess.run(['git','-c','core.fsmonitor=false','-c','core.untrackedCache=false','-c','core.pager=cat','-C',project['root'],*arguments],capture_output=True,timeout=timeout,env=git_environment())
    except (OSError,subprocess.TimeoutExpired):
        raise PlatformError('git_unavailable','Git 查询暂不可用；普通项目仍可使用文件快照。') from None


def git_output(project,arguments):
    result=git_run(project,arguments)
    if result.returncode:
        raise PlatformError('git_query_failed','Git 查询未成功，请确认仓库状态和提交引用。')
    return result.stdout.decode('utf-8',errors='replace')[:40_000]


def repository_info(project):
    fallback={'available':False,'repository':False,'revision':None,'baseline':'file_hashes'}
    try:
        result=git_run(project,['rev-parse','--show-toplevel'])
        if result.returncode:
            missing=b'not a git repository' in result.stderr.lower()
            return {**fallback,'state':'not_repository' if missing else 'unavailable','message':'当前文件夹没有 Git 仓库，使用文件快照和内容校验继续。' if missing else '无法读取 Git 状态，普通任务可使用文件快照继续。'}
        if Path(result.stdout.decode('utf-8',errors='replace').strip()).resolve()!=Path(project['root']).resolve():
            return {**fallback,'state':'outside_project','message':'当前文件夹没有独立的 Git 仓库，使用所选文件夹的快照继续。'}
        head=git_run(project,['rev-parse','--verify','HEAD'])
        revision=head.stdout.decode('utf-8',errors='replace').strip() if head.returncode==0 else None
        return {'available':True,'repository':True,'state':'ready' if revision else 'no_commits','revision':revision,'baseline':'git_and_file_hashes' if revision else 'file_hashes','message':'已读取当前项目的 Git 提交。' if revision else 'Git 仓库尚无提交，使用文件快照和内容校验继续。'}
    except PlatformError:
        return {**fallback,'state':'unavailable','message':'Git 查询暂不可用，普通任务可使用文件快照继续。'}
