import {buildReportHtml,reportHtmlFilename} from './report-export.js';
import {renderReportWorkspace,bindReportWorkspace} from './report-workspace.js';
import {setupModelSettings,modelTypeLabels} from './model-settings.js';
import {CollabJobClient,isCollabJobRunning,isCollabJobUnresolved,collabReplyLabel,collabWorkflowHtml,mergeCollabMessages} from './collab-jobs.js';
import {getDefaultChatModel} from './review-state.js';
import {ReviewJobClient,isReviewJobBusy,reviewJobAction,workflowGraphHtml,workflowGraphSvg,workflowOverviewSvg,workflowAcademicSvg,mountWorkflowInspector} from './review-jobs.js';
import {api,arrayOf,completion,proxyUrl,request,readSSE} from './api.js';
import {uploadSkipReason,uploadFileName,collectDocumentIds} from './upload-filter.js';
import {confirmedRequirements,requirementEditorHtml,mountRequirementEditor} from './three-stage-review.js';
import {openFile} from './viewer.js';
import {drawingEvidenceSettings,drawingFile} from './drawing-evidence.js';
import {reportMarkdown} from './review.js';
import {$,esc,toast,busy,empty,setOptions,fileIcon,date,download,loadLocal,saveLocal,answerHtml,sourcesHtml,modal,confirmAction,drawingEvidenceOf} from './ui.js';
const S={datasets:[],chats:[],models:[],defaults:[],kb:'',quickKb:'',docs:[],docPage:1,chunkPage:1,sessionPage:1,selected:new Set(),docTotal:0,selectionEpoch:0,selectingDocuments:false,baselineSelected:new Map(),doc:null,chunks:[],session:'',messages:[],report:'',reportMeta:'',page:'quick'};
const data=async(method,path,body,signal)=>(await api(method,path,body,signal)).data;
const ds=(id=S.kb)=>`/api/v1/datasets/${encodeURIComponent(id)}`;
const docPath=(id=S.doc?.id,kb=S.kb)=>`${ds(kb)}/documents/${encodeURIComponent(id)}`;
const chatPath=()=>`/api/v1/chats/${encodeURIComponent($('assistantSelect').value)}`;
const modelValue=m=>`${m.name}@${m.instance_name}@${m.provider_name}`;
const modelOptions=type=>S.models.filter(m=>(Array.isArray(m.model_type)?m.model_type:[m.model_type]).includes(type)).map(m=>({value:modelValue(m),label:`${m.name} · ${m.instance_name}`}));
function bind(id,fn){$(id).onclick=e=>busy(e.currentTarget,()=>fn(e));}
function page(name){if(!$(name)?.classList.contains('page'))name='quick';S.page=name;document.querySelectorAll('.page').forEach(e=>e.hidden=e.id!==name);document.querySelectorAll('[data-page]').forEach(e=>e.classList.toggle('active',e.dataset.page===name));document.querySelectorAll('[data-icon]').forEach(e=>e.src=`/assets/${e.dataset.icon}${e.dataset.icon===name?'-active':''}.png`);$('pageLabel').textContent={quick:'快速审查',collab:'人机协审',knowledge:'知识库',api:'接口调试',models:'模型设置',retrieval:'检索测试'}[name];}
document.querySelectorAll('[data-page]').forEach(b=>b.onclick=()=>location.hash=b.dataset.page);window.onhashchange=()=>page(location.hash.slice(1));page(location.hash.slice(1));
$('projectName').value=loadLocal('lift-project','合同与技术协议审查');$('projectName').onchange=e=>saveLocal('lift-project',e.target.value);
function theme(v){document.documentElement.dataset.theme=v;saveLocal('lift-theme',v);$('themeToggle').setAttribute('aria-label',v==='dark'?'切换浅色主题':'切换深色主题');}theme(loadLocal('lift-theme','dark'));$('themeToggle').onclick=()=>theme(document.documentElement.dataset.theme==='dark'?'light':'dark');
$('uiScale').value=loadLocal('lift-scale','1');function scale(){document.documentElement.style.setProperty('--scale',$('uiScale').value);saveLocal('lift-scale',$('uiScale').value);}scale();$('uiScale').onchange=scale;
function pager(prefix,p,total,size){$(prefix+'Page').textContent=`${p} / ${Math.max(1,Math.ceil(total/size))}`;$(prefix+'Prev').disabled=p<=1;$(prefix+'Next').disabled=p*size>=total;}
// Matches local v0.27.2 CreateDatasetReq; Audio is native-only for creation.
const chunkMethods=[['naive','General · 通用'],['qa','Q&A · 问答'],['manual','Manual · 手册'],['table','Table · 表格'],['paper','Paper · 论文'],['book','Book · 书籍'],['laws','Laws · 法规'],['presentation','Presentation · 演示文稿'],['picture','Picture · 图片'],['one','One · 整篇'],['email','Email · 邮件'],['tag','Tag · 标签'],['resume','Resume · 简历']];
const chunkLabel=value=>chunkMethods.find(m=>m[0]===value)?.[1]||({audio:'Audio · 音频',knowledge_graph:'Knowledge Graph · 知识图谱'}[value])||value||'—';
function setChunkDocument(doc=null,message='点击「选择已解析文件」查看切片'){
  S.doc=doc;
  S.chunks=[];
  S.chunkPage=1;
  $('chunkDocName').textContent=doc?.name||'';
  $('chunkDocName').hidden=!doc;
  $('addChunk').disabled=!doc;
  $('chunkList').innerHTML=empty(message);
  $('chunkSummary').textContent='';
  $('chunksPager').hidden=true;
  pager('chunks',1,0,10);
}
function resetKbView(){
  S.selectionEpoch++;
  S.docPage=1;
  S.selected.clear();
  $('chooseChunkDocument').disabled=!S.kb;
  setChunkDocument();
}
function kbActions(k){
  modal(k.name+' · 选项',`<div class="kb-action-list"><button type="button" id="renameKbAction" class="btn subtle">重命名</button><button type="button" id="deleteKbAction" class="btn danger">删除知识库</button></div>`);
  $('renameKbAction').onclick=()=>modal('重命名知识库',`<label for="renameKbName">名称</label><input id="renameKbName" name="name" required value="${esc(k.name)}">`,async f=>{const name=f.get('name').trim();if(!name)throw Error('名称不能为空');await data('PUT',ds(k.id),{name});await refreshKbs();toast('知识库已重命名');});
  $('deleteKbAction').onclick=()=>{if(S.reviewAbort||S.chatAbort){toast('请先停止当前审查或回答，再删除知识库',true);return;}modal('删除知识库',`<p>将永久删除「${esc(k.name)}」及其文档、切片，并解除助手关联。已有协审会话会保留。</p><label for="deleteKbName">输入知识库名称以确认</label><input id="deleteKbName" name="name" required autocomplete="off">`,async f=>{if(f.get('name')!==k.name)throw Error('名称不匹配，未执行删除');await data('DELETE','/api/v1/datasets',{ids:[k.id]});if(S.kb===k.id){S.kb='';resetKbView();}if(S.quickKb===k.id){S.quickKb='';S.baselineSelected.clear();}await refreshKbs();await Promise.all([loadDocs(),loadQuick(),refreshAssistantsAfterDelete()]);toast('知识库已删除');},'确认删除');};
}
function renderKbs(){for(const [id,search,active] of [['kbList','kbSearch',S.kb],['quickKbList','quickKbSearch',S.quickKb]]){$(id).innerHTML=S.datasets.filter(k=>k.name.toLowerCase().includes($(search).value.toLowerCase())).map(k=>`<div class="kb-row"><button class="kb-item ${k.id===active?'active':''}" data-kb="${esc(k.id)}"><span>▤</span><div><strong>${esc(k.name)}</strong><small>${k.document_count||0} 个文件 · ${k.chunk_count||0} 个切片</small></div>${k.id===active?'<b>✓</b>':''}</button>${id==='kbList'?`<button class="icon-btn kb-options" data-kb-options="${esc(k.id)}" aria-label="${esc(k.name)} 的选项" title="重命名或删除">⋯</button>`:''}</div>`).join('')||empty('没有匹配的知识库');$(id).querySelectorAll('[data-kb]').forEach(b=>b.onclick=()=>busy(b,async()=>{if(id==='quickKbList'){if(S.reviewAbort)throw Error('请先停止当前审查');S.quickKb=b.dataset.kb;S.baselineSelected.clear();await loadQuick();}else{S.kb=b.dataset.kb;resetKbView();await loadDocs();}renderKbs();}));$(id).querySelectorAll('[data-kb-options]').forEach(b=>b.onclick=()=>kbActions(S.datasets.find(k=>k.id===b.dataset.kbOptions)));}const k=S.datasets.find(k=>k.id===S.kb);$('kbMeta').innerHTML=k?`<strong>${esc(k.name)}</strong><p>${esc(k.description||'暂无描述')}</p><p>解析方式：${esc(k.pipeline_id?'Pipeline · 流水线':chunkLabel(k.chunk_method))}</p><p>嵌入模型：${esc(k.embedding_model||'默认')}</p>`:'';$('quickKbCount').textContent=S.datasets.length;$('quickTarget').textContent=`上传至：${S.datasets.find(k=>k.id===S.quickKb)?.name||'请先创建知识库'}`;}
$('kbSearch').oninput=$('quickKbSearch').oninput=renderKbs;
async function refreshKbs(){S.datasets=[];for(let p=1;;p++){const rows=arrayOf(await data('GET',`/api/v1/datasets?page=${p}&page_size=100`));S.datasets.push(...rows);if(rows.length<100)break;}if(!S.datasets.some(k=>k.id===S.kb)){S.kb=S.datasets[0]?.id||'';resetKbView();}S.quickKb=S.datasets.some(k=>k.id===S.quickKb)?S.quickKb:S.kb;renderKbs();setOptions($('retrievalKb'),S.datasets.map(k=>({value:k.id,label:k.name})),'选择知识库');if(!$('retrievalKb').value)$('retrievalKb').value=S.kb;}
function status(d){return {DONE:'完成解析',RUNNING:'正在解析',UNSTART:'未解析',FAIL:'解析失败',CANCEL:'已取消','0':'未解析','1':'正在解析','2':'已取消','3':'完成解析','4':'解析失败'}[d.run]||d.run||'未解析';}
function renderBaseline(){
  $('baselineFiles').innerHTML=[...S.baselineSelected.values()].map(d=>`<div class="file-item">${fileIcon(d.name)}<div><strong>${esc(d.name)}</strong><small>审查依据 · 不作为完成证明</small></div><button class="icon-btn" data-remove-baseline="${esc(d.id)}" aria-label="移除 ${esc(d.name)}">×</button></div>`).join('')||'<p class="hint">尚未选择合同或技术协议</p>';
  $('baselineFiles').querySelectorAll('[data-remove-baseline]').forEach(b=>b.onclick=()=>busy(b,async()=>{if(S.reviewAbort)throw Error('请先停止审查');S.baselineSelected.delete(b.dataset.removeBaseline);await loadQuick();}));
}
async function allKnowledgeDocs(kb){
  const docs=[];
  for(let page=1;;page++){
    const rows=arrayOf(await data('GET',`${ds(kb)}/documents?page=${page}&page_size=100`));
    docs.push(...rows);
    if(rows.length<100)return docs;
  }
}
bind('chooseChunkDocument',async()=>{
  const kb=S.kb;
  if(!kb)return;
  modal('选择已解析文件',`<div class="chunk-file-picker">
    <label for="chunkFileSearch">文件名</label>
    <input id="chunkFileSearch" type="search" placeholder="搜索当前知识库的文件" autofocus>
    <div id="chunkFileOptions" class="chunk-file-options">${empty('正在读取文件列表…')}</div>
  </div>`);
  const search=$('chunkFileSearch'),options=$('chunkFileOptions'),dialog=$('modal');
  const active=()=>options.isConnected&&dialog.open&&S.kb===kb;
  search.onkeydown=event=>{if(event.key==='Enter')event.preventDefault();};
  let docs;
  try{
    docs=(await allKnowledgeDocs(kb)).filter(doc=>['DONE','3'].includes(String(doc.run)));
  }catch(error){
    if(active())options.innerHTML=empty('读取文件失败：'+error.message);
    return;
  }
  if(!active())return;
  function render(){
    const keyword=search.value.trim().toLowerCase();
    const matching=docs.filter(doc=>doc.name.toLowerCase().includes(keyword));
    options.innerHTML=matching.map(doc=>`<button type="button" class="file-item chunk-file-option" data-chunk-choice="${esc(doc.id)}"${doc.id===S.doc?.id?' aria-current="true"':''}>
      ${fileIcon(doc.name)}<span class="chunk-file-option-name">${esc(doc.name)}</span><small>${Number(doc.chunk_count)||0} 个切片</small>
    </button>`).join('')||empty(docs.length?'没有匹配的已解析文件':'当前知识库暂无已解析文件');
    options.scrollTop=0;
  }
  search.oninput=render;
  options.onclick=event=>{
    const choice=event.target.closest('[data-chunk-choice]');
    if(!choice||!active())return;
    const doc=docs.find(item=>item.id===choice.dataset.chunkChoice);
    if(!doc)return;
    dialog.close();
    setChunkDocument(doc,'正在加载切片…');
    void busy(null,loadChunks);
  };
  render();
});
bind('chooseBaseline',async()=>{if(S.reviewAbort)throw Error('请先停止审查');if(!S.quickKb)throw Error('请先选择知识库');const kb=S.quickKb,docs=await allKnowledgeDocs(kb);modal('选择合同 / 技术协议',`<p class="hint">所选文件用于提取要求，随后结合整个知识库检索问答，不将依据文件当成完成证明。</p><div class="check-list">${docs.map(d=>`<label>${fileIcon(d.name)}<input name="baseline" type="checkbox" value="${esc(d.id)}" ${S.baselineSelected.has(d.id)?'checked':''}>${esc(d.name)}</label>`).join('')||empty('知识库暂无文件，请先上传')}</div>`,async f=>{if(S.reviewAbort||S.quickKb!==kb)throw Error('审查范围已变更，请重新选择');S.baselineSelected=new Map(f.getAll('baseline').map(id=>[id,docs.find(d=>d.id===id)]));await loadQuick();},'使用所选文件');});
async function loadQuick(){
  renderBaseline();
  const k=S.datasets.find(k=>k.id===S.quickKb);
  $('quickScope').textContent=k?`审查范围：${k.name} 的全部已解析内容。合同和技术协议仅作为要求依据，不作为完成证明。`:'请先选择需要审查的知识库。';
}
function syncDocSelection(){
  const count=S.selected.size,all=S.docTotal>0&&count===S.docTotal;
  $('selectedDocCount').textContent=$('docsSummary').textContent=`已选 ${count} 项`;
  const header=$('selectAllDocs'),pageCount=S.docs.filter(doc=>S.selected.has(doc.id)).length;
  header.checked=S.docs.length>0&&pageCount===S.docs.length;
  header.indeterminate=pageCount>0&&pageCount<S.docs.length;
  $('docRows').querySelectorAll('[data-check]').forEach(box=>box.checked=S.selected.has(box.dataset.check));
  const button=$('selectAllDocumentResults');
  button.textContent=all?'取消全选':'全选';
  button.setAttribute('aria-pressed',String(all));
}
bind('selectAllDocumentResults',async()=>{
  if(!S.kb)throw Error('请先选择知识库');
  if(S.docTotal>0&&S.selected.size===S.docTotal){S.selectionEpoch++;S.selected.clear();syncDocSelection();return;}
  const kb=S.kb,q=$('docSearch').value,epoch=++S.selectionEpoch;
  $('selectAllDocumentResults').textContent='正在全选…';
  S.selectingDocuments=true;
  try{
    const ids=await collectDocumentIds(async page=>{
      const result=await data('GET',`${ds(kb)}/documents?page=${page}&page_size=100&keywords=${encodeURIComponent(q)}`);
      return {docs:arrayOf(result),total:result.total};
    },()=>S.kb===kb&&$('docSearch').value===q&&S.selectionEpoch===epoch);
    if(ids)S.selected=ids;
  }finally{S.selectingDocuments=false;syncDocSelection();}
});
async function loadDocs(){if(!S.kb){S.docs=[];$('docRows').innerHTML='<tr><td colspan="8">暂无知识库，请先创建</td></tr>';$('docTotal').textContent='0 个文件';$('selectedDocCount').textContent='已选 0 项';$('docsSummary').textContent='';$('selectAllDocs').checked=false;S.docTotal=0;syncDocSelection();pager('docs',1,0,20);return;}const id=S.kb,p=S.docPage,q=$('docSearch').value;const r=await data('GET',`${ds(id)}/documents?page=${p}&page_size=20&keywords=${encodeURIComponent(q)}`);if(id!==S.kb||p!==S.docPage||q!==$('docSearch').value)return;S.docs=arrayOf(r);$('docTotal').textContent=`${r.total||0} 个文件`;S.docTotal=r.total||0;syncDocSelection();$('docRows').innerHTML=S.docs.map(d=>`<tr><td><input type="checkbox" data-check="${esc(d.id)}" ${S.selected.has(d.id)?'checked':''} aria-label="选择 ${esc(d.name)}"></td><td><button class="doc-name" data-open="${esc(d.id)}">${fileIcon(d.name)}${esc(d.name)}</button></td><td>${esc(date(d.create_time||d.create_date))}</td><td><button class="toggle-dot ${String(d.status)!=='0'?'on':''}" data-toggle="${esc(d.id)}" aria-label="切换 ${esc(d.name)} 启用状态"></button></td><td>${d.chunk_count||0}</td><td>${esc(chunkLabel(d.chunk_method))}</td><td><span class="badge ${d.run==='DONE'?'done':d.run==='RUNNING'?'running':d.run==='FAIL'?'failed':''}">${esc(status(d))}</span></td><td><a class="icon-btn" href="${proxyUrl(docPath(d.id,id))}" download title="下载">↓</a>${drawingFile(d.name)?`<button type="button" class="btn subtle small" data-file-source="${esc(JSON.stringify({document_id:d.id,dataset_id:id,document_name:d.name,drawing_table:true}))}" aria-label="查看 ${esc(d.name)} 的图纸证据">图纸证据</button>`:''}<button class="icon-btn" data-edit="${esc(d.id)}" aria-label="文件设置">⋯</button></td></tr>`).join('')||'<tr><td colspan="8">暂无文件</td></tr>';pager('docs',p,r.total||0,20);$('docRows').querySelectorAll('[data-check]').forEach(c=>c.onchange=()=>{S.selectionEpoch++;c.checked?S.selected.add(c.dataset.check):S.selected.delete(c.dataset.check);syncDocSelection();});$('docRows').querySelectorAll('[data-open]').forEach(b=>b.onclick=()=>busy(b,async()=>{const doc=S.docs.find(d=>d.id===b.dataset.open);if(doc)await openFile({...doc,document_id:doc.id,dataset_id:id});}));$('docRows').querySelectorAll('[data-toggle]').forEach(b=>b.onclick=()=>busy(b,async()=>{const d=S.docs.find(d=>d.id===b.dataset.toggle);await data('PATCH',docPath(d.id,id),{enabled:String(d.status)==='0'?1:0});await loadDocs();}));$('docRows').querySelectorAll('[data-edit]').forEach(button=>button.onclick=()=>editDocument(S.docs.find(doc=>doc.id===button.dataset.edit),id));}
function editDocument(doc,kb){
 if(!doc)return;
 const pdf=/\.pdf$/i.test(doc.name);
 const pdfSetting=pdf?`<label class="row"><input type="checkbox" name="draw_every_pdf_page" ${doc.draw_every_pdf_page?'checked':''}>识别此 PDF 的所有页面</label><p class="hint">启用图纸识别后，重新解析此文件会将每一页的图像发送至所选模型 API。</p>`:'';
 modal('文件设置',`<label>名称</label><input name="name" required value="${esc(doc.name)}">${pdfSetting}<label>解析状态</label><pre class="chunk-content">${esc(doc.progress_msg||status(doc))}</pre>`,async f=>{
  const changes={name:f.get('name')};
  if(pdf)changes.draw_every_pdf_page=f.has('draw_every_pdf_page');
  await data('PATCH',docPath(doc.id,kb),changes);
  await loadDocs();
 });
}
let chunkRequest=0;
async function loadChunks(page=S.chunkPage){
  const {doc,kb}=S;
  if(!doc)return;
  const request=++chunkRequest;
  const current=()=>request===chunkRequest&&S.doc===doc&&S.kb===kb;
  const list=$('chunkList');
  let result;
  try{
    result=await data('GET',`${docPath(doc.id,kb)}/chunks?page=${page}&page_size=10`);
  }catch(error){
    if(!current())return;
    if(!S.chunks.length)list.innerHTML=empty('切片加载失败：'+error.message);
    throw error;
  }
  if(!current())return;
  S.chunkPage=page;
  S.chunks=arrayOf(result);
  list.innerHTML=S.chunks.map((chunk,index)=>`<div class="chunk">
    <div class="chunk-head"><span>切片 ${(page-1)*10+index+1}</span><div class="row">${drawingEvidenceOf(chunk)?`<button type="button" class="text-btn" data-file-source="${esc(JSON.stringify({document_id:doc.id,dataset_id:kb,document_name:doc.name,drawing_evidence:drawingEvidenceOf(chunk)}))}">定位图纸证据</button>`:`<button class="text-btn" data-edit="${index}">编辑</button>`}</div></div>
    <div class="chunk-content">${esc(chunk.content||chunk.content_with_weight)}</div>
  </div>`).join('')||empty('该文件暂无切片');
  list.scrollTop=0;
  $('chunkSummary').textContent=`共 ${result.total||0} 个切片`;
  $('chunksPager').hidden=!result.total;
  pager('chunks',page,result.total||0,10);
  list.querySelectorAll('[data-edit]').forEach(button=>button.onclick=()=>editChunk(S.chunks[+button.dataset.edit]));
}
function editChunk(c){if(!S.doc)return toast('先选择文件',true);const path=docPath();modal(c?'编辑切片':'添加切片',`<label>切片内容</label><textarea name="content" rows="12" required>${esc(c?.content||c?.content_with_weight||'')}</textarea>`,async f=>{await data(c?'PATCH':'POST',path+'/chunks'+(c?'/'+c.id:''),{content:f.get('content')});await loadChunks();await loadDocs();});}bind('addChunk',()=>editChunk());
$('selectAllDocs').onchange=e=>{S.selectionEpoch++;S.docs.forEach(d=>e.target.checked?S.selected.add(d.id):S.selected.delete(d.id));syncDocSelection();};let searchTimer;$('docSearch').oninput=()=>{S.selectionEpoch++;clearTimeout(searchTimer);searchTimer=setTimeout(()=>{S.docPage=1;S.selected.clear();busy(null,loadDocs);},300);};
for(const [suffix,delta] of [['Prev',-1],['Next',1]]){
  $('chunks'+suffix).onclick=()=>busy(null,()=>loadChunks(S.chunkPage+delta));
  for(const [prefix,key,fn] of [['docs','docPage',loadDocs],['sessions','sessionPage',loadSessions]])bind(prefix+suffix,async()=>{S[key]=Math.max(1,S[key]+delta);await fn();});
}
bind('refreshDocs',loadDocs);
async function upload(files,kb,quick=false){if(!kb)throw Error('请先选择知识库');if(!files.length)return;const f=new FormData();for(const file of files)f.append('file',file);toast(`正在上传 ${files.length} 个文件…`);const docs=arrayOf(await data('POST',`${ds(kb)}/documents`,f));if(quick)for(const d of docs){S.baselineSelected.set(d.id,d);}await refreshKbs();await Promise.all([loadQuick(),loadDocs()]);toast('上传完成，可开始解析或审查');}
for(const [id,quick] of [['quickUpload',true]])$(id).onchange=e=>busy(null,async()=>{if(quick&&S.reviewAbort)throw Error('请先停止审查');await upload([...e.target.files],quick?S.quickKb:S.kb,quick);e.target.value='';});
let kbUploading=false;
$('kbAutoParse').checked=loadLocal('lift-kb-auto-parse',false)===true;
function updateAutoParseLabel(){$('kbAutoParseState').textContent=$('kbAutoParse').checked?'已启用':'未启用';}
updateAutoParseLabel();
$('kbAutoParse').onchange=()=>{saveLocal('lift-kb-auto-parse',$('kbAutoParse').checked);updateAutoParseLabel();};
async function uploadKnowledgeFiles(files,kb){
  if(kbUploading)throw Error('已有上传正在进行');if(!kb)throw Error('请先选择知识库');if(!files.length)return;
  kbUploading=true;$('kbUploadMenu').open=false;$('kbUploadStatus').hidden=false;$('kbUploadDetails').hidden=true;
  const autoParse=$('kbAutoParse').checked;
  const log=[],accepted=[];let success=0,failed=0,parseSubmitted=0,parseFailed=0;
  for(const file of files){const reason=uploadSkipReason(file);if(reason)log.push(`${uploadFileName(file)}：跳过 · ${reason}`);else accepted.push(file);}
  const skipped=files.length-accepted.length;
  const progress=text=>{$('kbUploadStatus').textContent=text;};
  for(const id of ['kbUpload','kbFolderUpload','kbAutoParse'])$(id).disabled=true;
  try{
    for(let i=0;i<accepted.length;i++){
      const file=accepted[i];progress(`上传至 ${S.datasets.find(k=>k.id===kb)?.name||'知识库'} · ${i+1}/${accepted.length} · 成功 ${success} · 失败 ${failed} · 跳过 ${skipped}`);
      try{const form=new FormData();form.append('file',file,uploadFileName(file));const docs=arrayOf(await data('POST',`${ds(kb)}/documents`,form));if(!docs.length)throw Error('服务端未返回成功文件');success++;
        if(autoParse){try{const ids=docs.map(d=>d.id).filter(Boolean);if(!ids.length)throw Error('未返回文件 ID');await parse(kb,ids);parseSubmitted+=ids.length;}catch(e){parseFailed++;log.push(`${uploadFileName(file)}：已上传，但自动解析提交失败 · ${e.message}；可在文件列表手动点击解析`);}}
      }
      catch(e){failed++;log.push(`${uploadFileName(file)}：上传失败 · ${e.message}`);if(/登录|权限|unauthoriz|forbidden/i.test(e.message)){for(const remaining of accepted.slice(i+1))log.push(`${uploadFileName(remaining)}：未上传 · 登录失效或权限不足`);break;}}
    }
    progress(`上传结束：成功 ${success}，失败 ${failed}，自动跳过 ${skipped}，未上传 ${accepted.length-success-failed}。${autoParse?`自动解析：已提交 ${parseSubmitted} 个，提交失败 ${parseFailed} 个（解析结果请查看文件状态）。`:'自动解析未启用。'}`);
    if(log.length){$('kbUploadLog').textContent=log.join('\n');$('kbUploadDetails').hidden=false;}
    await refreshKbs();await Promise.all([loadDocs(),loadQuick()]);
  }finally{kbUploading=false;for(const id of ['kbUpload','kbFolderUpload','kbAutoParse'])$(id).disabled=false;}
}
for(const id of ['kbUpload','kbFolderUpload'])$(id).onchange=e=>{const files=[...e.target.files],kb=S.kb;e.target.value='';busy(null,()=>uploadKnowledgeFiles(files,kb));};
$('quickDrop').ondragover=e=>{e.preventDefault();e.currentTarget.classList.add('dragging');};$('quickDrop').ondragleave=e=>e.currentTarget.classList.remove('dragging');$('quickDrop').ondrop=e=>{e.preventDefault();e.currentTarget.classList.remove('dragging');busy(null,async()=>{if(S.reviewAbort)throw Error('请先停止审查');await upload([...e.dataTransfer.files],S.quickKb,true);});};
async function parse(kb,ids,stop=false,signal){const k=S.datasets.find(k=>k.id===kb);return k?.pipeline_id?data('POST','/api/v1/documents/ingest',{doc_ids:ids,run:stop?'2':'1',delete:false},signal):data(stop?'DELETE':'POST',`${ds(kb)}/chunks`,{document_ids:ids},signal);}
for(const [id,stop] of [['parseDocs',false],['stopParse',true]])bind(id,async()=>{if(S.selectingDocuments)throw Error('正在全选文件，请稍候');if(!S.selected.size)throw Error('请先勾选文件');await parse(S.kb,[...S.selected],stop);toast(stop?'已提交取消解析':'已提交解析任务');await loadDocs();});
bind('deleteDocs',()=>{if(!S.selected.size)throw Error('请先勾选文件');const kb=S.kb,ids=[...S.selected];confirmAction('删除文件',`确定删除所选 ${ids.length} 个文件及其切片？`,async()=>{await data('DELETE',`${ds(kb)}/documents`,{ids});S.selected.clear();if(S.kb===kb&&ids.includes(S.doc?.id))setChunkDocument(null,'文件已删除，请重新选择');await refreshKbs();await Promise.all([loadDocs(),loadQuick()]);});});
function kbForm(edit=false){
 const k=S.datasets.find(k=>k.id===S.kb);if(edit&&!k)return;
 const current=edit?S.models.find(m=>m.model_id===k.embedding_model):null;
 const selected=current?modelValue(current):'';
 const drawingSelection=edit?k.parser_config?.drawing_vision_model:S.defaults.find(m=>m.model_type==='vision'&&m.configured!==false)?.model_id;
 const methods=[['naive','通用分块'],['one','每页 / 每个工作表整篇'],['laws','法律条款'],['table','表格 / 按行']];
 modal(edit?'知识库配置':'创建知识库',`<label>名称</label><input name="name" required value="${esc(edit?k.name:'')}"><label>描述</label><textarea name="description">${esc(edit?k.description||'':'')}</textarea><label>分块方法</label><select name="chunk_method">${methods.map(([v,l])=>`<option value="${v}" ${edit&&k.chunk_method===v?'selected':''}>${l}</option>`).join('')}</select><label>分块长度（字符）</label><input name="chunk_size" type="number" min="200" max="6000" value="${edit?k.parser_config?.chunk_size||1000:1000}"><label>重叠长度</label><input name="chunk_overlap" type="number" min="0" max="1999" value="${edit?k.parser_config?.chunk_overlap??120:120}"><label>嵌入模型</label><select name="embedding_model"><option value="">使用默认模型</option>${modelOptions('embedding').map(m=>`<option value="${esc(m.value)}" ${m.value===selected?'selected':''}>${esc(m.label)}</option>`).join('')}</select>${drawingEvidenceSettings(S.models,drawingSelection,S.drawingRecognitionMethod)}<p class="hint">修改解析配置后，请重新解析需更新的文件。原文件会保留。</p>`,async f=>{
 const body={name:f.get('name'),description:f.get('description'),chunk_method:f.get('chunk_method'),parser_config:{...(edit?k.parser_config:{}),chunk_size:Number(f.get('chunk_size')),chunk_overlap:Number(f.get('chunk_overlap')),drawing_vision_model:f.get('drawing_vision_model')}};
 if(f.get('embedding_model'))body.embedding_model=f.get('embedding_model');
 const r=await data(edit?'PUT':'POST',edit?ds(k.id):'/api/v1/datasets',body);if(!edit){S.kb=r.id;resetKbView();}
 await refreshKbs();await loadDocs();toast(edit?'配置已保存；如有解析变更，请重新解析文件':'知识库已创建');
 });
}bind('createKb',()=>kbForm());bind('editKb',()=>kbForm(true));
$('modalBody').addEventListener('click',event=>{
 const link=event.target.closest('a[href="#models"]');
 if(link){$('modal').close();$('resetModelConnection').click();$('configModelType').value='vision';$('configModelType').dispatchEvent(new Event('change'));}
});
function stage(n,text,error=false){document.querySelectorAll('[data-stage]').forEach(e=>e.classList.toggle('active',+e.dataset.stage<=n));$('reviewStatus').textContent=text;$('reviewStatus').classList.toggle('error',error);}
function showStructuredReport(report){
  S.structuredReport=report;S.report=reportMarkdown(report);S.reportMeta=`项目：${report.title}\n时间：${report.createdAt}`;
  const container=$('reportBody');container.innerHTML=renderReportWorkspace(report);bindReportWorkspace(container,report);$('exportReport').disabled=false;$('exportRawReview').disabled=false;
}
function syncViewedRecord(row){
  if(S.structuredReport?.dbId!==row.id)return;
  showStructuredReport({...row.report,dbId:row.id,dbRevision:row.revision});S.workflowRenderKey='';
  $('reportTime').textContent='人工修订已保存 · '+new Date(row.updated_at).toLocaleString('zh-CN',{hour12:false});
}
function clearDeletedReport(identifier){
  if(S.structuredReport?.dbId!==identifier)return;
  leaveWorkflowView();S.structuredReport=null;S.report='';S.reportMeta='';
  $('reportBody').innerHTML=empty('当前审查记录已删除。可打开其他记录，或开始新的审查。');$('reportSources').innerHTML='';
  $('exportReport').disabled=true;$('exportRawReview').disabled=true;$('reportTime').textContent='';
  stage(0,'当前审查记录已删除。');
}
async function recordRequest(method,path='',body){const r=await fetch('/review-records'+path,{method,headers:body?{'Content-Type':'application/json'}:{},body:body?JSON.stringify(body):undefined});const value=await r.json();if(!r.ok)throw Error(value.message||'数据库请求失败');return value;}
let recordQueue=Promise.resolve();
function persistReport(report){
  if(report.workflowJobId)return Promise.resolve(); // The backend owns graph report writes.
  const snapshot=JSON.parse(JSON.stringify(report));
  const task=recordQueue.then(async()=>{const row=await recordRequest(report.dbId?'PUT':'POST',report.dbId?'/'+report.dbId:'',{report:snapshot,revision:report.dbRevision,status:snapshot.complete?'已完成':snapshot.startedAt?'正在生成':'未开始'});report.dbId=row.id;report.dbRevision=row.revision;if(!report.complete)saveLocal('lift-review-draft',report);$('reportTime').textContent='已保存到本机 · '+new Date(row.updated_at).toLocaleString('zh-CN',{hour12:false});});
  recordQueue=task.catch(e=>{$('reportTime').textContent='数据库保存失败：'+e.message;toast('数据库保存失败：'+e.message,true);});return task;
}
async function reviewRecords(){
  if(S.reviewAbort)throw Error('请先停止当前生成，再管理记录');await recordQueue;
  modal('快速审查 · 本地记录',`<div class="record-search-toolbar"><label class="record-search-field" for="recordSearch"><span>搜索任务、知识库或合同</span><input id="recordSearch" placeholder="输入名称或关键词" aria-label="搜索任务、知识库或合同"></label><select id="recordStatus" aria-label="筛选审查状态"><option value="">全部状态</option><option>未开始</option><option>正在生成</option><option>已完成</option></select><button id="searchRecords" type="button" class="btn subtle">检索</button><button id="importRecords" type="button" class="btn subtle">导入旧浏览器记录</button></div><div id="recordRows"></div>`);
  const minute=t=>new Date(t).toLocaleString('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});
  async function search(){const rows=await recordRequest('GET','?'+new URLSearchParams({q:$('recordSearch').value,status:$('recordStatus').value}));$('recordRows').innerHTML=rows.map(r=>`<article class="review-result"><h3>${esc(r.title)} <span class="badge">${esc(r.status)}</span></h3><p>创建时间：${esc(minute(r.created_at))}<br>主体知识库：${esc(r.kb_name)}<br>合同 / 技术协议：${esc(r.contracts.map(x=>x.name).join('、'))}</p><p>${esc(r.notes)}</p><div class="row"><button type="button" class="btn subtle" data-record-open="${r.id}">查看 / 继续</button><button type="button" class="btn subtle" data-record-edit="${r.id}">编辑</button><button type="button" class="btn danger" data-record-delete="${r.id}">删除</button></div></article>`).join('')||empty('暂无匹配记录');
  $('recordRows').querySelectorAll('[data-record-open]').forEach(button=>button.onclick=()=>busy(button,async()=>{
    const row=await recordRequest('GET','/'+button.dataset.recordOpen),report={...row.report,dbId:row.id,dbRevision:row.revision};
    $('modal').close();
    if(report.workflowJobId){await observeWorkflow(report.workflowJobId,true);return;}
    leaveWorkflowView();showStructuredReport(report);
    if(!report.complete){saveLocal('lift-review-draft',report);$('resumeReview').hidden=false;$('resumeReview').textContent=report.results?.length?'查看旧任务':'开始已保存任务';}else $('resumeReview').hidden=true;
    stage(report.complete?3:1,'已载入本地数据库记录'+(report.interruption?' · '+report.interruption:''));
  }));
  $('recordRows').querySelectorAll('[data-record-edit]').forEach(b=>b.onclick=()=>busy(b,async()=>{const row=await recordRequest('GET','/'+b.dataset.recordEdit);if(row.report.workflowJobId&&!row.report.complete)throw Error('该后台任务尚未完成，请从任务清单确认指标；运行中的报告由后台保存。');modal('编辑审查记录',`<label>任务名称<input name="title" required value="${esc(row.title)}"></label><label>备注<textarea name="notes">${esc(row.notes)}</textarea></label><p class="hint">可修订判定和分析；修改后清除旧润色摘要，以修订结果显示看板。主体知识库与依据文件保留，避免改变溯源范围。</p>${row.report.results.map((r,i)=>`<fieldset><legend>${esc(r.requirementId)}</legend><label>判定<select name="status_${i}">${['已满足','部分满足','不满足','证据不足','待人工复核','证据冲突'].map(s=>`<option ${r.status===s?'selected':''}>${s}</option>`).join('')}</select></label><label>分析<textarea name="analysis_${i}">${esc(r.analysis)}</textarea></label></fieldset>`).join('')}`,async f=>{const report=row.report;report.title=String(f.get('title')).trim();report.results=report.results.map((r,i)=>{const status=String(f.get('status_'+i)),analysis=String(f.get('analysis_'+i));return status!==r.status||analysis!==r.analysis?{...r,status,analysis,humanEdited:true,retryable:false}:r;});report.polished=[];report.rawReport=null;report.editedAt=new Date().toISOString();const saved=await recordRequest('PUT','/'+row.id,{report,status:row.status,notes:String(f.get('notes')),revision:row.revision});syncViewedRecord(saved);if(loadLocal('lift-review-draft',null)?.dbId===row.id)localStorage.removeItem('lift-review-draft');toast('已保存修改');},'保存修改');}));
  $('recordRows').querySelectorAll('[data-record-delete]').forEach(b=>b.onclick=()=>{const row=rows.find(r=>r.id===b.dataset.recordDelete);modal('删除审查记录',`<p>删除“${esc(row.title)}”？仅删除该审查记录，不删除知识库或原文件。</p>`,async()=>{await recordRequest('DELETE','/'+row.id,{revision:row.revision});clearDeletedReport(row.id);if(loadLocal('lift-review-draft',null)?.dbId===row.id){localStorage.removeItem('lift-review-draft');$('resumeReview').hidden=true;}toast('记录已删除');},'确认删除');});
  }
  $('searchRecords').onclick=()=>busy(null,search);$('recordStatus').onchange=()=>busy(null,search);$('recordSearch').onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();busy(null,search);}};
  $('importRecords').onclick=()=>busy(null,async()=>{const imported=loadLocal('lift-imported-records',{}),history=loadLocal('lift-reports',[]);let n=0;for(const old of history){if(!old.structured||imported[old.id])continue;await recordRequest('POST','',{report:old.structured,status:old.structured.complete?'已完成':'正在生成'});imported[old.id]=true;saveLocal('lift-imported-records',imported);n++;}toast(`已导入 ${n} 条记录`);await search();});await search();
}
function setWorkflowControls(active){
  if(active&&!S.workflowControls){S.workflowControls=[...document.querySelectorAll('.quick-control button,.quick-control input,.quick-control select,.quick-control textarea')].filter(element=>element.id!=='stopReview').map(element=>[element,['startReview','resumeReview'].includes(element.id)?false:element.disabled]);}
  if(active){for(const [element] of S.workflowControls)element.disabled=true;}
  else if(S.workflowControls){for(const [element,disabled] of S.workflowControls)element.disabled=disabled;S.workflowControls=null;}
  S.reviewAbort=active?{workflowJobId:S.reviewJob?.id}:null;
  $('stopReview').hidden=!active;$('stopReview').disabled=S.reviewJob?.status==='stopping';
  const action=reviewJobAction(S.reviewJob);$('resumeReview').hidden=active||!action;
  if(action)$('resumeReview').textContent=action;
}
const shownConfirmations=new Set();
function applyWorkflowJob(job){
  S.reviewJob=job;saveLocal('lift-review-job',{id:job.id,recordId:job.record_id});
  if(job.report&&typeof job.report==='object'){
    const report={...job.report,workflowJobId:job.id,dbId:job.record_id||job.report.dbId,dbRevision:job.record_revision??job.report.dbRevision};
    if(report.kb&&Array.isArray(report.baselineDocs)&&report.baselineDocs.length){
      const scopeKey=job.id+':'+report.kb+':'+report.baselineDocs.map(document=>document.id||document.document_id).join('|');
      if(S.workflowScopeKey!==scopeKey){
        setWorkflowControls(false);S.workflowScopeKey=scopeKey;S.quickKb=report.kb;
        S.baselineSelected=new Map(report.baselineDocs.map(document=>{const id=document.id||document.document_id;return [id,{...document,id,name:document.name||document.document_name||id}];}));
        if(report.title)$('projectName').value=report.title;$('reviewFocus').value=report.focus||'';
        const configured=S.models.find(model=>model.id===report.model||model.model_id===report.model),modelSelection=configured?modelValue(configured):report.model;
        if([...$('quickModel').options].some(option=>option.value===modelSelection))$('quickModel').value=modelSelection;
        renderKbs();loadQuick().catch(error=>toast(error.message,true));
      }
    }
    const key=[job.id,job.revision,job.record_revision,job.status].join(':');
    if(S.workflowRenderKey!==key){showStructuredReport(report);S.workflowRenderKey=key;}
  }
  setWorkflowControls(isReviewJobBusy(job));
  const labels={pending:'任务已保存，准备开始审查。',running:'后台正在执行审查。',waiting_confirmation:'指标清单已保存，等待你确认后继续。',stopping:'已请求停止，等待当前处理结束后保存进度。',stopped:'任务已暂停，已保存的进度可以继续。',failed:'任务处理失败，已保存进度；请检查说明后继续。',completed:'审查报告已保存。',stale:'材料或模型配置已经变更，请新建任务，避免混用旧结果。'};
  stage(Number(job.stage)||1,job.message||labels[job.status]||'任务状态已同步。',['failed','stale'].includes(job.status));
  $('reportTime').textContent='后台任务状态已保存'+(job.updated_at?' · '+new Date(job.updated_at).toLocaleString('zh-CN',{hour12:false}):'');
  if(job.status==='waiting_confirmation'&&S.workflowAutoConfirm&&!shownConfirmations.has(job.id)&&!$('modal').open){
    shownConfirmations.add(job.id);setTimeout(()=>{if(S.reviewJob?.id===job.id&&S.reviewJob.status==='waiting_confirmation'&&!$('modal').open)editReviewRequirements(S.reviewJob);},0);
  }
}
const reviewJobs=new ReviewJobClient({onState:applyWorkflowJob,onConnection:(connected,error)=>{
  if(!S.reviewJob)return;
  if(!connected)$('reportTime').textContent='进度连接暂时中断，正在重新连接；后台任务未被停止。';
},onError:error=>{if(error.status===401){location.assign('/login');return;}if(error.status===404)leaveWorkflowView();toast(error.message,true);}});
async function observeWorkflow(value,autoConfirm=false){S.workflowAutoConfirm=autoConfirm;return reviewJobs.attach(value);}
function leaveWorkflowView(){reviewJobs.disconnect();S.reviewJob=null;S.workflowRenderKey='';S.workflowScopeKey='';S.workflowAutoConfirm=false;setWorkflowControls(false);localStorage.removeItem('lift-review-job');}
function editReviewRequirements(job){
  if(job.status!=='waiting_confirmation')throw Error('当前任务不在指标确认阶段，请刷新任务状态。');
  const drafts=(job.report?.requirements||[]).map(requirement=>({...requirement})),revision=job.revision;
  modal('第一步 · 确认指标与结题问句',requirementEditorHtml(drafts),async form=>{
    const rows=confirmedRequirements(drafts,form);
    if(S.reviewJob?.id!==job.id)throw Error('当前任务已经切换，请重新打开确认清单。');
    await reviewJobs.resume(rows,revision);
  },'确认并逐条问答');
  mountRequirementEditor(drafts);
}
function taskPayload(draft){
  const kb=draft?.kb||S.quickKb,baselineDocs=draft?.baselineDocs||[...S.baselineSelected.values()];
  if(!kb)throw Error('请选择需要审查的知识库');if(!baselineDocs.length)throw Error('请先上传或选择合同 / 技术协议作为审查依据');
  const selected=$('quickModel').value,defaultModel=getDefaultChatModel(S.models,S.defaults);
  return {kb,baseline_document_ids:baselineDocs.map(document=>document.id),scope:'dataset',target_document_ids:[],model_id:draft?.model||selected||(defaultModel?modelValue(defaultModel):''),title:draft?.title||$('projectName').value,focus:draft?.focus??$('reviewFocus').value,...(draft?.dbId?{record_id:draft.dbId,record_revision:draft.dbRevision}:{})};
}
async function startStructuredReview(resume=false){
  if(S.reviewAbort)throw Error('已有审查正在进行');
  if(resume&&S.reviewJob){
    await reviewJobs.refresh();const job=S.reviewJob;
    if(job.status==='waiting_confirmation'){editReviewRequirements(job);return;}
    if(job.status==='completed'&&job.can_retry){await reviewJobs.retry();return;}
    if(['stopped','failed'].includes(job.status)&&job.can_resume!==false){await reviewJobs.resume();return;}
    throw Error(job.status==='stale'?'材料或模型配置已经变化，请新建审查任务。':'当前任务没有可继续的步骤。');
  }
  if(!S.models.some(model=>(Array.isArray(model.model_type)?model.model_type:[model.model_type]).includes('chat'))){location.hash='models';throw Error('请先配置对话模型，再开始 AI 审查');}
  await recordQueue;
  const draft=resume?loadLocal('lift-review-draft',null):null;
  if(resume&&!draft)throw Error('没有可继续的审查任务');
  if(draft?.workflowJobId){await observeWorkflow(draft.workflowJobId,true);return startStructuredReview(true);}
  if(draft?.results?.length){showStructuredReport(draft);stage(2,'已载入旧流程记录，可查看与导出；开始新的后台审查请点击“提取指标与结题条件”。');$('resumeReview').hidden=true;return;}
  const payload=taskPayload(draft);
  S.workflowAutoConfirm=true;$('reportSources').innerHTML='';
  const job=await reviewJobs.create(payload);
  if(job&&draft?.dbId===job.record_id)localStorage.removeItem('lift-review-draft');
}
async function restoreWorkflow(){
  if(S.reviewJob){await reviewJobs.refresh();return;}
  const saved=loadLocal('lift-review-job',null);
  if(saved?.id){try{await observeWorkflow(saved.id,true);return;}catch(error){if(![403,404].includes(error.status))throw error;localStorage.removeItem('lift-review-job');}}
  const jobs=await reviewJobs.list();
  if(jobs.length){await observeWorkflow(jobs[0].id,true);return;}
  const legacy=loadLocal('lift-review-draft',null);
  $('resumeReview').hidden=!legacy;if(legacy)$('resumeReview').textContent=legacy.results?.length?'查看旧任务':'开始已保存任务';
}
bind('saveReviewTask',async()=>{
  if(!S.quickKb||!S.baselineSelected.size)throw Error('先选择知识库和合同 / 技术协议');
  if(S.reviewAbort)throw Error('请先停止正在运行的审查任务');
  leaveWorkflowView();
  const report={version:4,mode:'three-stage',scope:'dataset',kb:S.quickKb,kbName:S.datasets.find(k=>k.id===S.quickKb)?.name||S.quickKb,title:$('projectName').value,createdAt:new Date().toISOString(),baselineDocs:[...S.baselineSelected.values()],targetDocs:[],requirements:[],results:[],complete:false,model:$('quickModel').value,modelLabel:$('quickModel').selectedOptions[0]?.textContent,focus:$('reviewFocus').value};
  await persistReport(report);saveLocal('lift-review-draft',report);$('resumeReview').hidden=false;$('resumeReview').textContent='开始已保存任务';toast('已保存为未开始任务');
});
$('startReview').onclick=()=>busy($('startReview'),()=>startStructuredReview()).finally(()=>setWorkflowControls(isReviewJobBusy(S.reviewJob)));
$('resumeReview').onclick=()=>busy($('resumeReview'),()=>startStructuredReview(true)).finally(()=>setWorkflowControls(isReviewJobBusy(S.reviewJob)));$('resumeReview').hidden=true;
$('stopReview').onclick=()=>busy($('stopReview'),async()=>{if(!S.reviewJob||!isReviewJobBusy(S.reviewJob))throw Error('当前没有正在执行的后台审查任务');await reviewJobs.stop();}).finally(()=>{$('stopReview').disabled=S.reviewJob?.status==='stopping';});
const workflowButton=document.createElement('button');workflowButton.id='viewReviewWorkflow';workflowButton.type='button';workflowButton.className='btn subtle small';workflowButton.textContent='审查流程';$('exportRawReview').before(workflowButton);
workflowButton.onclick=()=>busy(workflowButton,async()=>{
  const graph=await reviewJobs.graph(S.structuredReport?.workflowJobId||S.reviewJob?.id);
  if(S.reviewJob?.id===graph.task_id)graph.job=S.reviewJob;
  modal('快速审查 · 流程工作台',workflowGraphHtml(graph));
  const dialog=$('modal');dialog.classList.add('workflow-modal');$('modalCancel').textContent='关闭';
  const cleanup=mountWorkflowInspector($('modalBody'),graph);
  const dispose=()=>{cleanup();dialog.removeEventListener('modal-replace',dispose);dialog.removeEventListener('close',onClose);};
  const onClose=()=>{if(!dialog.open){dispose();dialog.classList.remove('workflow-modal','workflow-fullscreen');}};
  dialog.addEventListener('modal-replace',dispose);dialog.addEventListener('close',onClose);
  const action=(label,handler)=>{const button=document.createElement('button');button.type='button';button.className='btn subtle';button.dataset.modalExtra='';button.textContent=label;button.onclick=handler;$('modalCancel').before(button);return button;};
  const fullscreen=action('全屏查看',()=>{const full=dialog.classList.toggle('workflow-fullscreen');fullscreen.textContent=full?'退出全屏':'全屏查看';fullscreen.setAttribute('aria-pressed',String(full));});fullscreen.setAttribute('aria-pressed','false');
  action('导出流程总览',()=>download('快速审查流程总览.svg',workflowOverviewSvg(graph),'image/svg+xml;charset=utf-8'));
  action('导出完整结构',()=>download('快速审查完整节点与连接.svg',workflowGraphSvg(graph),'image/svg+xml;charset=utf-8'));
  const flowExport=action('导出流程图',event=>busy(event.currentTarget,async()=>download('快速审查流程图.svg',await workflowAcademicSvg(graph),'image/svg+xml;charset=utf-8')));
  flowExport.title='科研风格矢量流程图；全部节点、N 编号和连接与完整结构对应';flowExport.dataset.flowExport='';
});
bind('exportRawReview',()=>{if(!S.structuredReport)throw Error('暂无结构化汇报');download('完整审查数据.json',JSON.stringify(S.structuredReport,null,2),'application/json;charset=utf-8');});
bind('exportReport',()=>{if(!S.structuredReport)throw Error('暂无可导出的结构化报告');download(reportHtmlFilename(S.structuredReport),buildReportHtml(S.structuredReport),'text/html;charset=utf-8');});
bind('reportHistory',reviewRecords);
// Shared annotations live on this application's server; localStorage is migration-only.
const annotationCache=new Map(),annotationLoading=new Map();let annotationWriting=0;
function annotationRow(session,index){return annotationCache.get(session)?.find(r=>r.message_index===Number(index))||{state:'none',note:'',revision:0};}
async function annotationRequest(session,method='GET',body){const r=await fetch('/conversation-annotations?session='+encodeURIComponent(session),{method,headers:body?{'Content-Type':'application/json'}:{},body:body?JSON.stringify(body):undefined});const value=await r.json();if(!r.ok)throw Error(value.message||'标记同步失败');return value;}
async function syncAnnotations(session=S.session){
 if(!session)return;if(annotationLoading.has(session))return annotationLoading.get(session);
 const task=(async()=>{const migrated='lift-annotations-migrated:'+session;let rows;
 if(!loadLocal(migrated,false)){
  const marks=loadLocal('lift-marks',{}),notes=loadLocal('lift-notes',{}),prefix=session+':';
  const keys=[...new Set([...Object.keys(marks),...Object.keys(notes)])].filter(k=>k.startsWith(prefix)&&/^\d+$/.test(k.slice(prefix.length)));
  const legacy=keys.map(k=>({message_index:Number(k.slice(prefix.length)),state:marks[k]==='important'?'important':marks[k]?'reviewed':'none',note:String(notes[k]||'')}));
  rows=legacy.length?await annotationRequest(session,'POST',{rows:legacy}):await annotationRequest(session);saveLocal(migrated,true);
 }else rows=await annotationRequest(session);
 const changed=JSON.stringify(annotationCache.get(session))!==JSON.stringify(rows);annotationCache.set(session,rows);if(changed&&S.session===session)renderMessages();
 })();annotationLoading.set(session,task);try{await task;}finally{annotationLoading.delete(session);}
}
async function saveAnnotation(session,index,patch,revision){
 if(!session)throw Error('会话正在创建，请稍后再标记');
 annotationWriting++;
 try{if(!annotationCache.has(session))await syncAnnotations(session);else if(annotationLoading.has(session))await annotationLoading.get(session);
 const row=await annotationRequest(session,'PUT',{message_index:Number(index),revision:revision??annotationRow(session,index).revision,...patch});
 const rows=annotationCache.get(session)||[];annotationCache.set(session,[...rows.filter(r=>r.message_index!==Number(index)),row].sort((a,b)=>a.message_index-b.message_index));if(S.session===session)renderMessages();
 }catch(e){await syncAnnotations(session).catch(()=>{});throw e;}finally{annotationWriting--;}
}
let annotationSyncFailed=false;
async function refreshAnnotations(){if(document.hidden||!S.session||S.page!=='collab'||annotationWriting)return;try{await syncAnnotations();annotationSyncFailed=false;}catch(e){if(!annotationSyncFailed){toast('标记暂未同步：'+e.message,true);annotationSyncFailed=true;}}}
setInterval(refreshAnnotations,4000);window.addEventListener('focus',refreshAnnotations);document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshAnnotations();});
async function migrateLegacyAnnotations(){
 const keys=new Set([...Object.keys(loadLocal('lift-marks',{})),...Object.keys(loadLocal('lift-notes',{}))]);
 const sessions=new Set([...keys].map(k=>/^([a-zA-Z0-9_-]{1,200}):\d+$/.exec(k)?.[1]).filter(Boolean));
 for(const session of sessions)if(!loadLocal('lift-annotations-migrated:'+session,false))await syncAnnotations(session);
}
migrateLegacyAnnotations().catch(e=>toast('旧标记迁移未完成，下次刷新会重试：'+e.message,true));


