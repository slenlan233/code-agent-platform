"""Persistent public conversations layered over independently approved tasks."""
import json
import time
from .models import PlatformError,digest,identifier,timestamp
from .security import redact

ACTIVE = {"queued","running","cancelling","awaiting_plan","awaiting_diff","awaiting_database","waiting_environment"}


def is_active(task):
    return task['status'] in ACTIVE or task['status']=='completed' and (task.get('delivery') or {}).get('status') in {'pending','writing'}


class Conversations:
    def __init__(self, platform):
        self.platform,self.store=platform,platform.store

    def session(self,actor,session_id,*,allow_inactive=False):
        if allow_inactive:return self.platform.library.session_record(actor, session_id)
        actor=self.platform.gateway.current_actor(actor)
        row=self.store.get("session",session_id)
        self.platform.project(actor,row["project_id"])
        if row["owner_id"]!=actor.user_id and "admin" not in actor.roles:
            raise PlatformError("session_denied","该对话不属于当前用户。",403)
        from .security import require_active_record
        require_active_record(row, '对话')
        return row

    def create(self,actor,project_id,title=""):
        actor=self.platform.gateway.current_actor(actor)
        actor.require("developer")
        self.platform.project(actor,project_id)
        return self.store.put("session",{"project_id":project_id,"owner_id":actor.user_id,"title":title.strip()[:100],"summary":{},"policy_rules":[],"conversation_version":1})

    def tasks(self,row):
        linked=set(row.get("linked_task_ids",[]))
        return sorted((item for item in self.store.list("task",project_id=row["project_id"]) if item.get("session_id")==row["id"] or item["id"] in linked),key=lambda item:(item["created_at"],item["id"]))

    def predecessors(self, task):
        history = self.tasks(self.store.get('session', task['session_id']))
        if 'history_task_ids' in task:
            allowed = set(task['history_task_ids'])
            history = [t for t in history if t['id'] in allowed]
        else:
            boundary = (task['created_at'], task['id'])
            history = [t for t in history if (t['created_at'], t['id']) < boundary]
        return [t for t in history if t.get('kind') != 'connector_check']

    def ensure_turn_available(self,task):
        from .security import require_active_record
        require_active_record(self.store.get('session', task['session_id']), '对话')
        if task["status"] in {"completed","cancelled"}:return
        row=self.store.get("session",task["session_id"])
        if any(other["id"]!=task["id"] and is_active(other) and other.get("kind")!="connector_check" for other in self.tasks(row)):
            raise PlatformError("conversation_busy","这个对话正在处理另一轮，请先完成或停止当前执行。",409)

    def describe(self,row,tasks=None):
        tasks=[t for t in (self.tasks(row) if tasks is None else tasks) if t.get("kind")!="connector_check"]
        latest=tasks[-1] if tasks else None
        usage={key:sum(t.get("usage",{}).get(key) or 0 for t in tasks) for key in ("input_tokens","output_tokens","model_calls","tool_calls")}
        for key in ("cache_read_tokens","uncached_input_tokens"):
            usage[key]=None if any(t.get("usage",{}).get("model_calls") and t["usage"].get(key) is None for t in tasks) else sum(t.get("usage",{}).get(key) or 0 for t in tasks)
        return {**row,"title":row.get("title") or (tasks[0]["user_input"].splitlines()[0][:42] if tasks else "新对话"),"preview":latest["user_input"][:100] if latest else "开始你的第一个需求", "status":latest["status"] if latest else "idle","task_count":len(tasks),"active_task_id":next((t["id"] for t in tasks if is_active(t)),None),"last_activity":max([row["created_at"],*(t["updated_at"] for t in tasks)]),"usage":usage}

    def list(self,actor,project_id):
        self.platform.project(actor,project_id)
        result=[]
        for row in self.store.list("session",project_id=project_id):
            if row["owner_id"]!=actor.user_id and "admin" not in actor.roles:continue
            if row.get("hidden_in_list") or row.get('lifecycle', 'active') != 'active':continue
            tasks=self.tasks(row)
            if tasks and all(t.get("kind")=="connector_check" for t in tasks):continue
            result.append(self.describe(row,tasks))
        return sorted(result,key=lambda row:row["last_activity"],reverse=True)

    def rename(self,actor,session_id,title):
        self.session(actor,session_id)
        return self.store.mutate("session",session_id,lambda row:{**row,"title":title.strip()[:100]})

    def save(self,task,key,kind,content,*,role="assistant",metadata=None,created_at=None):
        if task.get("kind")=="connector_check":return
        row={"id":"message_"+digest([task["id"],key])[:32],"project_id":task["project_id"],"task_id":task["id"],"session_id":task["session_id"],"role":role,"kind":kind,"content":redact(content),"metadata":redact(metadata or {}),"legacy":not bool(task.get("conversation_version"))}
        try:
            old=self.store.get("message",row["id"])
        except PlatformError as error:
            if error.code!="not_found":raise
            old=None
        if old and all(old.get(k)==v for k,v in row.items()):return old
        row["created_at"]=old["created_at"] if old else created_at or timestamp()
        return self.store.put("message",row)

    def sync_task(self,task):
        if task.get("kind")=="connector_check":return
        self.save(task,"user","user",task["user_input"],role="user",created_at=task["created_at"])
        if task.get('knowledge_warnings'):
            self.save(task, 'knowledge_warning', 'notice', '\n'.join(task['knowledge_warnings']))
        if task['status'] == 'cancelling':
            self.save(task, 'stopping', 'notice', '正在停止请求并清理测试资源，请等待“已停止”后继续发送。')
        if task.get("plan_id"):
            plan=self.store.get("plan",task["plan_id"])
            self.save(task,plan["id"],"plan",plan["goal"],metadata={"plan_id":plan["id"],"files":plan["files"],"steps":plan["steps"]},created_at=plan["created_at"])
        if task.get("patch_id"):
            patch=self.store.get("patch",task["patch_id"])
            self.save(task,patch["id"],"patch","已生成 "+str(len(patch["files"]))+" 个文件的改动。",metadata={"patch_id":patch["id"],"files":patch["files"]},created_at=patch["created_at"])
        if task.get("verification"):
            run=task["verification"]
            label={"passed":"验证通过","failed":"验证未通过","unavailable":"验证环境尚未就绪"}.get(run["status"],run["status"])
            text=label+("，测试 "+str(run["test_count"])+" 项。" if run.get("test_count") is not None else "。")
            if run.get('verification_kind') == 'project_reset':
                text = label+'，核对 '+str(run['checked_file_count'])+' 个文件的清空范围；未运行应用构建或集成测试。'
            self.save(task,run["id"],"verification",text,metadata={"status":run["status"],"test_count":run.get("test_count")},created_at=run["created_at"])
        if task.get("answer"):
            self.save(task,"answer","answer",task["answer"]["answer"],created_at=task.get("finished_at") or task["updated_at"])
        if task.get("review"):
            self.save(task,"review_"+task.get("patch_id","readonly"),"review",task["review"]["summary"],metadata={"issue_count":len(task["review"].get("issues",[]))},created_at=task.get("finished_at") or task["updated_at"])
        if task.get('database_result'):
            result = task['database_result']
            self.save(task, result['operation_id'], 'database', '已删除项目数据库 '+result['database']+'，并清理专属账号及本地连接配置。', metadata=result)
        if task["status"]=="completed":
            if task["mode"]=="approval_write":
                content=(task.get('summary',{}).get('final_response') or {}).get('message') or "本轮已完成审批和验证。"
            elif task["mode"]=="plan_only":content="计划已准备好，本轮没有修改项目文件。"
            else:content="本轮处理完成，可以在当前对话继续提问。"
            self.save(task,"complete","complete",content,created_at=task.get("finished_at") or task["updated_at"])
        elif task["status"]=="failed" and task.get("error"):
            self.save(task,"failure_"+digest([task.get("failed_node"),task["error"]])[:16],"error",task["error"]["message"])
        elif task["status"]=="cancelled":
            cleanup = task.get('cancellation_cleanup') or {}
            self.save(task,"cancelled","cancelled","本轮执行已停止。"+(' '+cleanup['reason'] if cleanup.get('verified') is False else ''))

    def detail(self,actor,session_id,*,before=None,limit=100,allow_inactive=False):
        row=self.session(actor,session_id,allow_inactive=allow_inactive)
        tasks=self.tasks(row)
        for task in tasks:self.sync_task(task)
        ids={t["id"] for t in tasks}
        messages=sorted((m for m in self.store.list("message",project_id=row["project_id"]) if m.get("task_id") in ids),key=lambda m:(m["created_at"],m["id"]))
        if before:
            index=next((i for i,m in enumerate(messages) if m["id"]==before),None)
            if index is None:raise PlatformError("message_cursor","消息位置已失效，请重新加载。",400)
            messages=messages[:index]
        has_more=len(messages)>limit
        # Full tool/model records stay behind the task detail endpoint.
        public_tasks=[]
        for task in tasks:
            public={k:task.get(k) for k in ("id","user_input","status","current_node","mode","requested_mode","engine","created_at","updated_at","approval_id","summary","error","usage","verification","ai_check","review","answer","database_required","delivery","database_operation","database_result","database_only","context_usage","approval_renewal")}
            if task.get('approval_id'):
                approval = self.store.get('approval', task['approval_id'])
                public['approval'] = {key: approval.get(key) for key in ('id', 'kind', 'status', 'subject_hash', 'expires_epoch')}
                public['approval']['expired'] = approval['expires_epoch'] < time.time()
                public['approval']['valid_for_seconds'] = self.platform.settings.approval_seconds
                public['approval']['decision_revision'] = approval.get('decision_revision', 0)
            if task.get('plan_id') and (task['status']=='awaiting_plan' or task['mode']=='plan_only' and task['status']=='completed'):
                plan=self.store.get('plan',task['plan_id'])
                public['plan']={k:plan.get(k) for k in ('id','hash','goal','steps','files','test_plan','risks','rollback_plan','database_deletion')}
            public_tasks.append(public)
        return {"session":self.describe(row,tasks),"messages":messages[-limit:],"has_more":has_more,"tasks":public_tasks}

    def context(self,task):
        from .conversation_memory import build_memory, episode
        current = self.store.get('task', task['id'])
        if current.get('conversation_context'):
            cached = current['conversation_context']
            if 'memory_request_task_ids' not in cached:
                memory = cached.get('memory', {})
                cached['memory_request_task_ids'] = list(dict.fromkeys(
                    ([memory['initial_goal']['task_id']] if memory.get('initial_goal') else [])+
                    [item['task_id'] for item in memory.get('user_constraints', [])]))
                self.store.mutate('task', task['id'], lambda t: {**t, 'conversation_context': cached})
            return cached
        history = self.predecessors(current)
        episodes = [episode(t) for t in history]
        memory = build_memory(episodes)
        if episodes:
            archive = self.platform.gateway.archive_context_material(task,
                '\n'.join(json.dumps(item, ensure_ascii=False) for item in episodes), {'kind': 'conversation_history'})
            memory['history_archive'] = {'artifact_id': archive['id'], 'read_with': 'read_tool_result',
                                         'format': 'jsonl', 'turn_count': len(episodes)}
        tasks=history[-4:]
        messages=[]
        for previous in tasks:
            messages.append({"role":"user","content":previous["user_input"], 'task_id': previous['id']})
            reply=(previous.get("answer") or {}).get("answer") or (previous.get("review") or {}).get("summary") or (previous.get("error") or {}).get("message") or previous["status"]
            messages.append({"role":"assistant","content":reply[:1500],"task_status":previous["status"],"applied_to_project":previous.get("summary",{}).get("applied_to_project",False)})
        context = {"recent_messages":messages,"prior_task_ids":[t["id"] for t in tasks],
                   "previous_task_summary":tasks[-1].get("summary",{}) if tasks else {}, 'memory': memory,
                   "note":"历史对话用于理解需求；当前代码事实仍需重新读取并使用本轮 source_id。重试使用首次检索时的历史快照，不混入后续轮次。"}
        context['memory_request_task_ids'] = list(dict.fromkeys(
            ([memory['initial_goal']['task_id']] if memory['initial_goal'] else [])+
            [item['task_id'] for item in memory['user_constraints']]))
        self.store.mutate('task', task['id'], lambda t: {**t, 'conversation_context': context})
        self.store.event(task['id'], 'ConversationMemoryPrepared', {'history_turns': len(history),
            'recent_turns': len(tasks), 'older_turns': len(history)-len(tasks), 'snapshot_retained_on_retry': True})
        return context
