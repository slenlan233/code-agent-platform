const toolStatusText={available:'已接入',approval_required:'已接入 · 需计划审批',connected:'MCP 连接正常',disabled:'未启用',not_selected:'当前项目未选择',not_checked:'尚未检查',unavailable:'连接失败'};
function renderToolInventory(data){
  const box=card('工具与服务');
  const native=data.native_tools||[];
  if(native.length){
    box.append(node('h3','内置工具'));
    const reads=native.filter(item=>item.kind==='readonly');
    if(reads.length)box.append(node('p',reads.map(item=>item.label).join('、')+' · 已接入','muted field'));
    for(const item of native.filter(item=>item.kind!=='readonly')){
      const line=node('div',undefined,'evidence');
      line.append(node('strong',item.label),node('p',toolStatusText[item.status]||item.status,'muted'),node('p',item.name+(item.backend==='docker'?' · 隔离环境执行':''),'muted small'));
      box.append(line);
    }
  }
  box.append(node('h3','MCP 服务','field'));
  const labels={project_files:'项目文件',mysql_readonly:'MySQL 只读查询',bailian:'百炼知识库'};
  for(const item of data.servers||[]){
    const line=node('div',undefined,'evidence');
    line.append(node('strong',labels[item.server]||item.server),node('p',(toolStatusText[item.status]||item.status)+' · '+item.transport,'muted'));
    if(item.connection){
      const c=item.connection;
      line.append(node('p','目标：'+c.host+':'+c.port+' · 用户：'+c.user,'small'),node('p','数据库：'+(c.databases.join('、')||'未配置')+'；密码：'+(c.password_configured?'已配置':'未配置'),'muted'));
      const dbLabels={connected:'数据库实际查询成功',not_configured:'数据库连接信息未配齐',not_checked:'尚未验证数据库查询',unavailable:'数据库查询失败'};
      line.append(node('p',(dbLabels[item.database_status]||'尚未验证数据库查询')+(item.database_checked?'（'+item.database_checked+'）':''),'muted'));
    }
    if(!item.enabled)line.append(node('p','管理员尚未启用此服务。','muted'));
    if(!item.selected)line.append(node('p','当前项目未授权使用此服务。','muted'));
    const names=item.available_tools||item.tools||[];
    if(names.length){const more=node('details',undefined,'field');more.append(node('summary','查看 '+names.length+' 项工具名称'),pre(names.join('\n')));line.append(more)}
    box.append(line);
  }
  box.append(node('p','内置工具与 MCP 服务均在此列出。PowerShell 通过平台执行，不是 MCP 文件服务的一部分。MCP 已连通也不代表数据库查询已通过。','muted field'));
  return box;
}

function tokenValue(value){return value===null||value===undefined?'未记录':Number(value).toLocaleString('zh-CN')}
function usageFacts(task){
  const u=task.usage;
  const cached=u.model_calls===0?0:u.cache_read_tokens;
  return [['工具调用',u.tool_calls],['输入 token',tokenValue(u.input_tokens)],['缓存命中 token',tokenValue(cached)],['输出 token',tokenValue(u.output_tokens)],['累计 token',tokenValue(u.input_tokens+u.output_tokens)],['验证结果',statusLabels[task.verification?.status||'not_run']||task.verification?.status]];
}
function renderTokenDetails(detail){
  const box=card('Token 用量明细');
  box.append(node('p','输入 token 已包含缓存命中部分；累计 token = 输入 + 输出，缓存不再重复相加。','muted'));
  const budget=detail.task.budget;
  const context=detail.task.context_usage;
  if(context)box.append(node('p','最近一次请求上下文（估算）：'+tokenValue(context.before_estimated_tokens)+' → '+tokenValue(context.after_estimated_tokens)+' token；窗口 '+tokenValue(context.context_window_tokens)+'。工具结果已归档 '+context.archived_results+' 项，历史已摘要 '+context.summarized_messages+' 条。此处与累计用量分别统计。','muted field'));
  if(context?.reserved_output_tokens)box.append(node('p','最近一次请求的单次输出上限：'+tokenValue(context.reserved_output_tokens)+' token；这是上限，实际输出另行统计。','muted field'));
  box.append(node('p','本轮额度：模型 '+budget.max_model_calls+' 次 · 工具 '+budget.max_tool_calls+' 次 · 工作流 '+(budget.max_graph_steps??60)+' 步 · 执行 '+(budget.max_seconds/60)+' 分钟','muted field'));
  const calls=detail.model_calls||[];
  if(!calls.length){box.append(node('p','本任务没有模型调用记录。','muted field'));return box}
  const wrap=node('div',undefined,'token-table');const table=node('table');const header=node('tr');
  for(const title of ['阶段','输入','缓存命中','未缓存输入','输出','合计'])header.append(node('th',title));
  const thead=node('thead');thead.append(header);table.append(thead);const body=node('tbody');
  const roles={context_agent:'分析',planner_agent:'规划',coding_agent:'编码',conversation_agent:'对话',review_agent:'AI 检查',summary_agent:'最终答复',context_summarizer:'上下文摘要'};
  for(const [index,call]of calls.entries()){
    const row=node('tr');const total=call.input_tokens!==undefined&&call.output_tokens!==undefined?call.input_tokens+call.output_tokens:null;
    const values=[String(index+1)+' · '+(roles[call.agent_role]||call.node||'模型'),tokenValue(call.input_tokens),tokenValue(call.cache_read_tokens),tokenValue(call.uncached_input_tokens),tokenValue(call.output_tokens),tokenValue(total)];
    for(const value of values)row.append(node('td',value));body.append(row);
  }
  table.append(body);wrap.append(table);box.append(wrap);
  if(detail.task.usage.cache_read_tokens===null||detail.task.usage.cache_read_tokens===undefined)box.append(node('p','旧任务未保存缓存明细，或供应商未返回该字段时显示“未记录”，不能当作 0。新任务按供应商返回值记录。','muted field'));
  return box;
}