function messageReviewTools(i){
 const row=annotationRow(S.session,i),state=row.state==='none'?false:row.state,note=row.note;
 const important=state==='important',marked=!!state;
 return `<div class="message-tools"><button type="button" data-mark="${i}" ${S.messages[i]?.pending||isCollabJobUnresolved(S.messages[i]?.workflow)?'disabled':''} data-review-state="${important?'important':marked?'reviewed':'none'}" class="${important?'important':marked?'marked':''}" aria-pressed="${marked}" title="点击切换复核标记；长按 0.65 秒切换重要对话">${important?'★ 重要对话':marked?'✓ 已人工复核':'○ 标记已复核'}</button><button type="button" data-note="${i}" ${S.messages[i]?.pending||isCollabJobUnresolved(S.messages[i]?.workflow)?'disabled':''}>✎ 复核备注</button><div class="message-review-note" ${note.trim()?'':'hidden'}>${esc(note)}</div></div>`;
}
function bindMessageReviewTools(){
 $('chatMessages').querySelectorAll('[data-mark]').forEach(b=>{
  if(b._reviewBound===S.session)return;b._reviewBound=S.session;
  const session=S.session,index=b.dataset.mark,key=session+':'+index;let timer=0,long=false,origin=null;
  function save(important){return busy(b,async()=>{await syncAnnotations(session);const old=annotationRow(session,index).state;const state=important?(old==='important'?'reviewed':'important'):(old==='none'?'reviewed':'none');await saveAnnotation(session,index,{state});if(important)toast(state==='important'?'已标记为重要对话':'已恢复为人工复核');});}
  function cancel(){clearTimeout(timer);timer=0;b.classList.remove('mark-holding');}
  function begin(){cancel();long=false;b.classList.add('mark-holding');timer=setTimeout(()=>{cancel();if(!b.isConnected||S.session!==session)return;long=true;save(true);},650);}
  b.onpointerdown=e=>{if(e.button!==0)return;origin={x:e.clientX,y:e.clientY};begin();};
  b.onpointermove=e=>{if(origin&&Math.hypot(e.clientX-origin.x,e.clientY-origin.y)>8)cancel();};
  b.onpointerup=b.onpointercancel=b.onpointerleave=()=>{origin=null;cancel();};
  b.oncontextmenu=e=>{e.preventDefault();};
  b.onkeydown=e=>{if([' ','Enter'].includes(e.key)){e.preventDefault();if(!e.repeat)begin();}};
  b.onkeyup=e=>{if([' ','Enter'].includes(e.key)){e.preventDefault();cancel();if(!long)save(false);}};
  b.onblur=cancel;
  b.onclick=e=>{e.preventDefault();cancel();if(long){long=false;return;}save(false);};
 });
 $('chatMessages').querySelectorAll('[data-note]').forEach(b=>b.onclick=()=>busy(b,async()=>{const session=S.session,index=Number(b.dataset.note);if(!session)throw Error('会话正在创建，请稍后再添加备注');await syncAnnotations(session);const row=annotationRow(session,index);modal('人工复核备注 · 多端同步',`<textarea name="note" rows="6" maxlength="20000">${esc(row.note)}</textarea>`,async f=>{await saveAnnotation(session,index,{note:String(f.get('note')||'').trim()},row.revision);toast('备注已保存');});}));
}
function renderMessages(){const opened=new Set([...$('chatMessages').querySelectorAll('details[open]')].map(e=>e.dataset.detailKey));const scroll=$('chatMessages').scrollTop;const markup=S.messages.map((m,i)=>`<article data-message-index="${i}" class="message ${m.role==='user'?'user':''} ${m.pending?'is-streaming':''}" aria-busy="${m.pending?'true':'false'}"><div class="message-role">${m.role==='user'?'你 · 人工审查':'LIFT AI · 审查助手'}${m.role==='assistant'?`<span class="reply-state" role="status" data-collab-state="${esc(m.workflow?.status||m.replyState||'complete')}">${m.pending?'<span class="typing-dots" aria-hidden="true"><i></i><i></i><i></i></span>':''}${esc(collabReplyLabel(m))}</span>`:''}</div><div class="message-text">${m.role==='user'?`<div class="markdown"><p>${esc(m.content)}</p></div>${chatImageHtml(m.image)}`:answerHtml(((m.reasoning||m.reasoning_content)&&!String(m.content).includes('<think>')?'<think>'+(m.reasoning||m.reasoning_content)+'</think>':'')+m.content)}${m.role==='assistant'&&!m.workflow&&!(m.reasoning||m.reasoning_content)&&!String(m.content).includes('<think>')?`<details class="reasoning"><summary>思考过程</summary><p>${m.pending?'正在等待模型返回；仅展示模型实际提供的思考内容。':'该回答未包含模型返回的独立思考内容。'}</p></details>`:''}</div>${sourcesHtml(m.reference?.chunks||m.reference||[],S.messages.slice(0,i).findLast(x=>x.role==='user')?.content||'')}${m.role==='assistant'?messageReviewTools(i):''}</article>`).join('')||empty('选择审查助手，开始一段有依据的协审对话');
const container=$('chatMessages'),template=document.createElement('template');template.innerHTML=markup;
const nodes=[...template.content.children];
for(let i=0;i<nodes.length;i++){const fresh=nodes[i],old=container.children[i];if(old?.classList.contains('message')&&fresh.classList.contains('message')&&old._message===S.messages[i]){old.className=fresh.className+(old.classList.contains('bubble-enter')?' bubble-enter':'');old.setAttribute('aria-busy',fresh.getAttribute('aria-busy'));for(const child of [...fresh.children]){const existing=[...old.children].find(x=>x.className===child.className);if(existing){if(existing.innerHTML!==child.innerHTML)existing.innerHTML=child.innerHTML;}else old.append(child);}nodes[i]=old;}else{fresh._message=S.messages[i];if(S.messages[i]?.animateIn){fresh.classList.add('bubble-enter');delete S.messages[i].animateIn;fresh.addEventListener('animationend',()=>fresh.classList.remove('bubble-enter'),{once:true});}}}
// Keep existing message elements attached so streaming does not restart border or bubble animations.
for(let i=0;i<nodes.length;i++)if(container.children[i]!==nodes[i])container.insertBefore(nodes[i],container.children[i]||null);
while(container.children.length>nodes.length)container.lastElementChild.remove();
$('chatMessages').querySelectorAll('.message').forEach((message,i)=>message.querySelectorAll('details').forEach((d,j)=>{d.dataset.detailKey=i+':'+j;if(opened.has(d.dataset.detailKey))d.open=true;}));$('chatMessages').scrollTop=scroll;bindMessageReviewTools();}
async function loadSessions(){const id=$('assistantSelect').value;if(!id){$('sessionList').innerHTML=empty('先创建一个审查助手');$('sessionsPage').textContent='1';$('sessionsPrev').disabled=true;$('sessionsNext').disabled=true;return;}const p=S.sessionPage,r=await data('GET',`${chatPath()}/sessions?page=${p}&page_size=20`);if(id!==$('assistantSelect').value||p!==S.sessionPage)return;const rows=arrayOf(r);$('sessionList').innerHTML=rows.map(s=>`<div class="session-item ${s.id===S.session?'active':''}"><button data-session="${esc(s.id)}"><strong>${esc(s.name||'新会话')}</strong><small>${esc(date(s.update_time||s.create_time))}</small></button><button class="icon-btn" data-rename="${esc(s.id)}" aria-label="重命名会话">✎</button><button class="icon-btn" data-delete="${esc(s.id)}" aria-label="删除会话">×</button></div>`).join('')||empty('暂无会话，发送问题即可创建');$('sessionsPage').textContent=p;$('sessionsPrev').disabled=p<=1;$('sessionsNext').disabled=rows.length<20;const base=chatPath();$('sessionList').querySelectorAll('[data-session]').forEach(b=>b.onclick=()=>busy(b,async()=>{if(S.chatAbort)throw Error('请先停止当前回答');await selectCollabSession(b.dataset.session,id);}));$('sessionList').querySelectorAll('[data-rename]').forEach(b=>b.onclick=()=>modal('重命名会话','<input name="name" required placeholder="会话名称">',async f=>{await data('PATCH',base+'/sessions/'+b.dataset.rename,{name:f.get('name')});await loadSessions();}));$('sessionList').querySelectorAll('[data-delete]').forEach(b=>b.onclick=()=>{if(S.chatAbort)return toast('请先停止当前回答',true);confirmAction('删除会话','确定删除这段会话及历史消息？',async()=>{await data('DELETE',base+'/sessions',{ids:[b.dataset.delete]});if(S.session===b.dataset.delete){resetCollabObserver(true);S.session='';S.messages=[];renderMessages();}await loadSessions();});});}
let chatFilesRequest=0;
async function loadChatFiles(){
  const requestId=++chatFilesRequest,id=$('assistantSelect').value,chat=S.chats.find(c=>c.id===id);
  const current=()=>requestId===chatFilesRequest&&id===$('assistantSelect').value;
  try{
    const groups=await Promise.all((chat?.dataset_ids||[]).map(async kb=>({kb,result:await data('GET',`${ds(kb)}/documents?page_size=10`)})));
    if(!current())return;
    $('chatFiles').innerHTML=groups.map(({kb,result})=>arrayOf(result).map(d=>`<a class="file-item" href="${proxyUrl(docPath(d.id,kb))}" download>${fileIcon(d.name)}<div><strong>${esc(d.name)}</strong><small>${esc(status(d))}</small></div></a>`).join('')+(result.total>10?`<p class="hint">该库共 ${result.total} 个文件，显示前 10 个</p>`:'')).join('')||empty(chat?'此助手未关联知识库文件':'请先选择审查助手');
  }catch(error){if(current())throw error;}
}
function renderAssistants(){setOptions($('assistantSelect'),S.chats.map(c=>({value:c.id,label:c.import_source&&!c.dataset_ids?.length?`${c.name}（未关联知识库）`:c.name})),'请选择审查助手',{placeholderDisabled:true});}
async function refreshAssistantsAfterDelete(){
  const chats=arrayOf(await data('GET','/api/v1/chats?page_size=100'));
  const select=$('assistantSelect'),previous=select.value;
  S.chats=chats;renderAssistants();
  if(previous&&chats.some(chat=>chat.id===previous)){await loadChatFiles();return;}
  select.value=chats[0]?.id||'';
  if(select.value!==previous)await changeAssistant();
  else await loadChatFiles();
}
async function changeAssistant(){const view=resetCollabObserver(),assistant=$('assistantSelect').value;S.collabLoading=true;setCollabControls();$('assistantOptions').disabled=!assistant;updateModelName();S.session='';S.sessionPage=1;S.messages=[];renderMessages();try{await Promise.all([loadSessions(),loadChatFiles()]);if(view===collabViewEpoch&&assistant===$('assistantSelect').value)await restoreCollabForSession('',view);}finally{if(view===collabViewEpoch){S.collabLoading=false;setCollabControls();}}}
$('assistantSelect').onchange=()=>busy(null,changeAssistant);bind('newSession',()=>{if(S.chatAbort)throw Error('请先停止当前回答');resetCollabObserver(true);S.session='';S.messages=[];S.collabPendingCreate=null;renderMessages();return loadSessions();});bind('chatFilesRefresh',loadChatFiles);bind('manageChatFiles',async()=>{const id=S.chats.find(c=>c.id===$('assistantSelect').value)?.dataset_ids?.[0];if(id){S.kb=id;S.docPage=1;S.selected.clear();renderKbs();await loadDocs();}location.hash='knowledge';});
bind('assistantOptions',()=>{
 if(S.chatAbort)throw Error('请先停止当前回答');
 const chat=S.chats.find(c=>c.id===$('assistantSelect').value);if(!chat)throw Error('请先选择审查助手');
 modal(chat.name+' · 选项','<div class="kb-action-list"><button type="button" id="renameAssistantAction" class="btn subtle">重命名</button><button type="button" id="deleteAssistantAction" class="btn danger">删除助手</button></div>');
 $('renameAssistantAction').onclick=()=>modal('重命名审查助手',`<label for="assistantRename">助手名称</label><input id="assistantRename" name="name" value="${esc(chat.name)}" required>`,async f=>{
 const name=f.get('name').trim();if(!name)throw Error('名称不能为空');await data('PATCH','/api/v1/chats/'+encodeURIComponent(chat.id),{name});chat.name=name;renderAssistants();toast('审查助手已重命名');
 });
 $('deleteAssistantAction').onclick=()=>modal('删除审查助手',`<p>确定删除「${esc(chat.name)}」？删除后该助手及其会话将无法从此界面访问，关联知识库和文件不会删除。</p><label for="assistantDeleteName">输入助手名称以确认</label><input id="assistantDeleteName" name="name" required autocomplete="off">`,async f=>{
 if(f.get('name')!==chat.name)throw Error('名称不匹配，未执行删除');if(S.chatAbort)throw Error('请先停止当前回答');
 await data('DELETE','/api/v1/chats/'+encodeURIComponent(chat.id));S.chats=S.chats.filter(c=>c.id!==chat.id);renderAssistants();$('assistantSelect').value=S.chats[0]?.id||'';await changeAssistant();toast('审查助手已删除');
 },'确认删除');
});
bind('newAssistant',()=>modal('新建审查助手',`<label>助手名称</label><input name="name" required><label>关联知识库</label><div class="check-list">${S.datasets.map(k=>`<label><input name="kb" type="checkbox" value="${esc(k.id)}">${esc(k.name)}</label>`).join('')}</div>`,async f=>{const c=await data('POST','/api/v1/chats',{name:f.get('name'),dataset_ids:f.getAll('kb')});S.chats=arrayOf(await data('GET','/api/v1/chats?page_size=100'));renderAssistants();$('assistantSelect').value=c.id;await changeAssistant();}));
// Reveal the new exchange immediately, before waiting for the first model token.
function revealCurrentChat(){
 const messages=$('chatMessages'),workspace=messages.closest('.chat-workspace');
 requestAnimationFrame(()=>{
  if($('collab').hidden)return;
  const bounds=workspace.getBoundingClientRect();
  if(bounds.top<0||bounds.bottom>innerHeight)workspace.scrollIntoView({block:'start',behavior:'instant'});
  messages.scrollTo({top:messages.scrollHeight,behavior:'instant'});
 });
}
// Each exchange is a durable backend workflow. This observer can be detached safely.
let collabViewEpoch=0;
const collabJobs=new CollabJobClient({onState:applyCollabJob,onConnection:connected=>{S.collabConnected=connected;renderCollabPanel();},onError:error=>{
  if(error.status===401){location.assign('/login');return;}
  if(error.status===404){resetCollabObserver(true);busy(null,loadSessions);}
  toast(error.message,true);
}});
function rememberCollabView(){
  const chat=$('assistantSelect').value;
  if(chat&&S.session)saveLocal('lift-collab-view',{chat_id:chat,session_id:S.session,job_id:S.collabJob?.id||''});
}
function setCollabControls(){
  const running=isCollabJobRunning(S.collabJob),blocked=isCollabJobUnresolved(S.collabJob)||!!S.collabSubmitting||!!S.collabLoading;
  S.chatAbort=running||S.collabSubmitting?{collabJobId:S.collabJob?.id}:null;
  $('chatInput').disabled=blocked;$('sendChat').disabled=blocked;$('chatModel').disabled=blocked;$('collabRequireConfirmation').disabled=blocked;
  $('chooseChatImage').disabled=blocked;$('removeChatImage').disabled=blocked;
  $('assistantSelect').disabled=running||!!S.collabSubmitting;$('newSession').disabled=running||!!S.collabSubmitting;$('newAssistant').disabled=running||!!S.collabSubmitting;
  $('stopChat').hidden=!running;$('stopChat').disabled=S.collabJob?.status==='stopping';
  $('chatInput').placeholder=blocked?'本轮协审尚未结束，请先确认、继续或结束本轮，再提出新问题。':'输入需要核对的条款或问题，Shift + Enter 换行';
}
function resetCollabObserver(clearStored=false){
  collabViewEpoch++;collabJobs.disconnect();S.collabJob=null;S.collabConnected=true;S.collabSubmitting=false;S.collabLoading=false;
  setChatImage();
  if(clearStored)localStorage.removeItem('lift-collab-view');
  renderCollabPanel();setCollabControls();return collabViewEpoch;
}
function renderCollabPanel(){
  const panel=$('collabWorkflow');if(!panel)return;
  const opened=new Set([...panel.querySelectorAll('details[open]')].map(detail=>detail.dataset.collabDetail));
  panel.hidden=!S.collabJob;panel.innerHTML=collabWorkflowHtml(S.collabJob,{connected:S.collabConnected!==false});
  panel.querySelectorAll('details[data-collab-detail]').forEach(detail=>{detail.open=opened.has(detail.dataset.collabDetail);});
  panel.querySelectorAll('[data-collab-action]').forEach(button=>button.onclick=()=>busy(button,()=>collabAction(button.dataset.collabAction)));
}
function applyCollabJob(job){
  if(job.chat_id!==$('assistantSelect').value||(S.session&&job.session_id!==S.session))return;
  const previous=S.collabJob,changedSession=S.session!==job.session_id;
  const messages=$('chatMessages'),follow=!previous||previous.id!==job.id||messages.scrollHeight-messages.clientHeight-messages.scrollTop<140;
  S.session=job.session_id;S.collabJob=job;S.collabConnected=true;S.messages=mergeCollabMessages(S.messages,job);rememberCollabView();
  renderMessages();renderCollabPanel();setCollabControls();
  if(follow)requestAnimationFrame(()=>{if(S.collabJob?.id===job.id)messages.scrollTop=messages.scrollHeight;});
  if(changedSession||previous?.status!==job.status){busy(null,async()=>{await loadSessions();if(S.session===job.session_id)await syncAnnotations(S.session);});}
}
async function selectCollabSession(sessionId,chat){
  const view=resetCollabObserver();S.collabLoading=true;setCollabControls();
  try{
    const session=await data('GET',`/api/v1/chats/${encodeURIComponent(chat)}/sessions/${encodeURIComponent(sessionId)}`);
    if(view!==collabViewEpoch||chat!==$('assistantSelect').value)return;
    S.session=session.id;S.messages=session.messages||session.message||[];rememberCollabView();renderMessages();
    await syncAnnotations(S.session);await loadSessions();await restoreCollabForSession(session.id,view);
  }finally{if(view===collabViewEpoch){S.collabLoading=false;setCollabControls();}}
}
async function restoreCollabForSession(sessionId='',view=collabViewEpoch){
  const chat=$('assistantSelect').value;if(!chat)return;
  const saved=loadLocal('lift-collab-view',{});
  let target=sessionId||(saved.chat_id===chat?saved.session_id:'');
  let jobs=await collabJobs.list(target?{chat_id:chat,session_id:target}:{chat_id:chat});
  if(view!==collabViewEpoch||chat!==$('assistantSelect').value)return;
  if(!target){const open=jobs.find(isCollabJobUnresolved);target=open?.session_id||'';jobs=open?[open]:[];}
  const job=jobs.find(row=>row.session_id===target&&row.chat_id===chat);
  if(!target)return;
  if(S.session!==target){
    try{
      const session=await data('GET',`/api/v1/chats/${encodeURIComponent(chat)}/sessions/${encodeURIComponent(target)}`);
      if(view!==collabViewEpoch||chat!==$('assistantSelect').value)return;
      S.session=session.id;S.messages=session.messages||session.message||[];rememberCollabView();renderMessages();await syncAnnotations(S.session);await loadSessions();
    }catch(error){if(view===collabViewEpoch){localStorage.removeItem('lift-collab-view');throw error;}return;}
  }
  if(view!==collabViewEpoch||S.session!==target||chat!==$('assistantSelect').value)return;
  if(job)await collabJobs.attach(job);
}
function assertCollabDialog(job){if(S.collabJob?.id!==job.id||collabJobs.job?.id!==job.id)throw Error('当前协审会话已切换，请重新打开需要确认的草稿。');}
async function collabAction(action){
  const job=S.collabJob;if(!job)throw Error('请先打开协审任务');
  if(action==='stop')return collabJobs.stop();
  if(action==='resume')return collabJobs.resume();
  if(action==='cancel'){confirmAction('结束本轮协审','结束这次协审并保留已有记录？结束后可在当前会话提出新的问题。',async()=>{assertCollabDialog(job);await collabJobs.command('cancel',{revision:job.revision});});return;}
  if(!['approve','revise','supplement','reject'].includes(action)||job.status!=='waiting_confirmation')throw Error('本轮尚未进入人工确认阶段，请先同步最新状态。');
  const intro='<p class="collab-review-context">本操作记录你对本轮协审草稿的复核意见。请核对来源、对象、单位和适用条件；不会自动改写快速审查报告。</p>';
  const notes='<label for="collabReviewNotes">复核备注（可选）</label><textarea id="collabReviewNotes" name="notes" rows="3" maxlength="10000"></textarea>';
  if(action==='supplement'){
    modal('补充检索并继续协审',intro+'<label for="collabSupplementQuery">需要进一步核查的问题</label><textarea id="collabSupplementQuery" name="query" rows="4" maxlength="16000" required placeholder="明确需要查找的对象、参数、测试记录或证据缺口"></textarea>'+notes,async form=>{
      assertCollabDialog(job);const query=String(form.get('query')||'').trim();if(!query)throw Error('请输入补充核查的问题');
      await collabJobs.review({revision:job.revision,decision:'supplement',query,notes:String(form.get('notes')||'').trim()});
    },'提交并补充检索');return;
  }
  const editable=action==='revise',title={approve:'确认本轮复核意见',revise:'修改草稿后确认',reject:'退回本轮草稿'}[action];
  modal(title,intro+`<label for="collabReviewAnswer">${editable?'修订后的意见':'当前草稿'}</label><textarea id="collabReviewAnswer" name="answer" class="collab-review-editor" rows="9" maxlength="16000" ${editable?'required':'readonly'}>${esc(job.answer||'')}</textarea>`+notes,async form=>{
    assertCollabDialog(job);const answer=String(form.get('answer')||'').trim();if(editable&&!answer)throw Error('修订后的意见不能为空');
    await collabJobs.review({revision:job.revision,decision:action,...(editable?{answer}:{}),notes:String(form.get('notes')||'').trim()});
  },{approve:'确认并保存',revise:'保存人工修订',reject:'确认退回'}[action]);
}
let chatImage=null,chatImagePreview='';
function chatImageHtml(image){
  if(!image?.id)return '';
  const url='/api/v1/collab-jobs/images/'+encodeURIComponent(image.id);
  return `<a class="chat-message-image" href="${url}" target="_blank" rel="noopener" title="查看原图">
    <img src="${url}" alt="${esc(image.name)}">
    <small>${esc(image.name)}</small>
  </a>`;
}
function setChatImage(file=null){
  if(file&&(file.size===0||file.size>8*1024*1024)){
    toast('请选择 8 MB 以内的有效图片',true);
    return;
  }
  if(file&&!/\.(png|jpe?g|webp)$/i.test(file.name)&&!['image/png','image/jpeg','image/webp'].includes(file.type)){
    toast('请使用 PNG、JPEG 或 WebP 图片',true);
    return;
  }
  if(chatImagePreview)URL.revokeObjectURL(chatImagePreview);
  chatImage=file;
  chatImagePreview=file?URL.createObjectURL(file):'';
  if(file)$('chatImageThumb').src=chatImagePreview;
  else $('chatImageThumb').removeAttribute('src');
  $('chatImageName').textContent=file?.name||'';
  $('chatImageDraft').hidden=!file;
}
function readChatImage(file){
  return new Promise((resolve,reject)=>{
    const reader=new FileReader();
    reader.onload=()=>resolve({name:file.name,data:reader.result.split(',',2)[1]});
    reader.onerror=()=>reject(new Error('读取图片失败，请重新选择'));
    reader.readAsDataURL(file);
  });
}
$('chooseChatImage').onclick=()=>$('chatImageInput').click();
$('chatImageInput').onchange=event=>{
  const file=event.target.files[0];
  if(file)setChatImage(file);
  event.target.value='';
};
$('removeChatImage').onclick=()=>setChatImage();
$('chatInput').onpaste=event=>{
  const file=[...event.clipboardData.files].find(item=>item.type.startsWith('image/'));
  if(file){event.preventDefault();setChatImage(file);}
};

