'use strict';
const expandedTurns=new Set();
const phaseLabels={plan:'实施计划',patch:'文件改动',verification:'验证结果',review:'AI 代码检查',database:'数据库操作',error:'执行提示',notice:'处理提示',cancelled:'已停止'};
function turnIsActive(t){return activeStatuses.has(t.status)||t.status==='completed'&&['pending','writing'].includes(t.delivery?.status)}
function renderTurnMessages(force=false){
  const scroller=$('conversation'),stick=force||scroller.scrollHeight-scroller.scrollTop-scroller.clientHeight<110,position=scroller.scrollTop;
  const target=$('messages');target.replaceChildren();
  if(!conversationData.tasks.length){renderWelcome();return}
  if(conversationData.has_more)target.append(button('加载更早的消息',loadOlder,'load-older'));
  const visibleIds=new Set(conversationData.messages.map(m=>m.task_id));
  for(const t of conversationData.tasks){
    if(!visibleIds.has(t.id))continue;
    const messages=conversationData.messages.filter(m=>m.task_id===t.id);
    const user=node('article',undefined,'message user');user.dataset.message=t.id;
    user.append(node('div',t.user_input,'user-bubble'));
    const meta=node('div',undefined,'message-meta');meta.append(node('span',date(t.created_at)),button('本轮详情',()=>openInspector('events',t.id),'inline-link'));user.append(meta);target.append(user);
    const process=node('details',undefined,'turn-process');process.dataset.turn=t.id;
    const summary=node('summary'),active=turnIsActive(t),label=t.status==='completed'&&active?'正在保存项目文件':active?(nodeNames[t.current_node]||statusLabels[t.status]):'处理过程';
    summary.append(node('span',active?'◌':'✧','process-icon'+(active&&!t.status.startsWith('awaiting')?' spinning':'')),node('span',label,'process-label'));
    if(!active)summary.append(node('span',statusLabels[t.status]||t.status,'process-state'));
    summary.append(node('span','⌄','process-chevron'));process.append(summary);
    const body=node('div',undefined,'process-body');
    for(const m of messages.filter(m=>phaseLabels[m.kind])){
      const phase=node('div',undefined,'process-phase');phase.append(node('strong',phaseLabels[m.kind]),node('p',m.content));
      if(m.kind==='error'&&t.status!=='failed')phase.append(node('p','该中断已恢复，本轮正在继续或已经完成。','muted'));
      const panel={plan:'plan',patch:'diff',verification:'tests',review:'sources',database:'database',error:'events'}[m.kind];
      if(panel)phase.append(button({plan:'查看计划',patch:'查看修改',verification:'查看测试',review:'查看检查结果',database:'查看数据库',error:'查看原因'}[m.kind],()=>openInspector(panel,t.id),'inline-link'));
      body.append(phase);
    }
    const tools=node('div',undefined,'process-tools');body.append(tools);
    const links=node('div',undefined,'phase-actions');links.append(button('执行记录',()=>openInspector('events',t.id),'inline-link'),button('引用依据',()=>openInspector('sources',t.id),'inline-link'),button('Token 用量',()=>openInspector('usage',t.id),'inline-link'));body.append(links);
    process.append(body);process.open=expandedTurns.has(t.id);
    const loadTools=async()=>{try{const detail=await taskDetail(t.id);if(!tools.isConnected)return;tools.replaceChildren();if(detail.tool_calls.length){tools.append(node('strong','工具调用'));const list=node('div',undefined,'process-tool-list scroll-area');for(const call of detail.tool_calls){const row=node('div',undefined,'process-tool-row');row.append(node('span',call.name),node('span',({succeeded:'完成',failed:'失败',denied:'未允许',requested:'执行中'})[call.status]||call.status));list.append(row)}tools.append(list)}if(detail.approvals.length){const approvals=node('div',undefined,'process-approvals');for(const a of detail.approvals)approvals.append(node('p',(a.kind==='database'?'数据库删除确认':a.kind==='plan'?'实施范围确认':'人工改动确认')+' · '+({pending:'待处理',approved:'你已确认',rejected:'你已拒绝'})[a.status]));tools.append(approvals)}}catch(e){tools.replaceChildren(node('p',e.message,'muted'))}};
    process.addEventListener('toggle',()=>{if(process.open){expandedTurns.add(t.id);loadTools()}else expandedTurns.delete(t.id)});
    if(process.open)loadTools();target.append(process);
    if(['awaiting_plan','awaiting_diff','awaiting_database'].includes(t.status))target.append(approvalCard(t));
    if(t.status==='waiting_environment'){
      const wait=node('section',undefined,'phase-card message environment-card');wait.append(node('h3','需要准备验证环境'),node('p','本轮已暂停，环境就绪后可继续。'),button('查看验证报告',()=>openInspector('tests',t.id)),button('继续验证',()=>resumeTask(t.id),'primary'));target.append(wait);
    }
    if(['completed','failed','cancelled'].includes(t.status)){
      const reply=node('article',undefined,'message assistant final-reply'),heading=node('div',undefined,'assistant-label');heading.append(node('span','✳','spark'),node('span','助手'));reply.append(heading);
      const complete=messages.find(m=>m.kind==='complete');
      const text=t.status==='failed'?(t.error?.message||'本轮未完成，请查看执行记录。'):t.status==='cancelled'?'本轮已停止。':t.mode==='readonly'?(t.answer?.answer||messages.find(m=>m.kind==='answer')?.content||complete?.content):t.mode==='ci_review'?(t.review?.summary||complete?.content):t.summary?.final_response?.message||complete?.content||'本轮已完成。';
      const displayText=t.summary?.applied_to_project?String(text||'').split('\n').filter(line=>!line.includes('本次内容尚未写入项目文件')&&!line.includes('本轮内容尚未写入项目文件')).join('\n'):text;
      reply.append(richText(displayText));
      for(const next of t.summary?.final_response?.next_steps||[])reply.append(node('p',next,'muted field'));
      if(t.mode==='approval_write'&&t.status==='completed'&&!t.database_only){
        const writing=['pending','writing'].includes(t.delivery?.status);
        const state=node('p',t.summary?.applied_to_project?'✓ 修改已保存到当前项目文件夹。':writing?'正在保存到当前项目文件夹…':t.delivery?.status==='failed'?'保存未完成：'+t.delivery.error.message:'你已确认这份修改，尚未写回项目文件夹。','write-state'+(t.summary?.applied_to_project?' saved':''));
        reply.insertBefore(state,reply.children[1]||null);
        const actions=node('div',undefined,'phase-actions');actions.append(button('查看修改',()=>openInspector('diff',t.id)));
        if(!t.summary?.applied_to_project&&!writing)actions.append(button(t.delivery?.status==='failed'?'重试保存':'写入项目',()=>applyTask(t.id),'primary'));
        reply.append(actions);
      }else if(t.mode==='plan_only'&&t.status==='completed'){if(t.plan)reply.append(planPreview(t.plan));reply.append(button('查看完整计划',()=>openInspector('plan',t.id),'inline-link'))}
      if(t.status==='failed'){
        reply.append(button('查看原因',()=>openInspector('events',t.id),'inline-link'));
        if(['token_budget','project_token_budget'].includes(t.error?.code))reply.append(button('调整 Token 预算',()=>openInspector('usage',t.id),'inline-link'));
        else if(t.current_node!=='DONE'&&!['time_budget','model_budget','tool_budget','cost_budget'].includes(t.error?.code))reply.append(button('重试本轮',()=>resumeTask(t.id),'inline-link'));
      }
      target.append(reply);
    }
  }
  scroller.scrollTop=stick?scroller.scrollHeight:position;
}

