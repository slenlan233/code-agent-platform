'use strict';
function initTokenBudgets(){
  $('project-token-budget').onchange=()=>{const custom=$('project-token-budget').value==='custom';$('project-token-custom-label').classList.toggle('hidden',!custom);$('project-token-custom').disabled=!custom};
  $('project-token-budget').onchange();
}
function newProjectTokenBudget(){
  const value=$('project-token-budget').value;
  if(value==='unlimited')return null;
  const limit=Number(value==='custom'?$('project-token-custom').value:value);
  if(!Number.isSafeInteger(limit)||limit<100)throw Error('请输入不小于 100 的整数，或选择无上限。');
  return limit;
}
async function renderProjectTokenBudget(content){
  const id=project.id,t=currentTask();
  const data=await request('/api/projects/'+id+'/token-budget');
  if(detailPanel!=='usage'||project?.id!==id)return;
  const box=card('项目累计用量与预算');
  const facts=node('div',undefined,'facts');
  for(const [label,value]of [['项目总预算',data.unlimited?'无上限':data.limit],['项目累计已用',data.total_tokens],['剩余额度',data.unlimited?'无上限':data.remaining_tokens],['全部对话轮次',data.task_count]]){
    const fact=node('div',label,'fact');fact.append(node('strong',typeof value==='number'?value.toLocaleString('zh-CN'):value));facts.append(fact);
  }
  box.append(facts);
  if(!data.unlimited){const progress=node('progress',undefined,'token-progress');progress.max=data.limit;progress.value=Math.min(data.total_tokens,data.limit);progress.setAttribute('aria-label','项目 token 预算使用比例');box.append(progress)}
  box.append(node('p','范围：此项目的全部对话。累计用量 = 输入 + 输出；缓存命中已包含在输入中。','muted field'));
  if(data.usage_incomplete)box.append(node('p','部分请求已中断或未返回完整用量；这里仅累计已收到的 token，实际供应商计费可能更高。','error field'));
  if(data.exhausted)box.append(node('p','项目额度已用完，调整后可继续。','error field'));
  box.append(button('刷新项目用量',()=>renderInspector(true),'inline-link'));
  if(data.can_edit){
    const editor=node('form',undefined,'budget-editor form-stack field');
    const unlimitedLabel=node('label',undefined,'check-row'),unlimited=node('input');unlimited.type='checkbox';unlimited.checked=data.unlimited;unlimited.setAttribute('aria-label','项目 Token 无上限');unlimitedLabel.append(unlimited,node('span','项目 Token 无上限'));editor.append(unlimitedLabel);
    const label=node('label','自定义项目 Token 总预算');
    const input=node('input');input.type='number';input.min='100';input.max=String(Number.MAX_SAFE_INTEGER);input.step='1';input.required=!data.unlimited;input.disabled=data.unlimited;input.value=data.limit??2000000;input.setAttribute('aria-label','项目 Token 总预算');label.append(input);editor.append(label);
    unlimited.onchange=()=>{input.disabled=unlimited.checked;input.required=!unlimited.checked};
    const actions=node('div',undefined,'phase-actions');
    const save=async resume=>{
      const limit=unlimited.checked?null:Number(input.value);
      if(limit!==null&&(!Number.isSafeInteger(limit)||limit<100))throw Error('请输入不小于 100 的整数，或选择无上限。');
      if(resume&&limit!==null&&limit<=data.total_tokens)throw Error('继续执行需要把预算设为当前已用 token 以上，或选择无上限。');
      await request('/api/projects/'+id,{method:'PATCH',body:JSON.stringify({token_budget:limit})});
      project.token_budget=limit;
      if(resume){await request('/api/tasks/'+t.id+'/token-budget',{method:'PATCH',body:JSON.stringify({follow_project:true})});await resumeTask(t.id)}
      await renderInspector(true);
    };
    const submit=button('保存项目预算',()=>save(false));actions.append(submit);
    if(t?.status==='failed'&&['token_budget','project_token_budget'].includes(t.error?.code))actions.append(button('保存并继续本轮',()=>save(true),'primary'));
    editor.onsubmit=event=>{event.preventDefault();submit.click()};
    editor.append(actions,node('p','修改预算会保留已经消耗的用量、计划及测试记录。额度代表累计允许用量，不会一次性发送给模型。','muted'));
    box.append(editor);
  }
  content.append(box);
}