$('chatForm').onsubmit=async event=>{
  event.preventDefault();if(S.collabSubmitting||S.collabLoading||isCollabJobUnresolved(S.collabJob))return;
  const typedQuestion=$('chatInput').value.trim(),file=chatImage,chat=$('assistantSelect').value;
  if(!typedQuestion&&!file)return;
  const question=typedQuestion||'请描述附件图中可辨认的内容和标注。';
  if(!S.models.some(model=>(Array.isArray(model.model_type)?model.model_type:[model.model_type]).includes('chat'))){location.hash='models';toast('请先配置对话模型',true);return;}
  if(!chat){toast('请先选择审查助手',true);return;}
  const view=collabViewEpoch,prior=S.collabPendingCreate,session=S.session||'';
  const model=$('chatModel').value,confirm=$('collabRequireConfirmation').checked;
  const requestId=prior&&prior.chat===chat&&prior.session===session&&prior.question===question&&prior.file===file&&prior.model===model&&prior.confirm===confirm?prior.id:globalThis.crypto?.randomUUID?.()||('collab-'+Date.now()+'-'+Math.random().toString(36).slice(2));
  S.collabPendingCreate={id:requestId,chat,session,question,file,model,confirm};S.collabSubmitting=true;setCollabControls();
  try{
    const image=file?await readChatImage(file):null;
    if(view!==collabViewEpoch||chat!==$('assistantSelect').value)return;
    const job=await collabJobs.create({chat_id:chat,...(session?{session_id:session}:{}),question,...(model?{model_id:model}:{}),...(image?{image}:{}),require_confirmation:confirm,request_id:requestId});
    if(view!==collabViewEpoch||chat!==$('assistantSelect').value||!job)return;
    S.collabPendingCreate=null;
    if($('chatInput').value.trim()===typedQuestion)$('chatInput').value='';
    if(chatImage===file)setChatImage();
    revealCurrentChat();
  }catch(error){
    if(error.status===401){location.assign('/login');return;}
    if(view===collabViewEpoch){toast(error.message||'协审任务未能连接，请重新连接后再试。',true);if(error.status===409)await restoreCollabForSession(session,view).catch(()=>{});}
  }finally{if(view===collabViewEpoch){S.collabSubmitting=false;setCollabControls();}}
};
$('chatInput').onkeydown=event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();$('chatForm').requestSubmit();}};
$('stopChat').onclick=()=>busy($('stopChat'),()=>collabJobs.stop());
window.addEventListener('focus',()=>{if(S.collabJob)collabJobs.refresh().catch(error=>{if(error.status===401)location.assign('/login');});});
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&S.collabJob)collabJobs.refresh().catch(()=>{});});
$('threshold').oninput=e=>$('thresholdValue').textContent=e.target.value;