function planPreview(plan){
  const preview=node('section',undefined,'plan-preview scroll-area');preview.setAttribute('aria-label','实施计划内容');
  preview.append(node('p',plan.goal,'plan-goal'),node('strong','实施步骤'));
  const steps=node('ol');for(const step of plan.steps||[])steps.append(node('li',step));preview.append(steps);
  if(plan.test_plan)preview.append(node('strong','验证方式'),node('p',plan.test_plan));
  if(plan.database_deletion)preview.append(databaseDeletionPreview(plan.database_deletion));
  const paths=node('details');paths.append(node('summary','涉及 '+(plan.files||[]).length+' 个文件'));const list=node('ul');for(const path of plan.files||[])list.append(node('li',path));paths.append(list);preview.append(paths);
  if(plan.risks?.length)preview.append(node('strong','风险'),node('p',plan.risks.join('\n')));
  return preview;
}

function humanApprovalCard(t){
  if(t.approval?.expired)return expiredApprovalCard(t);
  if(t.status==='awaiting_database')return databaseDeletionCard(t);
  const plan=t.status==='awaiting_plan',box=node('section',undefined,'phase-card approval-card message');
  const renewal=t.approval_renewal?.reason==='expired'&&(t.approval_renewal.subject_hash===t.approval?.subject_hash||plan&&t.approval_renewal.plan_hash===t.plan?.hash);
  box.append(node('div','需要你确认','approval-eyebrow'),node('h3',renewal?(plan?'原计划审批已过期':'原改动审批已过期'):plan?'确认实施范围':'等待你确认改动'),node('p',renewal?'原内容已重新核对，已生成的改动保留。请重新审批；刷新有效期不会自动同意。':plan?'确认计划、涉及的文件和必要操作后，开始生成与验证修改。':(t.verification?.verification_kind==='project_reset'?'文件清空验证':'实际测试')+'和 AI 代码检查已通过。接受后会直接将这份具体修改保存到项目文件夹。'));
  if(t.approval)box.append(node('p','本次确认页面有效至 '+date(t.approval.expires_epoch*1000)+'；批准后的执行授权有效期为 '+Math.ceil(t.approval.valid_for_seconds/60)+' 分钟。','muted field'));
  if(plan&&t.plan)box.append(planPreview(t.plan));
  if(plan&&t.database_required)box.append(node('p','本轮需要业务数据库：平台会沿用已有连接，或自动准备此项目专属的永久库和账号。'));
  const actions=node('div',undefined,'phase-actions');actions.append(button(plan?'查看计划':'查看修改',()=>openInspector(plan?'plan':'diff',t.id)),button(plan?'确认并开始':'接受并写入项目',()=>decide(t,'approved'),'primary'),button(plan?'暂不执行':'拒绝这份修改',()=>decide(t,'rejected'),'secondary'));box.append(actions);return box;
}

