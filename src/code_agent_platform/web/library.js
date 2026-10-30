'use strict';
let libraryTab='projects',libraryData=null,libraryPage=null,libraryMenu=null,libraryAnchor=null,libraryDelete=null;
const recordName=(kind,row)=>kind==='project'?row.name:(row.title||'新对话');

function recordMenuButton(kind,row){
  const control=button('⋯',()=>openRecordMenu(kind,row,control),'icon-button record-menu-button');
  control.setAttribute('aria-label',(kind==='project'?'项目操作：':'对话操作：')+recordName(kind,row));
  control.setAttribute('aria-haspopup','menu');return control;
}
function closeRecordMenu(){if(libraryAnchor)libraryAnchor.setAttribute('aria-expanded','false');libraryMenu?.remove();libraryMenu=null;libraryAnchor=null}
function openRecordMenu(kind,row,anchor){
  const same=libraryAnchor===anchor;closeRecordMenu();if(same)return;
  libraryAnchor=anchor;anchor.setAttribute('aria-expanded','true');
  libraryMenu=node('div',undefined,'record-action-menu');libraryMenu.setAttribute('role','menu');
  for(const [action,label]of [['archive','归档'],['delete','删除']]){
    const item=button(label+(kind==='project'?'项目':'对话'),()=>{closeRecordMenu();if(action==='delete')confirmRecordDelete(kind,row);else changeLibraryRecord(kind,row,'archive')},action==='delete'?'danger':'quiet');
    item.setAttribute('role','menuitem');libraryMenu.append(item);
  }
  document.body.append(libraryMenu);const rect=anchor.getBoundingClientRect();
  libraryMenu.style.left=Math.min(rect.right-150,window.innerWidth-160)+'px';
  libraryMenu.style.top=Math.min(rect.bottom+4,window.innerHeight-105)+'px';
  libraryMenu.querySelector('button').focus();
}
function libraryNotice(message,isError=false){
  if(isError&&$('library-delete-dialog').open){localError('library-delete-error',message);return}
  if($('library-dialog').open){localError('library-error',isError?message:'');if(isError)return}
  const toast=$('library-toast');toast.textContent=message;toast.classList.remove('hidden');
  clearTimeout(libraryNotice.timer);libraryNotice.timer=setTimeout(()=>toast.classList.add('hidden'),4500);
}
async function refreshLibraryWorkspace(){
  const previousProject=project?.id,previousSession=sessionId;rememberDraft();
  await loadProjects();
  const next=projects.find(p=>p.id===previousProject)||projects[0];
  if(next){await selectProject(next);const same=(sessionMap.get(next.id)||[]).find(s=>s.id===previousSession);if(same&&sessionId!==same.id)await selectSession(same.id,next)}
  else{project=null;sessionId=null;conversationData=null;selectedTaskId=null;renderKey='';drawerKey='';$('breadcrumb').textContent='工作空间';$('chat-title').textContent='新对话';$('prompt').value='';$('inspector').classList.add('hidden');renderSidebar();renderWelcome();updateComposer()}
}
async function changeLibraryRecord(kind,row,action){
  try{
    const updated=await request('/api/library/'+kind+'/'+encodeURIComponent(row.id)+'/'+action,{method:'POST'});
    await refreshLibraryWorkspace();
    if(action==='restore'&&updated.lifecycle==='active'){
      const parent=projects.find(p=>p.id===(kind==='project'?row.id:row.project_id));
      if(parent){if(kind==='project')await selectProject(parent);else await selectSession(row.id,parent);$('library-dialog').close()}
      else if($('library-dialog').open)await loadLibrary();
    }else if($('library-dialog').open)await loadLibrary();
    libraryNotice(action==='archive'?'已归档，可在左下角“归档”查看。':action==='delete'?'已移到回收站，可恢复。':updated.lifecycle==='archived'?'已恢复到归档。':'已恢复。');
    return updated;
  }catch(e){libraryNotice(e.message,true);return null}
}
function confirmRecordDelete(kind,row){
  libraryDelete={kind,row};$('library-delete-title').textContent='删除'+(kind==='project'?'项目':'对话')+'“'+recordName(kind,row)+'”？';
  $('library-delete-description').textContent=kind==='project'?'将项目及其对话从工作台列表移到回收站，可恢复。电脑上的项目文件夹和数据库会保留。':'将此对话移到回收站，可恢复；项目文件和数据库会保留。';
  $('library-delete-error').classList.add('hidden');$('library-delete-dialog').showModal();
}
async function openLibrary(){closeRecordMenu();libraryPage=null;$('library-dialog').showModal();await loadLibrary()}
async function loadLibrary(){
  try{localError('library-error','');const [archived,trash]=await Promise.all([request('/api/library?view=archived'),request('/api/library?view=trash')]);libraryData={archived,trash};libraryPage=null;renderLibrary()}
  catch(e){localError('library-error',e.message)}
}
function libraryAction(label,handler,cls='secondary'){return button(label,handler,cls)}
function libraryRow(kind,row,trash=false){
  const item=node('article',undefined,'library-row');
  const body=node('div',undefined,'library-row-main');body.append(button(recordName(kind,row),()=>kind==='project'?showLibraryProject(row.id):showLibrarySession(row.id),'library-row-title'));
  body.append(node('p',kind==='project'?row.root:'所属项目：'+(row.project_name||libraryPage?.data?.project?.name||project?.name||''),'muted path-text'));
  body.append(node('p',(kind==='project'?(row.session_count??0)+' 个对话 · ':'')+date((row.lifecycle==='deleted'?row.deleted_at:row.archived_at)||row.updated_at),'muted small'));
  const actions=node('div',undefined,'library-row-actions');
  actions.append(libraryAction(kind==='project'?'查看对话':'查看记录',()=>kind==='project'?showLibraryProject(row.id):showLibrarySession(row.id)));
  const parentDeleted=kind==='session'&&libraryPage?.data?.project?.lifecycle==='deleted';
  const restore=libraryAction(trash&&row.deleted_from==='archived'?'恢复到归档':'恢复',()=>changeLibraryRecord(kind,row,'restore'));
  if(parentDeleted){restore.disabled=true;restore.title='先恢复所属项目'}
  if(row.lifecycle!=='active')actions.append(restore);
  if(!trash)actions.append(libraryAction('删除',()=>confirmRecordDelete(kind,row),'quiet danger'));
  item.append(body,actions);return item;
}
function renderLibrary(){
  const tabs=$('library-tabs');tabs.replaceChildren();
  for(const [key,label]of [['projects','归档项目'],['sessions','归档对话'],['trash','回收站']]){
    const control=button(label,()=>{libraryTab=key;libraryPage=null;renderLibrary()},'library-tab'+(libraryTab===key?' active':''));control.setAttribute('aria-pressed',String(libraryTab===key));tabs.append(control);
  }
  const content=$('library-content');content.replaceChildren();if(!libraryData)return;
  if(libraryPage?.kind==='project'){renderLibraryProject(content);return}
  if(libraryPage?.kind==='session'){renderLibrarySession(content);return}
  const term=$('library-search').value.trim().toLowerCase();
  const rows=libraryTab==='projects'?libraryData.archived.projects:libraryTab==='sessions'?libraryData.archived.sessions:[...libraryData.trash.projects,...libraryData.trash.sessions];
  const matches=rows.filter(row=>[row.name,row.title,row.root,row.project_name].some(value=>String(value||'').toLowerCase().includes(term)));
  if(!matches.length)content.append(node('p',term?'没有匹配的项目或对话。':libraryTab==='trash'?'回收站为空。':libraryTab==='projects'?'暂无归档项目。':'暂无归档对话。','library-empty'));
  for(const row of matches)content.append(libraryRow(row.kind,row,libraryTab==='trash'));
}
async function showLibraryProject(id){
  try{const data=await request('/api/library/projects/'+id);libraryPage={kind:'project',data};renderLibrary()}catch(e){localError('library-error',e.message)}
}
async function showLibrarySession(id){
  try{const data=await request('/api/library/sessions/'+id);libraryPage={kind:'session',data,parent:libraryPage?.kind==='project'?libraryPage.data.project.id:null};renderLibrary()}catch(e){localError('library-error',e.message)}
}
function libraryBack(content){content.append(button('← 返回',()=>{const parent=libraryPage?.parent;if(parent)showLibraryProject(parent);else{libraryPage=null;renderLibrary()}},'inline-link library-back'))}
function renderLibraryProject(content){
  const {project:record,sessions}=libraryPage.data;libraryBack(content);
  content.append(node('h3',record.name),node('p',record.root,'muted path-text'));
  if(record.lifecycle!=='active')content.append(libraryAction('恢复项目',()=>changeLibraryRecord('project',record,'restore')));
  content.append(node('p','项目对话记录（只读查看，恢复后可继续）','muted field'));
  if(!sessions.length)content.append(node('p','这个项目还没有对话。','library-empty'));
  for(const row of sessions)content.append(libraryRow('session',{...row,project_name:record.name},row.lifecycle==='deleted'));
}
function renderLibrarySession(content){
  const data=libraryPage.data;libraryBack(content);content.append(node('h3',data.session.title),node('p','所属项目：'+data.project.name+' · 记录只读；恢复后可继续对话。','muted field'));
  if(data.project.lifecycle!=='active')content.append(libraryAction('先恢复所属项目',()=>changeLibraryRecord('project',data.project,'restore')));
  else if(data.session.lifecycle&&data.session.lifecycle!=='active')content.append(libraryAction('恢复对话',()=>changeLibraryRecord('session',data.session,'restore')));
  if(data.has_more)content.append(libraryAction('加载更早的记录',async()=>{try{const older=await request('/api/library/sessions/'+data.session.id+'?before='+encodeURIComponent(data.messages[0].id));data.messages=[...older.messages,...data.messages];data.has_more=older.has_more;renderLibrary()}catch(e){localError('library-error',e.message)}}));
  const transcript=node('div',undefined,'library-transcript scroll-area');
  for(const m of data.messages.filter(m=>['user','answer','complete'].includes(m.kind))){const box=node('article',undefined,'library-message'+(m.role==='user'?' user':''));box.append(node('strong',m.role==='user'?'你':'助手'),richText(m.content));transcript.append(box)}
  const process=data.messages.filter(m=>!['user','answer','complete'].includes(m.kind));
  if(process.length){const details=node('details');details.append(node('summary','处理记录（'+process.length+' 条）'));for(const m of process)details.append(node('p',m.kind+' · '+date(m.created_at),'muted field'),richText(m.content));transcript.append(details)}
  if(!data.messages.length)transcript.append(node('p','这个对话还没有消息。','muted'));content.append(transcript);
}
function initLibrary(){
  const entry=button('▣ 归档',openLibrary,'quiet archive-entry');entry.id='open-library';entry.setAttribute('aria-label','打开归档与回收站');document.querySelector('.sidebar-bottom').prepend(entry);
  const dialog=node('dialog',undefined,'library-dialog');dialog.id='library-dialog';dialog.setAttribute('aria-label','归档与回收站');
  const header=node('div',undefined,'dialog-heading'),close=button('×',()=>dialog.close(),'icon-button');close.setAttribute('aria-label','关闭归档与回收站');header.append(node('h2','归档与回收站'),close);dialog.append(header);
  const tabs=node('nav',undefined,'library-tabs');tabs.id='library-tabs';tabs.setAttribute('aria-label','归档分类');dialog.append(tabs);
  const search=node('input');search.id='library-search';search.placeholder='搜索项目或对话';search.setAttribute('aria-label','搜索归档与回收站');search.oninput=()=>{libraryPage=null;renderLibrary()};dialog.append(search);
  const err=node('p',undefined,'error hidden');err.id='library-error';err.setAttribute('role','alert');dialog.append(err);
  const content=node('div',undefined,'library-content scroll-area');content.id='library-content';dialog.append(content);document.body.append(dialog);
  const deletion=node('dialog');deletion.id='library-delete-dialog';const title=node('h2');title.id='library-delete-title';const description=node('p',undefined,'muted field');description.id='library-delete-description';
  const actions=node('div',undefined,'phase-actions');actions.append(button('取消',()=>deletion.close(),'secondary'),button('移到回收站',async()=>{if(!libraryDelete)return;const {kind,row}=libraryDelete;const result=await changeLibraryRecord(kind,row,'delete');if(result){deletion.close();libraryDelete=null}},'primary danger'));
  const deleteError=node('p',undefined,'error hidden');deleteError.id='library-delete-error';deletion.append(title,description,actions,deleteError);document.body.append(deletion);
  const toast=node('div',undefined,'library-toast hidden');toast.id='library-toast';toast.setAttribute('role','status');document.body.append(toast);
  document.addEventListener('click',event=>{if(libraryMenu&&!libraryMenu.contains(event.target)&&!libraryAnchor?.contains(event.target))closeRecordMenu()});
  document.addEventListener('keydown',event=>{if(event.key==='Escape')closeRecordMenu()});
}