function retrievalExcerpt(content){
  return String(content).replace(/\r\n?/g,'\n').replace(/<table\b[^>]*>[\s\S]*?<\/table\s*>/gi, markup=>{
    // Imported tables can be stored as HTML. Read the cells from an inert fragment.
    const template=document.createElement('template');
    template.innerHTML=markup;
    const table=template.content.querySelector('table');
    if(!table?.rows.length)return markup;
    const rows=Array.from(table.rows,row=>Array.from(row.cells,cell=>cell.textContent.trim()).join(' ｜ '));
    return '\n'+rows.join('\n')+'\n';
  }).trim();
}

function retrievalCard(chunk){
  const path=String(chunk.document_name||chunk.doc_name||'未知来源');
  const filename=path.split(/[\\/]/).pop()||path;
  const folder=path.slice(0,path.length-filename.length);
  const score=Number(chunk.similarity);
  const header=`<span class="retrieval-card-head">
    ${fileIcon(filename)}
    <span class="retrieval-source"><strong>${esc(filename)}</strong>${folder?`<small>${esc(folder)}</small>`:''}</span>
    <span class="retrieval-score">相似度 <b>${chunk.similarity!=null&&Number.isFinite(score)?score.toFixed(3):'—'}</b></span>
  </span>`;
  const excerpt=retrievalExcerpt(chunk.content||chunk.content_with_weight||'');
  const preview=Array.from(excerpt.split('\n',4).join('\n')).slice(0,280).join('').trimEnd();
  const full=`<p class="retrieval-text">${esc(excerpt||'暂无可显示的切片正文')}</p>`;
  if(preview===excerpt)return `<li class="retrieval-card">${header}${full}</li>`;
  return `<li class="retrieval-card"><details>
    <summary>
      ${header}
      <span class="retrieval-text retrieval-preview">${esc(preview)}…</span>
      <span class="retrieval-toggle"><span>展开切片</span><span>收起切片</span></span>
    </summary>
    ${full}
  </details></li>`;
}