function expiredApprovalCard(t){
  const box=node('section',undefined,'phase-card approval-card message');
  box.append(node('div','需要重新审批','approval-eyebrow'),node('h3','上次审批时限已到，请重新审批'),node('p','原计划和已生成内容保留，尚未获得新的执行授权。先刷新并核对原内容，再重新确认。'));
  if(t.status==='awaiting_plan'&&t.plan)box.append(planPreview(t.plan));
  if(t.status==='awaiting_database'&&t.database_operation)box.append(databaseDeletionPreview(t.database_operation));
  const actions=node('div',undefined,'phase-actions');actions.append(button('刷新原审批内容',()=>renewApproval(t),'primary'));
  if(t.status==='awaiting_diff')actions.append(button('查看修改',()=>openInspector('diff',t.id)));
  box.append(actions);return box;
}

function databaseDeletionPreview(operation){
  const target=operation.target,box=node('section',undefined,'database-deletion-preview');
  box.append(node('strong','删除本项目专属数据库'),node('p','项目：'+target.project_name),node('p','实例：'+target.host+':'+target.port),node('p','数据库：'+target.database,'path-text'),node('p','范围：全部 '+target.table_count+' 个表及其中数据；两个项目专属账号、本地连接配置和保存的项目凭证。'));
  const tables=node('details');tables.append(node('summary','查看表清单（'+target.table_count+'）'));const list=node('ul',undefined,'database-table-list scroll-area');for(const name of target.tables)list.append(node('li',name));tables.append(list);box.append(tables,node('p','确认后立即执行。删库不属于文件补丁，不能通过文件回滚恢复数据库数据。','error field'));return box;
}

function databaseDeletionCard(t){
  const box=node('section',undefined,'phase-card approval-card message');
  const renewed=t.approval_renewal?.kind==='database'&&t.approval_renewal.subject_hash===t.approval?.subject_hash;
  box.append(node('div','需要你单独确认','approval-eyebrow'),node('h3',renewed?'原删库审批已过期，请重新确认':'确认删除项目数据库'),databaseDeletionPreview(t.database_operation));
  if(renewed)box.append(node('p','尚未执行删除，原目标已重新核对。刷新有效期不会自动批准。'));
  if(t.approval)box.append(node('p','本次确认页面有效至 '+date(t.approval.expires_epoch*1000)+'；批准后的执行授权有效期为 '+Math.ceil(t.approval.valid_for_seconds/60)+' 分钟。','muted field'));
  if(!t.database_only)box.append(node('p','这是本轮独立的数据库操作；文件改动仍有自己的检查和确认。','muted field'));
  const actions=node('div',undefined,'phase-actions');actions.append(button('确认删除这个数据库',()=>decide(t,'approved'),'primary'),button('取消删库',()=>decide(t,'rejected'),'secondary'));box.append(actions);return box;
}

