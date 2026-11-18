"""Final browser acceptance with synthetic files, account and local fake model."""
from pathlib import Path
import importlib.util
import sys

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'runtime-logs/browser-acceptance'
spec = importlib.util.spec_from_file_location('graph_browser_fixture', ROOT / 'tests/browser/quick_review_fixture.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
probe.OUT = OUT / ('narrow-dark-delete' if '--delete' in sys.argv else 'narrow-dark' if '--narrow' in sys.argv else 'desktop-light')
probe.JS = r'''
const result={errors:[]},$=id=>document.getElementById(id),wait=ms=>new Promise(r=>setTimeout(r,ms));
window.addEventListener('error',e=>result.errors.push(e.message));window.addEventListener('unhandledrejection',e=>result.errors.push(String(e.reason)));
const until=async(fn,label)=>{const end=performance.now()+16000;while(!await fn()){if(performance.now()>end)throw Error('Timed out: '+label);await wait(50);}};
const api=async(url,method='GET',body)=>{const r=await fetch(url,{method,headers:body?{'Content-Type':'application/json'}:undefined,body:body?JSON.stringify(body):undefined});if(!r.ok)throw Error(url+' '+r.status);return r.json();};
const change=(el,value)=>{el.value=value;el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));};
try{
 localStorage.clear();await api('/auth/login','POST',{username:'graph-browser',password:'synthetic-only-password'});await import('/app.js');
 await until(()=>$('quickModel').options.length>1&&$('assistantSelect').options.length>1,'app init');
 document.documentElement.dataset.theme=fixtureData.narrow?'dark':'light';
 location.hash='models';await until(()=>document.querySelector('[data-connection-edit]'),'models');
 document.querySelector('[data-connection-edit]').click();
 result.edit_type_fixed=$('configModelType').disabled&&$('configApiKey').value==='';
 $('modelConnectionForm').requestSubmit();await until(()=>!$('configModelId').value&&!$('saveModelConnection').disabled,'save model');
 result.new_model_type_editable=!$('configModelType').disabled;
 const settings=await api('/settings/models');result.blank_edit_preserves_key=settings.models.find(m=>m.model_type==='chat').api_key_set;
 document.querySelector('[data-connection-test]').click();await until(()=>$('toast').textContent.includes('连接成功'),'model connection');result.real_chat_connection_test=true;
 result.clear_default_hints=$('quickModel').options[0].textContent==='使用默认对话模型'&&$('chatModel').options[0].textContent.includes('（助手 / 默认）')&&$('chatModel').options[0].textContent.includes('synthetic-graph-model');
 location.hash='collab';await wait(130);$('chatInput').value='设备续航时间是多少？';$('chatForm').requestSubmit();
 await until(()=>document.querySelector('#chatMessages .message:last-child .reply-state')?.textContent.includes('回复完成'),'RAG answer');
 result.rag_answer=document.querySelector('#chatMessages .message:last-child').textContent.includes('10小时');
 await until(()=>!$('sendChat').disabled,'chat persisted');
 const assistants=(await api('/api/v1/chats')).data,assistant=assistants.find(a=>a.id===$('assistantSelect').value);
 const sessions=(await api('/api/v1/chats/'+assistant.id+'/sessions')).data;
 const session=(await api('/api/v1/chats/'+assistant.id+'/sessions/'+sessions[0].id)).data;
 result.chat_history_saved=session.messages.length===2&&session.messages[1].replyState==='complete';
 const realFetch=window.fetch.bind(window);window.fetch=(url,options)=>{
  if(String(url).includes('/proxy?')&&decodeURIComponent(String(url)).includes('/api/v1/chat/completions'))return Promise.resolve(new Response('data: {"code":0,"data":{"answer":"未完成的合成回答"}}\n\n',{headers:{'Content-Type':'text/event-stream'}}));return realFetch(url,options);
 };
 $('chatInput').value='合成断流测试';$('chatForm').requestSubmit();await until(()=>document.querySelector('#chatMessages .message:last-child .reply-state')?.textContent==='回复失败','stream loss');
 result.stream_loss_not_success=$('chatMessages').lastElementChild.textContent.includes('连接提前中断');window.fetch=realFetch;
 await until(()=>!$('sendChat').disabled,'stream reset');
 location.hash='quick';await wait(80);
 const verdicts=['已满足','部分满足','不满足','证据不足','待人工复核','证据冲突'],types=['numeric','functional','document'];
 const requirements=Array.from({length:13},(_,i)=>({id:'R'+String(i+1).padStart(3,'0'),type:types[i%3],category:['数值指标','功能条件','文档存在'][i%3],title:'合成核查条件'+(i+1),criterion:'设备续航时间不得低于8小时。',question:'项目方案是否符合本条要求？',quote:'设备续航时间不得低于8小时。',source:{document_id:fixtureData.document.id,document_name:fixtureData.document.name,content:'设备续航时间不得低于8小时。'}}));
 const results=requirements.map((r,i)=>({requirementId:r.id,status:verdicts[i%6],checkType:r.type,project_plan:'合成项目方案描述设备续航10小时。',analysis:'此处为软件验收用合成判定，不能用于真实项目验收。',gaps:'请人工核对适用条件和原文。',recommendation:'核实方案、测试与交付阶段。',evidence:[{source_id:'E1',kind:'背景',analysis:'要求基准，仅供比较。',quote:'设备续航时间不得低于8小时。',source:r.source}]}));
 const report={title:'交付前综合验收 · 合成样例',mode:'three-stage',scope:'dataset',kb:assistant.dataset_ids[0],kbName:'合成验收知识库',baselineDocs:[fixtureData.document],requirements,results,issues:[],complete:true,confirmed:true,createdAt:new Date().toISOString(),finishedAt:new Date().toISOString()};
 const record=await api('/review-records','POST',{report,status:'已完成'});
 $('reportHistory').click();await until(()=>document.querySelector('[data-record-open="'+record.id+'"]'),'saved report');document.querySelector('[data-record-open="'+record.id+'"]').click();
 await until(()=>!$('modal').open&&document.querySelector('[data-report-workspace]'),'report loaded');
 const root=$('reportBody'),tab=id=>root.querySelector('[data-rw-tab="'+id+'"]');
 result.thirteen_unique_items=root.querySelectorAll('[data-rw-item]').length===13;
 const visible=()=>[...root.querySelectorAll('[data-rw-item]')].filter(el=>!el.hidden&&getComputedStyle(el).display!=='none').length;
 tab('items').click();change(root.querySelector('[data-rw-status]'),'attention');result.attention_filter=visible()===10;
 change(root.querySelector('[data-rw-status]'),'all');change(root.querySelector('[data-rw-search]'),'条件13');result.search_filter=visible()===1;
 change(root.querySelector('[data-rw-search]'),'');change(root.querySelector('[data-rw-category]'),'文档存在');result.type_filter=visible()===4;
 change(root.querySelector('[data-rw-category]'),'all');tab('scope').click();result.scope_readable=root.querySelector('[data-rw-panel="scope"]').textContent.includes('技术协议');tab('overview').click();
 $('reportHistory').click();await until(()=>$('modal').open&&document.querySelector('[data-record-edit="'+record.id+'"]'),'record editor');document.querySelector('[data-record-edit="'+record.id+'"]').click();
 await until(()=>$('modalForm').querySelector('[name="analysis_0"]'),'edit form');$('modalForm').querySelector('[name="title"]').value='人工修订后的合成报告';$('modalForm').querySelector('[name="analysis_0"]').value='人工核对后的有效分析，仅用于软件测试。';$('modalSubmit').click();
 await until(()=>!$('modal').open,'edit saved');result.edit_refreshes_current_report=root.querySelector('h2').textContent==='人工修订后的合成报告';
 const blobs=new Map(),downloads=[],make=URL.createObjectURL.bind(URL),anchor=HTMLAnchorElement.prototype.click;
 URL.createObjectURL=blob=>{const url=make(blob);blobs.set(url,blob);return url;};HTMLAnchorElement.prototype.click=function(){if(this.download){downloads.push({name:this.download,blob:blobs.get(this.href)});return;}return anchor.call(this);};
 $('exportRawReview').click();$('exportReport').click();await until(()=>downloads.length===2,'two downloads');
 const raw=JSON.parse(await downloads.find(d=>d.name.endsWith('.json')).blob.text());
 result.full_json_export=raw.scope==='dataset'&&raw.kb===report.kb&&raw.requirements.length===13&&raw.results.length===13&&Array.isArray(raw.issues);
 result.export_uses_human_revision=raw.title==='人工修订后的合成报告'&&raw.results[0].analysis==='人工核对后的有效分析，仅用于软件测试。';
 const html=await downloads.find(d=>d.name.endsWith('.html')).blob.text(),doc=new DOMParser().parseFromString(html,'text/html');
 result.offline_html=doc.querySelectorAll('svg').length===4&&!doc.querySelector('script,iframe,object,embed,link[rel="stylesheet"]')&&[...doc.querySelectorAll('[src]')].every(el=>el.getAttribute('src').startsWith('data:'));
 result.all_export_anchors_valid=[...doc.querySelectorAll('a[href^="#"]')].every(a=>doc.getElementById(a.getAttribute('href').slice(1)));
 result.all_thirteen_exported=requirements.every(r=>html.includes(r.id));
 await api('/__probe/export','POST',{html,svg:'<svg xmlns="http://www.w3.org/2000/svg"/>'});
 let fits=true;
 for(const scale of ['.9','1','1.1','1.25']){
  document.documentElement.style.setProperty('--scale',scale);await wait(60);
  for(const button of document.querySelectorAll('.review-actions button:not([hidden]),.report-panel>.panel-heading button')){
   const outer=button.getBoundingClientRect();if(!outer.width)continue;const range=document.createRange();range.selectNodeContents(button);
   fits&&=[...range.getClientRects()].every(r=>r.left>=outer.left-1&&r.right<=outer.right+1&&r.top>=outer.top-1&&r.bottom<=outer.bottom+1);
  }
  fits&&=root.scrollWidth<=root.clientWidth+2;
 }
 result.button_text_and_report_fit=fits;document.documentElement.style.setProperty('--scale','1');
 if(fixtureDelete){
  $('reportHistory').click();await until(()=>$('modal').open&&document.querySelector('[data-record-delete="'+record.id+'"]'),'delete record');document.querySelector('[data-record-delete="'+record.id+'"]').click();await wait(30);$('modalSubmit').click();await until(()=>!$('modal').open,'delete accepted');
  result.deleted_report_cleared=root.textContent.includes('当前审查记录已删除')&&$('exportRawReview').disabled&&$('exportReport').disabled;
 }
 document.querySelector('.report-panel').scrollIntoView({block:'start',behavior:'instant'});await wait(250);
 const preview=document.querySelector('.report-panel').getBoundingClientRect();result.preview_in_view=preview.top>=-1&&preview.top<200&&preview.bottom>0;
 result.success=Object.entries(result).filter(([k])=>k!=='errors').every(([,v])=>v===true)&&!result.errors.length;
}catch(error){result.success=false;result.error=String(error.stack||error);result.toast=$('toast')?.textContent;}
const output=document.createElement('pre');output.id='graph-probe-result';output.hidden=true;output.textContent=JSON.stringify(result);document.body.append(output);
'''
probe.JS = 'const fixtureDelete=' + ('true' if '--delete' in sys.argv else 'false') + ';\n' + probe.JS

if __name__ == '__main__':
    probe.main()