bind('retrieve',async()=>{
  const datasetId=$('retrievalKb').value;
  const question=$('retrievalQuestion').value.trim();
  if(!datasetId||!question)throw Error('请选择知识库并输入检索问题');
  const results=$('retrievalResults'),totalLabel=$('retrievalTotal');
  totalLabel.textContent='';
  results.innerHTML=empty('正在检索…');
  try{
    const response=await data('POST','/api/v1/retrieval',{question,dataset_ids:[datasetId],page_size:10,similarity_threshold:+$('threshold').value});
    const chunks=response.chunks||[],total=response.total??chunks.length;
    totalLabel.textContent=total>chunks.length?`${total} 条命中 · 显示前 ${chunks.length} 条`:`${total} 条命中`;
    results.innerHTML=chunks.length?`<ul class="retrieval-card-list" role="list">${chunks.map(retrievalCard).join('')}</ul>`:empty('未命中，请调整问题或阈值');
    results.scrollTop=0;
  }catch(error){
    totalLabel.textContent='';
    results.innerHTML=empty('检索失败：'+error.message);
    throw error;
  }
});
function updateModelName(){const select=$('chatModel'),chat=S.chats.find(c=>c.id===$('assistantSelect').value);const configured=chat?.llm_id||chat?.tenant_llm_id||chat?.llm?.model_name;const fallback=S.defaults.find(m=>m.model_type==='chat');const found=S.models.find(m=>m.model_id===configured||modelValue(m)===configured||m.name===configured);const label=found?`${found.name} · ${found.instance_name}`:configured||fallback?.model_name||'尚未配置默认模型';if(select.options[0])select.options[0].textContent=`${label}（助手 / 默认）`;const text=select.selectedOptions[0]?.textContent||label;select.title=text;}
$('chatModel').onchange=updateModelName;
function renderModels(){
  for(const id of ['quickModel','chatModel'])setOptions($(id),modelOptions('chat'),id==='quickModel'?'使用默认对话模型':'使用助手模型 / 默认模型');$('modelList').innerHTML=S.models.map(m=>`<article class="model-card"><h3>${esc(m.name)}</h3><p class="hint">${esc(m.provider_name)} / ${esc(m.instance_name)}</p>${(Array.isArray(m.model_type)?m.model_type:[m.model_type]).map(t=>`<span class="badge">${esc(modelTypeLabels[t]||t)}</span>`).join('')}</article>`).join('')||empty('尚未配置模型');
  const defaults=$('defaultModels');
  const names=modelTypeLabels;
  defaults.innerHTML=Object.entries(names).map(([type,label])=>{
    const options=S.models.filter(model=>[model.model_type].flat().includes(type));
    const defaultId=S.defaults.find(model=>model.model_type===type&&model.configured!==false)?.model_id;
    const hasDefault=options.some(model=>model.model_id===defaultId);
    return `<div>
      <h3>${label}</h3>
      <label for="default-model-${type}">本地服务 默认模型</label>
      <div class="default-model-select">
        <select id="default-model-${type}" data-type="${type}" ${options.length?'':'disabled'}>
          ${options.map(model=>`<option value="${esc(model.model_id)}" ${model.model_id===defaultId?'selected':''}>${esc(model.name)} · ${esc(model.instance_name)}</option>`).join('')}
        </select>
        <span id="default-model-${type}-placeholder" class="default-model-placeholder" ${hasDefault?'hidden':''}>未设置</span>
      </div>
      <button class="btn subtle small" data-default="${type}" ${hasDefault?'':'disabled'}>设为默认</button>
    </div>`;
  }).join('');
  defaults.querySelectorAll('[data-default]').forEach(button=>{
    const type=button.dataset.default;
    const select=$('default-model-'+type);
    const placeholder=$('default-model-'+type+'-placeholder');
    if(!placeholder.hidden)select.selectedIndex=-1;
    function syncSelection(){
      const unset=select.selectedIndex===-1;
      placeholder.hidden=!unset;
      button.disabled=unset;
      if(unset)select.setAttribute('aria-describedby',placeholder.id);
      else select.removeAttribute('aria-describedby');
    }
    syncSelection();
    select.onchange=syncSelection;
    button.onclick=()=>{
      const modelId=select.value;
      if(!modelId)return;
      confirmAction('更新默认模型','此设置将同步到 本地服务，影响使用默认配置的功能。',async()=>{
        S.defaults=arrayOf(await data('PATCH','/api/v1/models/default',{model_type:type,model_id:modelId}));
        renderModels();
        updateModelName();
        toast('默认模型已更新');
      });
    };
  });
}
bind('runApi',async()=>{const method=$('apiMethod').value,path=$('apiPath').value.trim();if(/[{}]/.test(path))throw Error('请将路径中的占位符替换为真实参数');const body=$('apiBody').value.trim()?JSON.parse($('apiBody').value):undefined;const run=async()=>{$('apiResponse').textContent='正在请求…';try{const r=await request(method,path,method==='GET'?undefined:body);if(r.headers.get('content-type')?.includes('text/event-stream')){await readSSE(r,d=>$('apiResponse').textContent=JSON.stringify(d,null,2));}else if(r.headers.get('content-type')?.includes('json'))$('apiResponse').textContent=`HTTP ${r.status}\n`+JSON.stringify(await r.json(),null,2);else{const blob=await r.blob();download('response.bin',blob,blob.type);$('apiResponse').textContent=`HTTP ${r.status} · 已下载二进制响应 (${blob.size} bytes)`;}}catch(e){$('apiResponse').textContent=e.message;throw e;}};if(method==='GET')await run();else confirmAction('执行接口请求',`${method} ${path} 将直接作用于 本地服务 的真实数据。`,run);});
async function init(){const config=await(await fetch('/config')).json();S.nativeBase=config.base;S.drawingRecognitionMethod=config.drawing_recognition_method;$('nativeModels').onclick=()=>document.getElementById('modelConnectionForm').scrollIntoView({behavior:'smooth'});if(!config.configured)throw Error('本地服务尚未就绪');const results=await Promise.allSettled([refreshKbs(),data('GET','/api/v1/chats?page_size=100'),data('GET','/api/v1/models'),data('GET','/api/v1/models/default')]);const errors=results.map((r,i)=>r.status==='rejected'?['知识库','助手','模型','默认模型'][i]+'：'+r.reason.message:null).filter(Boolean);if(results[1].status==='fulfilled')S.chats=arrayOf(results[1].value);if(results[2].status==='fulfilled')S.models=arrayOf(results[2].value);if(results[3].status==='fulfilled')S.defaults=arrayOf(results[3].value);renderModels();renderAssistants();if(!$('assistantSelect').value){const view=loadLocal('lift-collab-view',{});$('assistantSelect').value=S.chats.some(chat=>chat.id===view.chat_id)?view.chat_id:S.chats[0]?.id||'';}$('connection').textContent=errors.length?'部分接口不可用':config.llm_configured?'服务已连接':'服务已连接 · 待配置模型';await Promise.allSettled([loadQuick(),loadDocs(),changeAssistant()]).then(rs=>rs.forEach(r=>{if(r.status==='rejected')errors.push(r.reason.message);}));if(errors.length)toast(errors.join('；'),true);}
bind('reconnect',()=>{if(S.chatAbort||S.reviewAbort)throw Error('请先停止当前生成任务');return init();});busy(null,init).then(()=>restoreWorkflow()).catch(error=>toast('后台任务重连失败：'+error.message,true));fetch('/endpoints.json').then(r=>r.json()).then(list=>{list.push({name:'列出模型',method:'GET',path:'/api/v1/models'},{name:'读取默认模型',method:'GET',path:'/api/v1/models/default'});setOptions($('endpointPreset'),list.map((r,i)=>({value:String(i),label:`${r.method} ${r.name} · ${r.path}`})),'自定义请求');$('endpointPreset').onchange=e=>{const r=list[e.target.value];if(r){$('apiMethod').value=r.method;$('apiPath').value=r.path;$('apiBody').value='';}};}).catch(e=>toast(e.message,true));
let polling=false;setInterval(async()=>{if(polling||document.hidden||S.page!=='knowledge'||!S.docs.some(d=>d.run==='RUNNING'))return;polling=true;try{await loadDocs();}catch(e){toast('刷新解析进度失败：'+e.message,true);}finally{polling=false;}},5000);

