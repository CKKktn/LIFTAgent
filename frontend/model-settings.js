import {esc,toast,confirmAction} from './ui.js';

export const modelTypeLabels=Object.freeze({chat:'对话模型',vision:'视觉模型',embedding:'嵌入模型',rerank:'重排模型'});

export function setupModelSettings({refresh,canSave}){
 const byId=id=>document.getElementById(id),form=byId('modelConnectionForm');if(!form)return;
 let connections=[];
 async function request(path,options={}){const response=await fetch(path,options);const value=await response.json();if(response.status===401){location.assign('/login');throw Error('请先登录');}if(!response.ok)throw Error(value.message||'请求失败');return value;}
 async function load(){
  try{const settings=await request('/settings/models');connections=settings.models;byId('modelConnectionStatus').textContent=[settings.llm_configured?'对话模型已配置':'对话模型待配置',settings.vision_configured?'视觉模型已配置':'视觉模型待配置'].join(' · ');
  byId('configuredConnections').innerHTML=connections.map(model=>`<div class="connection-card"><div><strong>${esc(model.name)}</strong><small>${esc(modelTypeLabels[model.model_type]||model.model_type)} · ${esc(model.instance_name)}${model.read_only?' · 内置 / 环境配置':''}</small><small>${esc(model.base_url||'本机运行，无需模型密钥')}</small>${model.read_only?'':`<small>${model.api_key_set?'已保存密钥（不回显）':'未设置密钥'}</small>`}</div><div class="row">${model.read_only?'':`<button type="button" class="btn subtle small" data-connection-edit="${esc(model.id)}">编辑</button><button type="button" class="btn subtle small" data-connection-test="${esc(model.id)}">${model.model_type==='vision'?'测试看图连接':'测试连接'}</button><button type="button" class="btn danger small" data-connection-delete="${esc(model.id)}">删除</button>`}</div></div>`).join('');
  byId('configuredConnections').querySelectorAll('[data-connection-edit]').forEach(button=>button.onclick=()=>{const model=connections.find(item=>item.id===button.dataset.connectionEdit);byId('configModelId').value=model.id;byId('configModelName').value=model.name;byId('configModelType').value=model.model_type;byId('configModelType').disabled=true;byId('configModelType').title='已有接入的用途固定；如需其他用途，请新建接入';byId('configInstance').value=model.instance_name;byId('configBaseUrl').value=model.base_url;byId('configApiKey').value='';byId('configApiKey').placeholder=model.api_key_set?'留空保留已保存的密钥':'本地接口可留空';byId('configSetDefault').checked=false;syncPurpose();form.scrollIntoView({behavior:'smooth'});});
  byId('configuredConnections').querySelectorAll('[data-connection-test]').forEach(button=>button.onclick=async()=>{button.disabled=true;try{const value=await request('/settings/models/'+button.dataset.connectionTest+'/test',{method:'POST'});toast(value.message);}catch(e){toast(e.message,true);}finally{button.disabled=false;}});
  byId('configuredConnections').querySelectorAll('[data-connection-delete]').forEach(button=>button.onclick=()=>confirmAction('删除模型接入','删除这项本地模型配置？不会删除服务商上的模型。',async()=>{if(!canSave())throw Error('请先停止正在进行的审查或问答');await request('/settings/models/'+button.dataset.connectionDelete,{method:'DELETE'});await load();await refresh();toast('接入配置已删除');}));
  }catch(e){byId('modelConnectionStatus').textContent=e.message;}
 }
 function syncPurpose(){byId('configVisionHelp').hidden=byId('configModelType').value!=='vision';}
 byId('configModelType').onchange=syncPurpose;
 function reset(){form.reset();byId('configModelType').disabled=false;byId('configModelType').title='';byId('configModelId').value='';byId('configInstance').value='通用接口';byId('configApiKey').value='';byId('configApiKey').placeholder='输入服务商密钥';syncPurpose();}
 byId('resetModelConnection').onclick=reset;
 form.onsubmit=async event=>{event.preventDefault();const button=byId('saveModelConnection');button.disabled=true;
  try{if(!canSave())throw Error('请先停止正在进行的审查或问答');await request('/settings/models',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:byId('configModelId').value||undefined,name:byId('configModelName').value.trim(),model_type:byId('configModelType').value,instance_name:byId('configInstance').value.trim(),base_url:byId('configBaseUrl').value.trim(),api_key:byId('configApiKey').value,set_default:byId('configSetDefault').checked})});reset();await load();await refresh();toast('模型接入已保存');}catch(e){toast(e.message,true);}finally{button.disabled=false;}
 };
 window.addEventListener('hashchange',()=>{if(location.hash==='#models')load();});syncPurpose();load();
}