async function beginDatabaseDeletion(){
  if(conversationData?.tasks.some(turnIsActive)){error('先处理当前轮次，再申请删除数据库。');return}
  if(!sessionId)await newChat(project);
  $('prompt').value='删除当前项目的业务数据库，包括全部表和数据，并清理本地连接配置。保留项目代码。请先展示具体库名和影响范围，等我确认后执行。';
  $('mode').value='auto';rememberDraft();$('inspector').classList.add('hidden');$('prompt').focus();
}

async function renderBusinessDatabase(content){
  const projectId=project.id;
  const [data,instance]=await Promise.all([request('/api/projects/'+projectId+'/database'),request('/api/mysql-instance').catch(()=>null)]);
  if(project?.id!==projectId||detailPanel!=='database')return;
  const state=data.state,connection=data.connection,box=card('项目业务数据库');
  const labels={deleted:'数据库已删除',deleting:'数据库删除清理中',ready:'永久数据库已就绪',external:'沿用项目已有连接',not_prepared:'需要时自动准备',failed:'数据库准备失败',config_changed:'本地配置已变更'};
  box.append(node('h3',labels[state.status]||state.status),node('p',state.status==='deleted'?'此项目原有的永久业务库已删除，删除记录已保留。':'Agent 会自动获得项目的连接约定，生成代码读取本地 .env。密码由后台写入，不进入提示词。','field'));
  if(state.database){box.append(node('p',(state.status==='deleted'?'已删除的库：':'数据库：')+state.database),node('p','实例：'+state.host+':'+state.port));if(state.status!=='deleted')box.append(node('p','业务账号：'+state.application_user+'（增删改查）'),node('p','迁移账号：'+state.migration_user+'（建表和迁移）'))}
  if(state.status!=='deleted')box.append(node('p','本地配置：'+project.root+'\\.env','path-text field'),node('p','此数据库用于项目日常运行，任务结束后保留。自动测试仍使用独立的临时测试库。','muted field'));
  if(state.error)box.append(node('p',state.error,'error field'));
  if(state.ready&&state.managed&&project.allow_write)box.append(button('在对话中申请删除此数据库',beginDatabaseDeletion,'secondary'));
  if(state.status==='deleted')box.append(node('p','专属账号和本地连接已清理；后续需要数据库时可重新准备，不会复用旧凭证。','muted field'));
  if(!state.ready&&state.status!=='deleting'&&project.allow_write&&instance?.configured)box.append(button('为此项目准备数据库',async()=>{await request('/api/projects/'+projectId+'/database/prepare',{method:'POST'});drawerKey='';await renderInspector(true)}));
  const more=node('details');more.append(node('summary','连接约定（不含密码）'),pre(connection));box.append(more);content.append(box);
  if(!instance)return;
  const global=card('本机 MySQL 实例');global.append(node('p',instance.configured?instance.host+':'+instance.port+' · MySQL '+instance.server_version:'尚未配置'),node('p',instance.configured?'连接已保存；凭证受本地用户加密保护。':'配置一次后，Java + MySQL 项目添加时自动准备数据库；其他项目在计划需要数据库时准备。','field'),node('p','MySQL 只读查询工具用于让 Agent 查看指定库内容，与业务程序的读写连接分别管理。','muted field'));
  global.append(button(instance.configured?'修改实例配置':'配置实例',()=>openMySQLConfig(instance)));content.append(global);
}

function openMySQLConfig(instance={}){
  const dialog=$('mysql-dialog');$('mysql-form').reset();$('mysql-host').value=instance.host||'127.0.0.1';$('mysql-port').value=instance.port||3306;$('mysql-user').value=instance.user||'';$('mysql-auto').checked=instance.auto_provision!==false;localError('mysql-error','');dialog.showModal();
}
function initMySQLConfig(){
  $('close-mysql').onclick=()=>$('mysql-dialog').close();
  $('mysql-form').onsubmit=async event=>{event.preventDefault();const submit=event.submitter;submit.disabled=true;try{localError('mysql-error','');await request('/api/mysql-instance',{method:'PUT',body:JSON.stringify({host:$('mysql-host').value,port:Number($('mysql-port').value),user:$('mysql-user').value,password:$('mysql-password').value,auto_provision:$('mysql-auto').checked})});$('mysql-password').value='';$('mysql-dialog').close();await renderInspector(true)}catch(e){localError('mysql-error',e.message)}finally{submit.disabled=false}};
  $('mysql-dialog').addEventListener('close',()=>{$('mysql-password').value=''});
}