for(const input of [$('quickUpload'),$('kbUpload'),$('kbFolderUpload')]){const label=input.closest('label');label.tabIndex=0;label.setAttribute('role','button');label.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();input.click();}};}

// Collaborative review explicit message focus.
(()=>{
 const messages=$('chatMessages');
 let selectedMessage=null;
 function syncFocus(){
  const cards=[...messages.querySelectorAll('.message')];
  if(!cards.some(c=>c._message===selectedMessage))selectedMessage=null;
  messages.toggleAttribute('data-has-focus',!!selectedMessage);
  for(const card of cards){
   card.tabIndex=0;card.title='点击突出此条对话；再次点击或按 Esc 取消';
   const active=!!selectedMessage&&card._message===selectedMessage;
   card.toggleAttribute('data-focused',active);
   if(active)card.setAttribute('aria-current','true');else card.removeAttribute('aria-current');
  }
 }
 function select(card){selectedMessage=selectedMessage===card._message?null:card._message;syncFocus();}
 messages.addEventListener('click',e=>{
  if(e.target.closest('button,a,input,select,textarea,summary,video,audio')||window.getSelection()?.toString())return;
  const card=e.target.closest('.message');if(card)select(card);else{selectedMessage=null;syncFocus();}
 });
 messages.addEventListener('keydown',e=>{
  if(e.key==='Escape'){selectedMessage=null;syncFocus();return;}
  if(e.target.matches('.message')&&['Enter',' '].includes(e.key)){e.preventDefault();select(e.target);}
 });
 new MutationObserver(syncFocus).observe(messages,{childList:true,subtree:true});syncFocus();

})();