function renderProjectScope(item){
  const target=$('project-scope');target.replaceChildren();
  target.append(node('h2','当前项目与文件位置'),node('p','读取目录：'+item.root,'path-text field'),node('p','批准后写回目录：'+item.root,'path-text'),node('p',item.allow_write?'已允许审批后修改。生成的补丁先保存在任务记录，点击“写入所选项目”才会写回此目录。':'当前项目只读，可查看代码和生成计划。','muted field'));
  target.append(node('p',item.database_test_profile==='mysql'?'验证环境：Java / MySQL 集成测试；每次使用独立临时数据库，允许迁移和业务读写。':'验证环境：独立容器，默认不联网。','muted field'));
}

function renderDatabaseVerification(run,box){
  const db=run.database;
  if(db){
    box.append(node('h3','数据库与执行环境','field'),node('p','MySQL '+(db.server_version||'版本未就绪')+' · 临时数据库：'+db.schema,'small'),node('p','迁移账号：'+db.migration_user+'；业务读写账号：'+db.application_user,'muted'),node('p','专用网络仅供本次后端与数据库互通；数据库未向电脑发布端口。','muted'));
    const details=node('details',undefined,'field');details.append(node('summary','查看 Docker 容器名称'),pre({backend:db.application_container,mysql:db.container_name,network:db.network_name}));box.append(details);
    if(run.database_cleanup)box.append(node('p',Object.values(run.database_cleanup).every(Boolean)?'本次临时数据库和网络已清理。':'临时资源尚未全部清理，请检查详细报告。','muted field'));
  }
  if(run.test_report){
    const r=run.test_report;box.append(node('p','逐项测试：'+r.tests+' 项；失败 '+r.failures+'；错误 '+r.errors+'；跳过 '+r.skipped,'field'));
    const cases=node('ul',undefined,'list');for(const suite of r.suites||[])for(const c of suite.cases||[])cases.append(node('li',(c.status==='passed'?'通过':c.status==='skipped'?'跳过':'失败')+' · '+c.name));box.append(cases);
  }
}

let folderSelection=null;
let localFolderSelection=false;
let projectSelection=null;
async function browseProjectFolder(path=null){
  const data=await request('/api/project-folders'+(path?'?path='+encodeURIComponent(path):''));localError('folder-error','');error('');folderSelection=data.path;
  localFolderSelection=!!data.local_selection;
  $('folder-current').textContent=data.path||(localFolderSelection?'选择本机磁盘和项目文件夹':'选择已允许的项目目录');$('folder-location').value=data.path||'';
  $('folder-allowed').textContent=localFolderSelection?'仅将你最终选中的项目文件夹授权给 Agent；浏览磁盘不会授予整个磁盘的访问权限。':'当前允许范围：'+data.allowed_roots.join('、');
  $('folder-roots').textContent=localFolderSelection?'本机磁盘':'允许的目录';
  $('folder-up').disabled=!data.parent;$('folder-up').onclick=()=>browseProjectFolder(data.parent).catch(e=>localError('folder-error',e.message));
  $('folder-choose').disabled=!data.path||data.selectable===false;const list=$('folder-list');list.replaceChildren();
  for(const child of data.folders)list.append(button(child.name,()=>browseProjectFolder(child.path),'folder-item secondary'));
  if(!data.folders.length)list.append(node('p','此文件夹下没有可选的子文件夹。','muted'));
  if(data.limited)list.append(node('p','显示前 300 个文件夹，可输入具体路径继续浏览。','muted'));
}
async function openProjectFolderPicker(){await browseProjectFolder();$('project-folder-dialog').showModal()}
function initProjectFolderPicker(){
  $('pick-project-folder').onclick=()=>openProjectFolderPicker().catch(e=>localError('project-error',e.message));
  $('folder-close').onclick=()=>$('project-folder-dialog').close();
  $('folder-roots').onclick=()=>browseProjectFolder().catch(e=>localError('folder-error',e.message));
  $('folder-open').onclick=()=>browseProjectFolder($('folder-location').value).catch(e=>localError('folder-error',e.message));
  $('folder-choose').onclick=async()=>{if(!folderSelection)return;try{projectSelection=localFolderSelection?await request('/api/project-selections',{method:'POST',body:JSON.stringify({path:folderSelection})}):null;$('project-path').value=projectSelection?.root||folderSelection;if(!$('project-name').value)$('project-name').value=folderSelection.split(/[\\/]/).filter(Boolean).at(-1);$('project-folder-dialog').close()}catch(e){error(e.message)}};
  $('project-path').addEventListener('input',()=>{projectSelection=null});
}