// Conversation minimap: one tick per message, with a preview and direct jump.
(()=>{
 const messages=$('chatMessages'),shell=document.createElement('div'),nav=document.createElement('nav'),preview=document.createElement('div');
 shell.className='chat-scroll-shell';messages.before(shell);shell.append(messages);nav.className='chat-jump-nav';nav.setAttribute('aria-label','对话快速跳转');shell.append(nav);
 preview.className='chat-jump-preview';preview.hidden=true;preview.setAttribute('role','tooltip');preview.id='chatJumpPreview';shell.append(preview);
 let cards=[],buttons=[],queued=0,pinnedCard=null;
 function hide(){preview.hidden=true;}
 function show(i){const card=cards[i];if(!card)return;const role=card.classList.contains('user')?'我的问题':'AI 回复';const text=card.querySelector('.message-text .markdown')?.innerText||card.querySelector('.message-text')?.innerText||'正在回复…';preview.replaceChildren();const title=document.createElement('strong'),body=document.createElement('p');title.textContent=`${i+1} · ${role}`;body.textContent=text.slice(0,220)+(text.length>220?'…':'');preview.append(title,body);const state=card.querySelector('[data-review-state]')?.dataset.reviewState,note=card.querySelector('.message-review-note')?.textContent?.trim();if(state==='reviewed'||state==='important'||note){const remark=document.createElement('p');remark.className='chat-jump-remark';remark.dataset.reviewState=state||'none';remark.textContent=(state==='important'?'★ 重要对话 · ':state==='reviewed'?'✓ 已人工复核 · ':'')+(note||'无备注');preview.append(remark);}preview.hidden=false;const y=buttons[i].getBoundingClientRect().top-shell.getBoundingClientRect().top;preview.style.top=Math.max(8,Math.min(y-20,shell.clientHeight-preview.offsetHeight-8))+'px';}
 function current(){
  if(!cards.length)return;
  let closest=cards.indexOf(pinnedCard);
  if(closest<0){
   pinnedCard=null;
   const max=messages.scrollHeight-messages.clientHeight;
   if(max>0&&messages.scrollTop>=max-3)closest=cards.length-1;
   else{
    const bounds=messages.getBoundingClientRect(),composer=$('chatForm').getBoundingClientRect();
    const visibleBottom=Math.min(bounds.bottom,composer.top>bounds.top?composer.top:bounds.bottom);
    const line=bounds.top+Math.min(100,Math.max(0,visibleBottom-bounds.top)*.25);
    closest=0;let distance=Infinity;
    cards.forEach((c,i)=>{const r=c.getBoundingClientRect(),d=r.top<=line&&r.bottom>=line?0:Math.min(Math.abs(r.top-line),Math.abs(r.bottom-line));if(d<distance){closest=i;distance=d;}});
   }
  }
  buttons.forEach((b,i)=>{b.setAttribute('aria-current',i===closest?'true':'false');b.dataset.reviewState=cards[i]?.querySelector('[data-review-state]')?.dataset.reviewState||'none';});
  const tick=buttons[closest];
  if(tick){const r=tick.getBoundingClientRect(),n=nav.getBoundingClientRect();if(r.top<n.top)nav.scrollTop-=n.top-r.top;else if(r.bottom>n.bottom)nav.scrollTop+=r.bottom-n.bottom;}
 }

 function refresh(){queued=0;const next=[...messages.querySelectorAll('.message')];if(next.length!==cards.length||next.some((c,i)=>c!==cards[i])){cards=next;hide();nav.replaceChildren();buttons=cards.map((card,i)=>{const b=document.createElement('button');b.type='button';b.className='chat-jump-tick';b.setAttribute('aria-label',`跳转至第 ${i+1} 条${card.classList.contains('user')?'提问':'回复'}`);b.setAttribute('aria-describedby','chatJumpPreview');b.addEventListener('pointerenter',()=>show(i));b.addEventListener('pointerleave',hide);b.addEventListener('focus',()=>show(i));b.addEventListener('blur',hide);b.addEventListener('click',()=>{pinnedCard=card;current();const offset=card.getBoundingClientRect().top-messages.getBoundingClientRect().top+messages.scrollTop-20;messages.scrollTo({top:Math.max(0,offset),behavior:matchMedia('(prefers-reduced-motion:reduce)').matches?'instant':'smooth'});hide();});nav.append(b);return b;});}nav.hidden=!cards.length;current();}
 new MutationObserver(()=>{if(!queued)queued=requestAnimationFrame(refresh);}).observe(messages,{childList:true,subtree:true,characterData:true});
 // Explicit selection wins until the user deliberately scrolls again.
 const unpin=()=>{pinnedCard=null;};
 messages.addEventListener('wheel',unpin,{passive:true});messages.addEventListener('touchmove',unpin,{passive:true});
 messages.addEventListener('keydown',e=>{if(['ArrowUp','ArrowDown','PageUp','PageDown','Home','End',' '].includes(e.key)&&!e.target.matches('.message'))unpin();});
 document.addEventListener('pointerdown',e=>{if(e.target.closest('[data-scroll-owner="chatMessages"]'))unpin();},true);
 document.addEventListener('keydown',e=>{if(e.target.closest('[data-scroll-owner="chatMessages"]'))unpin();},true);
 new MutationObserver(()=>{pinnedCard=messages.querySelector('.message[data-focused]');current();}).observe(messages,{subtree:true,attributes:true,attributeFilter:['data-focused']});
 new ResizeObserver(current).observe(messages);
 messages.addEventListener('scroll',()=>{current();hide();},{passive:true});nav.addEventListener('keydown',e=>{if(e.key==='Escape')hide();});window.addEventListener('resize',()=>{current();hide();});refresh();
})();

// Reserve space for the floating composer, including user-resized text input.
(()=>{
 const composer=$('chatForm'),workspace=composer.closest('.chat-workspace');
 const resize=new ResizeObserver(()=>{if(composer.offsetHeight)workspace.style.setProperty('--composer-clearance',Math.ceil(composer.getBoundingClientRect().height+12)+'px');});
 resize.observe(composer);
})();

setupModelSettings({refresh:()=>init(),canSave:()=>!S.chatAbort&&!S.reviewAbort});
