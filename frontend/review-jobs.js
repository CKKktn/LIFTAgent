// The browser observes a durable backend task. Closing this observer never stops it.
const BUSY=new Set(['pending','running','stopping']);
const TERMINAL=new Set(['completed','stale']);
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export const isReviewJobBusy=job=>Boolean(job&&BUSY.has(job.status));
export function reviewJobAction(job){
  if(!job)return '';
  if(job.status==='waiting_confirmation')return '确认指标清单';
  if(job.status==='stopping')return '正在停止';
  if(job.status==='completed'&&job.can_retry)return '重试异常项';
  if(['stopped','failed'].includes(job.status)&&job.can_resume!==false)return '继续此任务';
  return '';
}
export class ReviewJobError extends Error{
  constructor(message,status=0){super(message);this.name='ReviewJobError';this.status=status;}
}
export class ReviewJobClient{
  constructor({fetchImpl=globalThis.fetch,EventSourceImpl=globalThis.EventSource,onState=()=>{},onConnection=()=>{},onError=()=>{},setTimer=setTimeout,clearTimer=clearTimeout,pollInterval=1800,idleInterval=8000}={}){
    this.fetch=(...args)=>fetchImpl.call(globalThis,...args);this.EventSource=EventSourceImpl;this.onState=onState;this.onConnection=onConnection;this.onError=onError;
    this.setTimer=(...args)=>setTimer.call(globalThis,...args);this.clearTimer=(...args)=>clearTimer.call(globalThis,...args);this.pollInterval=pollInterval;this.idleInterval=idleInterval;
    this.job=null;this.epoch=0;this.lastEventId=0;this.closed=true;this.pendingRefresh=null;
  }
  async request(method,path='',body){
    const response=await this.fetch('/api/v1/review-jobs'+path,{method,credentials:'same-origin',headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
    let value;try{value=await response.json();}catch{throw new ReviewJobError('任务接口暂时没有返回有效数据，请稍后重连。',response.status);}
    if(!response.ok)throw new ReviewJobError(value.message||value.detail||('任务请求失败（'+response.status+'）'),response.status);
    return value;
  }
  async list(){const rows=await this.request('GET','?active=true');return Array.isArray(rows)?rows:rows?.jobs||rows?.items||[];}
  async create(payload){const job=await this.request('POST','',payload);return this.attach(job);}
  async attach(value){
    this.disconnect();const epoch=this.epoch;this.closed=false;
    try{
      const job=typeof value==='string'?await this.request('GET','/'+encodeURIComponent(value)):value;
      if(epoch!==this.epoch)return this.job;
      if(!job||typeof job.id!=='string'||!Number.isInteger(job.revision))throw new ReviewJobError('后台任务数据不完整，请重新打开任务。');
      this.job=null;this.lastEventId=0;this.accept(job);this.connect();this.schedule();return this.job;
    }catch(error){if(epoch===this.epoch)this.disconnect();throw error;}
  }
  accept(job){
    if(!job||typeof job.id!=='string'||!Number.isInteger(job.revision))return false;
    if(this.job&&job.id===this.job.id&&job.revision<this.job.revision)return false;
    if(this.job&&job.id!==this.job.id)return false;
    this.job=job;this.lastEventId=Math.max(this.lastEventId,Number(job.last_event_id)||0);
    this.onState(job);
    if(TERMINAL.has(job.status)){this.source?.close();this.source=null;}
    return true;
  }
  async refresh(){
    if(!this.job||this.closed)return this.job;
    if(this.pendingRefresh)return this.pendingRefresh;
    const id=this.job.id,epoch=this.epoch;
    const task=(async()=>{try{
      const next=await this.request('GET','/'+encodeURIComponent(id));
      if(epoch===this.epoch&&!this.closed&&next.id===id){this.accept(next);this.onConnection(true);}
      return next;
    }finally{if(this.pendingRefresh===task)this.pendingRefresh=null;}})();
    this.pendingRefresh=task;return task;
  }
  schedule(){
    if(this.closed||!this.job)return;
    this.clearTimer(this.timer);
    // Periodic reads are a fallback for SSE loss and another tab's commands.
    this.timer=this.setTimer(async()=>{this.timer=null;try{await this.refresh();}catch(error){this.onConnection(false,error);if(error.status===401||error.status===404){this.onError(error);this.disconnect();return;}}finally{this.schedule();}},isReviewJobBusy(this.job)?this.pollInterval:this.idleInterval);
  }
  connect(){
    if(this.closed||!this.job||!this.EventSource||TERMINAL.has(this.job.status))return;
    this.source?.close();const epoch=this.epoch;
    const source=this.source=new this.EventSource('/api/v1/review-jobs/'+encodeURIComponent(this.job.id)+'/events?after='+this.lastEventId,{withCredentials:true});
    source.onopen=()=>{if(epoch===this.epoch)this.onConnection(true);};
    source.onerror=()=>{if(epoch===this.epoch&&!this.closed)this.onConnection(false);};
    const update=event=>{
      if(epoch!==this.epoch||this.closed)return;
      this.lastEventId=Math.max(this.lastEventId,Number(event.lastEventId)||0);
      try{const value=JSON.parse(event.data),job=value.job||value;if(job.id===this.job.id&&job.report&&Number.isInteger(job.revision))this.accept(job);else this.refresh().catch(error=>this.onConnection(false,error));}catch{this.refresh().catch(error=>this.onConnection(false,error));}
    };
    source.onmessage=update;for(const event of ['state','progress','update'])source.addEventListener(event,update);
  }
  async command(operation,body={}){
    if(!['resume','stop','retry'].includes(operation)||!this.job)throw new ReviewJobError('请先打开需要操作的任务。');
    const id=this.job.id,epoch=this.epoch,revision=body.revision??this.job.revision;
    try{
      const next=await this.request('POST','/'+encodeURIComponent(id)+'/'+operation,{...body,revision});
      if(epoch===this.epoch&&!this.closed&&next.id===id){this.accept(next);if(!this.source)this.connect();this.schedule();}
      return next;
    }catch(error){
      if(error.status===409){await this.refresh().catch(()=>{});throw new ReviewJobError('任务进度或修订版本已更新，已同步最新状态。请重新打开清单后再确认，或再次执行该操作。',409);}
      throw error;
    }
  }
  resume(requirements,revision){return this.command('resume',{...(requirements?{requirements}:{}),...(revision===undefined?{}:{revision})});}
  async stop(){
    const id=this.job?.id;
    try{return await this.command('stop');}
    catch(error){
      // A node can advance between the click and the stop command. Only this
      // idempotent stop may retry once; human confirmation is never replayed.
      if(error.status===409&&this.job?.id===id&&isReviewJobBusy(this.job))return this.command('stop');
      throw error;
    }
  }
  retry(){return this.command('retry');}
  graph(id=this.job?.id){return this.request('GET',id?'/'+encodeURIComponent(id)+'/graph':'/graph');}
  disconnect(){this.epoch++;this.closed=true;this.source?.close();this.source=null;this.clearTimer(this.timer);this.timer=null;this.pendingRefresh=null;}
}

function graphParts(graph){
  const raw=Array.isArray(graph?.nodes)?graph.nodes:[];
  const nodes=raw.map(node=>typeof node==='string'?{id:node,label:node}:{id:String(node.id??node.name??''),label:String(node.label??node.name??node.id??'')}).filter(node=>node.id);
  const known=new Set(nodes.map(node=>node.id));
  const edges=(Array.isArray(graph?.edges)?graph.edges:[]).filter(edge=>known.has(String(edge.source))&&known.has(String(edge.target))).map(edge=>({source:String(edge.source),target:String(edge.target),label:String(edge.label??edge.data??''),conditional:Boolean(edge.conditional)}));
  return {nodes,edges};
}
const STAGES=[
  {id:'prepare',title:'固定审查输入',tag:'任务准备',description:'记录合同、知识库范围与模型版本。'},
  {id:'extract',title:'提取技术要求',tag:'LangChain 提取',description:'分批提取指标，核验原文、过滤与去重。'},
  {id:'confirm',title:'人工核对确认',tag:'人工参与',description:'等待你修订清单，确认后再开始核查。'},
  {id:'check',title:'按类型逐项核查',tag:'证据检索与判定',description:'进入文档、数值或功能分支，必要时补检索。'},
  {id:'report',title:'整理并保存报告',tag:'报告输出',description:'保留原始判定与证据，校验并保存报告。'},
  {id:'finish',title:'结束或单独重试',tag:'任务收尾',description:'完成后保留结果，可选择重试异常项。'},
  {id:'other',title:'其他编译节点',tag:'扩展节点',description:'当前编译结构中的其他节点。'}
];
const EVENT_NAMES={created:'创建任务',runner_started:'开始后台执行',command:'接收人工操作',completed:'保存最终报告',completed_with_retry:'保存报告，异常可单独重试',stopped:'保存停止位置',paused:'保存暂停位置',service_restart:'服务重启后保留进度',service_shutdown:'服务关闭前保留进度',failed:'记录处理异常',materials_changed:'核对资料版本变更',waiting_confirmation:'等待人工确认',incomplete:'记录未完成事项',checkpoint_projection:'同步检查点与报告'};
const STATUS_NAMES={pending:'等待后台执行',running:'后台执行中',waiting_confirmation:'等待人工确认',stopping:'正在停止',stopped:'已停止，可继续',failed:'存在处理异常',completed:'本轮处理完成',stale:'资料或配置已变更'};
const NODE_NOTES={
 initialize:'固定本次使用的合同、资料范围和模型版本，后续步骤持续核对一致性。',
 select_batch:'选择尚未完成的原文批次；已成功的批次继续保留。',
 extract_batch:'由 LangChain 调用模型提取技术要求，再检查输出结构和原文来源。',
 merge_batch:'合并有效要求、筛除超出技术范围的内容，并去除重复项。',
 split_or_retry:'大批次失败时拆小一次；叶批次最多尝试两次，限制重复调用。',
 record_batch_issue:'记录该批异常，保留其他批次已经取得的有效要求。',
 extraction_finish:'保存提取清单；存在有效要求时进入人工确认，否则结束本轮。',
 await_confirmation:'工作流在这里暂停。关闭窗口仍会等待，提交确认后才继续。',
 validate_confirmation:'检查人工修改后的类型、条件和原始来源，并再次核对材料版本。',
 select_requirement:'分派下一条已确认要求，按文档、数值或功能类型选择路线。',
 document_check:'核对资料清单中的技术文件名称；文件存在不等于内容或验收合格。',
 numeric_retrieve:'从限定范围检索数值方案证据，排除作为要求依据的合同文件。',
 functional_retrieve:'从限定范围检索功能实现描述，排除作为要求依据的合同文件。',
 judge_numeric:'比较参数、单位、阈值和适用条件；方案符合与实测达标分别表述。',
 judge_functional:'核对具体实现机制，区分已经实现的描述与未来计划或承诺。',
 assess_evidence:'检查当前证据与判定，决定保存结果还是在预算内补充检索。',
 supplement_retrieval:'结合证据缺口补检索，数值和功能核查各最多两轮检索。',
 item_failure:'保留本项异常，供人工复核或单独重试，其他成功结果继续保留。',
 save_item:'按要求编号更新结果与引文，恢复执行时避免重复追加条目。',
 checking_done:'逐项核查结束，转入报告整理。',
 collect_report:'核对版本并固定原始判定、分析及引用，作为报告整理的依据。',
 polish_one:'按批次整理展示内容，校验条目、判定、数字和引用；失败时有限重试。',
 polish_fallback:'整理仍未通过时使用原始分析，保留原判定和原始证据。',
 advance_report:'继续处理尚未完成的报告批次，已成功批次可复用。',
 finalize_report:'保存报告、范围和处理异常，标记可单独重试的内容。',
 await_retry:'报告已保存，在这里等待用户选择是否重试异常项。',
 restart_retry:'保留成功核查结果，只重新处理异常项或失败的报告整理批次。',
 __start__:'当前图或子图的入口。',__end__:'本轮工作流的结束位置。'
};
const leaf=id=>String(id).split(':').at(-1);
const eventNode=event=>String(event.node||event.node_name||event.name||'').replaceAll('/',':');
function stageFor(id){
 if(id.startsWith('extract_requirements:')||id==='extract_requirements')return 'extract';
 if(id.startsWith('check_requirements:')||id==='check_requirements')return 'check';
 if(id.startsWith('report_workflow:')||id==='report_workflow')return 'report';
 if(['__start__','initialize'].includes(id))return 'prepare';
 if(['await_confirmation','validate_confirmation'].includes(id))return 'confirm';
 if(['await_retry','restart_retry','__end__'].includes(id))return 'finish';
 return 'other';
}
export function workflowViewModel(graph){
 const parts=graphParts(graph),trace=(Array.isArray(graph?.trace)?graph.trace:Array.isArray(graph?.events)?graph.events:[]).filter(event=>event&&typeof event==='object');
 const visited=new Set(trace.map(eventNode));
 const nodes=parts.nodes.map((node,index)=>({...node,code:'N'+String(index+1).padStart(2,'0'),stage:stageFor(node.id),visited:visited.has(node.id),note:NODE_NOTES[leaf(node.id)]||'该节点及其连接来自当前后端编译结构。'}));
 const byId=new Map(nodes.map(node=>[node.id,node]));
 const stages=STAGES.map(stage=>({...stage,nodes:nodes.filter(node=>node.stage===stage.id)})).filter(stage=>stage.nodes.length);
 return {nodes,edges:parts.edges,trace,byId,stages,visitedCount:nodes.filter(node=>node.visited).length};
}
function truncationNotice(graph,model){return graph?.trace_truncated?`<p class="wf-notice">仅显示前 ${model.trace.length} / ${esc(graph.trace_total??'未知')} 条执行记录；后续节点未显示不代表未执行。</p>`:'';}
function dateTime(value){if(!value)return '未记录时间';const date=new Date(value);return Number.isNaN(date.valueOf())?String(value):date.toLocaleString('zh-CN',{hour12:false});}
function routeCards(model){
 const routes=[['document_check','文档存在','核对技术文档名称','仅检查文件存在，不代表内容合格。'],['numeric_retrieve','数值指标','检索方案 → 比较参数与条件','区分方案设计与实测结果。'],['functional_retrieve','功能条件','检索实现描述 → 核对实现机制','区分具体实现与未来计划。']];
 return routes.filter(([key])=>model.nodes.some(node=>leaf(node.id)===key)).map(([key,title,path,note])=>`<button type="button" class="wf-route" data-wf-node="${esc(model.nodes.find(node=>leaf(node.id)===key).id)}"><strong>${title}<span aria-hidden="true">↗</span></strong><span>${path}</span><small>${note}</small></button>`).join('');
}
function overviewHtml(model){
 if(!model.nodes.length)return '<div class="wf-empty">当前未返回编译节点，可在执行记录中查看已有进度。</div>';
 const routes=routeCards(model);
 return `<div class="wf-section-heading"><div><h3>从输入到报告，一条清楚的主线</h3><p>按阶段归纳展示；点击任一阶段，查看它包含的实际节点与分支。</p></div><span class="wf-legend"><i></i>已有执行记录</span></div><div class="wf-stage-flow" style="--wf-stage-count:${model.stages.length}">${model.stages.map((stage,index)=>`<button type="button" class="wf-stage-card ${stage.nodes.some(node=>node.visited)?'has-events':''}" data-wf-stage="${stage.id}"><span class="wf-stage-top"><b>${String(index+1).padStart(2,'0')}</b><small>${esc(stage.tag)}</small></span><strong>${esc(stage.title)}</strong><span class="wf-stage-description">${esc(stage.description)}</span><span class="wf-stage-bottom">${stage.nodes.length} 个实际节点<span>查看阶段 →</span></span></button>`).join('')}</div>${routes?`<div class="wf-section-heading wf-route-heading"><h3>逐项核查按要求类型分派</h3><span>每条要求进入相应分支</span></div><div class="wf-routes">${routes}</div>`:''}<div class="wf-safeguards"><details><summary>有限重试与补检索</summary><span>仅在相应条件成立时进入，预算耗尽保留不确定性。</span></details><details><summary>人工确认与暂停恢复</summary><span>确认前等待；正常停止后可从已保存的检查点继续。</span></details><details><summary>结果与来源持续保留</summary><span>异常项单独处理，成功结果和原始证据继续保留。</span></details></div><p class="wf-footnote">绿色仅表示节点出现过执行记录。是否满足要求，请以审查报告中的逐项判定为准。</p>`;
}
function connectionHtml(edge,model,direction){
 const node=model.byId.get(direction==='in'?edge.source:edge.target);
 return `<button type="button" class="wf-connection" data-wf-node="${esc(node.id)}"><span class="wf-node-code">${node.code}</span><span><strong>${esc(node.label)}</strong><small>${esc(edge.label||(edge.source===edge.target?'在本节点重试':edge.conditional?'按状态选择分支':'顺序连接'))}</small></span><span aria-hidden="true">${direction==='in'?'←':'→'}</span></button>`;
}
function detailHtml(model,stageId,nodeId){
 const stage=model.stages.find(item=>item.id===stageId)||model.stages[0];if(!stage)return '<div class="wf-empty">暂无节点结构。</div>';
 const node=model.byId.get(nodeId)?.stage===stage.id?model.byId.get(nodeId):stage.nodes.find(item=>!leaf(item.id).startsWith('__'))||stage.nodes[0];
 const incoming=model.edges.filter(edge=>edge.target===node.id),outgoing=model.edges.filter(edge=>edge.source===node.id);
 return `<nav class="wf-stage-nav" aria-label="流程阶段">${model.stages.map((item,index)=>`<button type="button" data-wf-stage="${item.id}" aria-current="${item.id===stage.id?'step':'false'}"><b>${String(index+1).padStart(2,'0')}</b><span>${esc(item.title)}<small>${item.nodes.length} 个节点</small></span></button>`).join('')}</nav><section class="wf-node-catalog"><div class="wf-section-heading"><div><h3>${esc(stage.title)}</h3><p>${esc(stage.description)}</p></div></div><p class="wf-footnote">选择节点，在右侧查看职责及真实前后连接。</p><div class="wf-node-list">${stage.nodes.map(item=>`<button type="button" class="wf-node ${item.id===node.id?'is-selected':''} ${item.visited?'has-events':''}" data-wf-node="${esc(item.id)}" aria-pressed="${item.id===node.id}"><span class="wf-node-code">${item.code}</span><span><strong>${esc(item.label)}</strong><small>${item.visited?'已有执行记录':'尚无执行记录'}</small></span><span aria-hidden="true">›</span></button>`).join('')}</div></section><aside class="wf-node-details" aria-label="选中节点详情"><div class="wf-detail-head"><span class="wf-eyebrow">${node.code} · 节点职责</span><h3>${esc(node.label)}</h3><p>${esc(node.note)}</p></div><section><h4>从哪里来 <small>${incoming.length} 条</small></h4>${incoming.map(edge=>connectionHtml(edge,model,'in')).join('')||'<p class="wf-footnote">当前编译结构中没有前置连接。</p>'}</section><section><h4>接下来去哪里 <small>${outgoing.length} 条</small></h4>${outgoing.map(edge=>connectionHtml(edge,model,'out')).join('')||'<p class="wf-footnote">当前编译结构中没有后续连接。</p>'}</section><details class="wf-node-source"><summary>查看代码节点标识</summary><code>${esc(node.id)}</code></details></aside>`;
}
function recordsHtml(model,stage='all',query=''){
 const rows=model.trace.map((event,index)=>({event,index,node:model.byId.get(eventNode(event))})).filter(({event,node})=>(stage==='all'||(node?.stage||'operations')===stage)&&(!query||[EVENT_NAMES[event.node],event.label,event.node,event.message,event.phase].filter(Boolean).join(' ').toLowerCase().includes(query.toLowerCase()))).reverse();
 return `<div class="wf-record-count" role="status">显示 ${rows.length} / ${model.trace.length} 条已载入记录 · 时间从新到旧</div>${rows.length?`<ol class="wf-timeline">${rows.map(({event,index,node})=>`<li><span class="wf-event-index">${index+1}</span><div><div class="wf-event-heading"><strong>${esc(EVENT_NAMES[event.node]||node?.label||event.label||event.node||'进度更新')}</strong><time>${esc(dateTime(event.created_at||event.timestamp||event.time))}</time></div><p>${esc(event.message||event.phase||event.status||'已记录此步骤')}</p><span class="wf-event-stage">${esc(model.stages.find(stage=>stage.id===node?.stage)?.title||'任务操作')}</span></div></li>`).join('')}</ol>`:'<div class="wf-empty">暂无匹配的执行记录。结构预览可以先查看流程总览和节点详图。</div>'}`;
}
export function workflowGraphHtml(graph){
 const model=workflowViewModel(graph),latest=graph?.trace_truncated?null:model.trace.at(-1),job=graph?.job;
 const status=job?.status||latest?.status;
 const caption=job?.message||latest?.message||(graph?.trace_truncated?'已载入部分轨迹，详情见执行记录。':'结构来自当前后端实际编译的 LangGraph。');
 return `<div class="workflow-inspector"><div class="wf-summary"><div class="wf-task-summary"><span class="wf-eyebrow">${graph?.task_id?'本次任务':'工作流结构预览'}</span><strong>${esc(STATUS_NAMES[status]||'快速审查工作流')}</strong><p>${esc(caption)}</p></div><div class="wf-metrics"><div><b>${model.nodes.length}</b><span>实际节点</span></div><div><b>${model.edges.length}</b><span>真实连接</span></div><div><b>${model.visitedCount}</b><span>有记录节点</span></div></div></div><div class="wf-tabs" role="tablist" aria-label="审查流程视图">${[['overview','流程总览'],['nodes','节点详图'],['records','执行记录']].map(([key,label],index)=>`<button type="button" id="wf-tab-${key}" role="tab" aria-controls="wf-pane-${key}" aria-selected="${!index}" tabindex="${index?-1:0}" data-wf-tab="${key}">${label}${key==='records'?`<span>${model.trace.length}</span>`:''}</button>`).join('')}</div><div class="wf-workspace-content"><section id="wf-pane-overview" role="tabpanel" aria-labelledby="wf-tab-overview" class="wf-pane wf-overview">${overviewHtml(model)}</section><section id="wf-pane-nodes" role="tabpanel" aria-labelledby="wf-tab-nodes" class="wf-pane wf-details" hidden>${detailHtml(model,'extract')}</section><section id="wf-pane-records" role="tabpanel" aria-labelledby="wf-tab-records" class="wf-pane wf-records" hidden>${truncationNotice(graph,model)}<div class="wf-record-tools"><label>记录范围<select data-wf-filter><option value="all">全部阶段与操作</option>${model.stages.map(stage=>`<option value="${stage.id}">${esc(stage.title)}</option>`).join('')}<option value="operations">任务操作</option></select></label><label>查找记录<input type="search" data-wf-search placeholder="搜索节点或进度说明"></label></div><div data-wf-record-list>${recordsHtml(model)}</div><details class="wf-technical"><summary>编译结构原文（Mermaid）</summary><pre class="workflow-mermaid">${esc(graph?.mermaid||'未返回流程原文')}</pre></details></section></div></div>`;
}
export function mountWorkflowInspector(container,graph){
 const root=container.querySelector('.workflow-inspector');if(!root)return ()=>{};
 const model=workflowViewModel(graph),controller=new AbortController(),options={signal:controller.signal};
 let stageId=model.stages.find(stage=>stage.id==='extract')?.id||model.stages[0]?.id,nodeId='';
 const activate=key=>{const previous=root.querySelector('.wf-pane:not([hidden])')?.id;for(const button of root.querySelectorAll('[data-wf-tab]')){const active=button.dataset.wfTab===key;button.setAttribute('aria-selected',String(active));button.tabIndex=active?0:-1;}for(const pane of root.querySelectorAll('.wf-pane'))pane.hidden=pane.id!=='wf-pane-'+key;if(previous!=='wf-pane-'+key)root.querySelector('.wf-workspace-content').scrollTop=0;};
 const showNode=()=>{root.querySelector('#wf-pane-nodes').innerHTML=detailHtml(model,stageId,nodeId);activate('nodes');const selected=root.querySelector(nodeId?'.wf-node.is-selected':'.wf-stage-nav [aria-current="step"]');selected?.focus({preventScroll:true});if(nodeId){if(root.clientWidth<850)root.querySelector('.wf-node-details')?.scrollIntoView({block:'start'});else selected?.scrollIntoView({block:'nearest'});}};
 root.addEventListener('click',event=>{
  const target=event.target.closest('button');if(!target||!root.contains(target))return;
  if(target.dataset.wfTab){activate(target.dataset.wfTab);return;}
  if(target.dataset.wfStage){stageId=target.dataset.wfStage;nodeId='';showNode();root.querySelector('.wf-workspace-content').scrollTop=0;}
  if(target.dataset.wfNode&&model.byId.has(target.dataset.wfNode)){nodeId=target.dataset.wfNode;stageId=model.byId.get(nodeId).stage;showNode();}
 },options);
 const filter=()=>{root.querySelector('[data-wf-record-list]').innerHTML=recordsHtml(model,root.querySelector('[data-wf-filter]').value,root.querySelector('[data-wf-search]').value.trim());};
 root.addEventListener('input',event=>{if(event.target.matches('[data-wf-search]'))filter();},options);
 root.addEventListener('change',event=>{if(event.target.matches('[data-wf-filter]'))filter();},options);
 root.addEventListener('keydown',event=>{
  if(event.key==='Enter'&&event.target.matches('input'))event.preventDefault();
  if(event.target.matches('[data-wf-tab]')&&['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){
   event.preventDefault();const tabs=[...root.querySelectorAll('[data-wf-tab]')],current=tabs.indexOf(event.target),index=event.key==='Home'?0:event.key==='End'?tabs.length-1:(current+(event.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;activate(tabs[index].dataset.wfTab);tabs[index].focus();
  }
 },options);
 return ()=>controller.abort();
}
function wrapSvg(value,limit=25){const characters=Array.from(String(value)),lines=[];for(let index=0;index<characters.length;index+=limit)lines.push(characters.slice(index,index+limit).join(''));return lines.length?lines:[''];}
function svgLines(lines,x,y,{size=16,color='#283a50',lineHeight=24,weight=400}={}){return `<text x="${x}" y="${y}" fill="${color}" font-size="${size}" font-weight="${weight}">${lines.map((line,index)=>`<tspan x="${x}" dy="${index?lineHeight:0}">${esc(line)}</tspan>`).join('')}</text>`;}
function svgShell(width,height,title,content){return `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(title)}" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Microsoft YaHei,sans-serif"><title>${esc(title)}</title><rect width="${width}" height="${height}" fill="#ffffff"/>${content}</svg>`;}
export function workflowOverviewSvg(graph){
 const model=workflowViewModel(graph),width=1440,columns=Math.min(3,Math.max(1,model.stages.length)),height=240+Math.ceil(model.stages.length/columns)*220;
 let content=svgLines(['快速审查 · 六阶段流程总览'],48,58,{size:28,weight:650})+svgLines([`依据实际编译结构归纳 · ${model.nodes.length} 个节点 · ${model.edges.length} 条连接`],48,94,{color:'#64748b'});
 model.stages.forEach((stage,index)=>{const x=48+(index%columns)*458,y=135+Math.floor(index/columns)*220,done=stage.nodes.some(node=>node.visited);content+=`<g data-members="${esc(JSON.stringify(stage.nodes.map(node=>node.id)))}"><rect x="${x}" y="${y}" width="420" height="180" rx="16" fill="${done?'#e5f5ec':'#f3f6fa'}" stroke="#cbd5e1"/>${svgLines([String(index+1).padStart(2,'0')+'  '+stage.title],x+22,y+38,{size:21,weight:650})}${svgLines(wrapSvg(stage.description,22),x+22,y+75,{size:16,color:'#526479'})}${svgLines([stage.nodes.length+' 个实际节点 · '+(done?'已有执行记录':'结构说明')],x+22,y+153,{size:14,color:'#64748b'})}</g>`;if(index%columns<columns-1&&index<model.stages.length-1)content+=svgLines(['→'],x+428,y+96,{size:26,color:'#8190a3'});});
 content+=svgLines(['按阶段归纳展示；全部节点、分支及回路见“完整节点与连接索引”。'],48,height-58,{size:16,color:'#526479'})+svgLines(['绿色表示已有执行记录；具体符合性以审查报告的判定为准。'],48,height-28,{size:14,color:'#64748b'});
 return svgShell(width,height,'快速审查流程总览',content);
}
export function workflowGraphSvg(graph){
 const model=workflowViewModel(graph);if(!model.nodes.length)return '';
 let y=144,content=svgLines(['快速审查 · 完整节点与连接索引'],48,58,{size:28,weight:650})+svgLines([`${model.nodes.length} 个实际节点 / ${model.edges.length} 条连接 · 按阶段分组，点击后续节点编号可定位；绿色表示已有执行记录。`],48,96,{size:15,color:'#64748b'});
 for(const [stageIndex,stage] of model.stages.entries()){
  content+=svgLines([String(stageIndex+1).padStart(2,'0')+'  '+stage.title],48,y,{size:23,weight:650});y+=24;
  for(let offset=0;offset<stage.nodes.length;offset+=3){
   const row=stage.nodes.slice(offset,offset+3),cards=row.map(node=>{const outgoing=model.edges.filter(edge=>edge.source===node.id),title=wrapSvg(node.code+'  '+node.label,20),note=wrapSvg(node.note,25),links=outgoing.map(edge=>({edge,lines:wrapSvg('→ '+model.byId.get(edge.target).code+' · '+model.byId.get(edge.target).label+(edge.conditional?'（分支）':''),26)}));return {node,title,note,links,height:88+title.length*26+note.length*22+links.reduce((sum,item)=>sum+item.lines.length*22+10,0)};});
   const rowHeight=Math.max(...cards.map(card=>card.height));
   cards.forEach((card,index)=>{const x=48+index*458;let textY=y+34;content+=`<g id="wf-node-${card.node.code}" data-node-id="${esc(card.node.id)}"><rect x="${x}" y="${y}" width="430" height="${rowHeight}" rx="12" fill="${card.node.visited?'#e5f5ec':'#f3f6fa'}" stroke="#cbd5e1"/>${svgLines(card.title,x+18,textY,{size:18,weight:650,lineHeight:26})}`;textY+=card.title.length*26+12;content+=svgLines(card.note,x+18,textY,{size:15,color:'#526479',lineHeight:22});textY+=card.note.length*22+18;for(const {edge,lines} of card.links){content+=`<a href="#wf-node-${model.byId.get(edge.target).code}" data-source="${esc(edge.source)}" data-target="${esc(edge.target)}">${svgLines(lines,x+18,textY,{size:14,color:'#2563a0',lineHeight:22})}</a>`;textY+=lines.length*22+10;}if(!card.links.length)content+=svgLines(['没有后续连接'],x+18,textY,{size:14,color:'#64748b'});content+='</g>';});
   y+=rowHeight+18;
  }
  y+=30;
 }
 return svgShell(1440,y+24,'实际编译的快速审查工作流：完整节点与连接索引',content);
}

let academicLayoutPromise;
function loadAcademicLayout(){
 const ready=globalThis.dagre?.default||globalThis.dagre;
 if(ready?.graphlib&&ready?.layout)return Promise.resolve(ready);
 if(!academicLayoutPromise)academicLayoutPromise=new Promise((resolve,reject)=>{
  const script=document.createElement('script');script.src=new URL('./vendor/dagre/dagre.min.js',import.meta.url).href;script.async=true;
  script.onload=()=>{const value=globalThis.dagre?.default||globalThis.dagre;if(value?.layout&&value?.graphlib)resolve(value);else{academicLayoutPromise=null;script.remove();reject(new Error('流程图排版组件未能加载，请刷新后重试。'));}};
  script.onerror=()=>{academicLayoutPromise=null;script.remove();reject(new Error('流程图本地资源加载失败，请刷新后重试。'));};document.head.append(script);
 });
 return academicLayoutPromise;
}
function academicLines(value,maxUnits=15){
 const lines=[];let line='',width=0;
 for(const character of String(value)){const units=/[\u2e80-\uffff]/u.test(character)?1:.56;if(width+units>maxUnits&&line){lines.push(line);line='';width=0;}line+=character;width+=units;}
 if(line||!lines.length)lines.push(line);return lines;
}
function academicLabel(node){
 if(node.id==='__start__')return '主流程开始';
 if(node.id==='__end__')return '主流程结束';
 if(leaf(node.id)==='__start__')return ({extract:'提取子图入口',check:'核查子图入口',report:'报告子图入口'})[node.stage]||'子图入口';
 return node.label;
}
function roundedAcademicPath(points){
 if(!points.length)return '';
 const number=value=>Number(value.toFixed(2)),point=p=>`${number(p.x)},${number(p.y)}`;
 let path='M'+point(points[0]);
 for(let index=1;index<points.length-1;index++){
  const a=points[index-1],b=points[index],c=points[index+1],before=Math.hypot(b.x-a.x,b.y-a.y),after=Math.hypot(c.x-b.x,c.y-b.y),radius=Math.min(10,before/3,after/3);
  if(!before||!after){path+=' L'+point(b);continue;}
  const enter={x:b.x-(b.x-a.x)*radius/before,y:b.y-(b.y-a.y)*radius/before},leave={x:b.x+(c.x-b.x)*radius/after,y:b.y+(c.y-b.y)*radius/after};
  path+=' L'+point(enter)+' Q'+point(b)+' '+point(leave);
 }
 if(points.length>1)path+=' L'+point(points.at(-1));return path;
}
function academicSegmentHits(a,b,box,padding=6){
 const left=box.x-box.width/2-padding,right=box.x+box.width/2+padding,top=box.y-box.height/2-padding,bottom=box.y+box.height/2+padding;
 let low=0,high=1;
 for(const [origin,delta,min,max] of [[a.x,b.x-a.x,left,right],[a.y,b.y-a.y,top,bottom]]){
  if(Math.abs(delta)<1e-8){if(origin<=min||origin>=max)return false;continue;}
  const first=(min-origin)/delta,last=(max-origin)/delta;low=Math.max(low,Math.min(first,last));high=Math.min(high,Math.max(first,last));if(low>=high)return false;
 }
 return low<1&&high>0;
}
function avoidAcademicNodes(points,sourceId,targetId,nodes){
 const boxes=[...nodes.entries()].filter(([id])=>!id.startsWith('academic-stage-')).map(([id,box])=>({id,...box}));
 const obstructed=points.some((point,index)=>index&&boxes.some(box=>box.id!==sourceId&&box.id!==targetId&&academicSegmentHits(points[index-1],point,box,7)));
 if(!obstructed||sourceId===targetId)return points;
 const escape=(point,box)=>{const choices=[{d:Math.abs(point.x-(box.x-box.width/2)),x:-1,y:0},{d:Math.abs(point.x-(box.x+box.width/2)),x:1,y:0},{d:Math.abs(point.y-(box.y-box.height/2)),x:0,y:-1},{d:Math.abs(point.y-(box.y+box.height/2)),x:0,y:1}].sort((a,b)=>a.d-b.d);for(const distance of [20,14,10,8]){const candidate={x:point.x+choices[0].x*distance,y:point.y+choices[0].y*distance};if(!boxes.some(other=>candidate.x>other.x-other.width/2-7&&candidate.x<other.x+other.width/2+7&&candidate.y>other.y-other.height/2-7&&candidate.y<other.y+other.height/2+7))return candidate;}return {x:point.x+choices[0].x*8,y:point.y+choices[0].y*8};};
 const start=escape(points[0],nodes.get(sourceId)),end=escape(points.at(-1),nodes.get(targetId)),unique=values=>[...new Set(values)].sort((a,b)=>a-b);
 const xs=unique([start.x,end.x,...boxes.flatMap(box=>[box.x-box.width/2-20,box.x,box.x+box.width/2+20])]),ys=unique([start.y,end.y,...boxes.flatMap(box=>[box.y-box.height/2-20,box.y,box.y+box.height/2+20])]),width=xs.length;
 const pointFor=id=>({x:xs[id%width],y:ys[Math.floor(id/width)]}),startId=ys.indexOf(start.y)*width+xs.indexOf(start.x),endId=ys.indexOf(end.y)*width+xs.indexOf(end.x);
 const blocked=new Uint8Array(width*ys.length);
 for(let index=0;index<blocked.length;index++){const point=pointFor(index);blocked[index]=boxes.some(box=>point.x>box.x-box.width/2-7&&point.x<box.x+box.width/2+7&&point.y>box.y-box.height/2-7&&point.y<box.y+box.height/2+7)?1:0;}
 const heap=[],push=item=>{let index=heap.length;heap.push(item);while(index){const parent=(index-1)>>1;if(heap[parent].score<=item.score)break;heap[index]=heap[parent];index=parent;}heap[index]=item;},pop=()=>{const first=heap[0],last=heap.pop();if(heap.length){let index=0;while(index*2+1<heap.length){let child=index*2+1;if(child+1<heap.length&&heap[child+1].score<heap[child].score)child++;if(heap[child].score>=last.score)break;heap[index]=heap[child];index=child;}heap[index]=last;}return first;};
 const distances=new Map(),previous=new Map(),initial=startId*3+2;distances.set(initial,0);push({key:initial,cost:0,score:Math.abs(start.x-end.x)+Math.abs(start.y-end.y)});
 let final;
 while(heap.length){
  const current=pop();if(current.cost!==distances.get(current.key))continue;
  const id=Math.floor(current.key/3),direction=current.key%3;if(id===endId){final=current.key;break;}
  const a=pointFor(id),x=id%width,y=Math.floor(id/width);
  for(const [nextX,nextY,nextDirection] of [[x-1,y,0],[x+1,y,0],[x,y-1,1],[x,y+1,1]]){
   if(nextX<0||nextX>=width||nextY<0||nextY>=ys.length)continue;const nextId=nextY*width+nextX;if(blocked[nextId])continue;
   const b=pointFor(nextId);if(boxes.some(box=>academicSegmentHits(a,b,box,7)))continue;
   const key=nextId*3+nextDirection,cost=current.cost+Math.abs(a.x-b.x)+Math.abs(a.y-b.y)+(direction!==2&&direction!==nextDirection?24:0);
   if(cost>=(distances.get(key)??Infinity))continue;distances.set(key,cost);previous.set(key,current.key);push({key,cost,score:cost+Math.abs(b.x-end.x)+Math.abs(b.y-end.y)});
  }
 }
 if(final===undefined)throw new Error('部分流程连接无法安全排版，请检查图结构后重试。');
 const route=[];for(let key=final;key!==undefined;key=previous.get(key))route.push(pointFor(Math.floor(key/3)));route.reverse();
 const joined=[points[0],...route,points.at(-1)],simplified=[];
 for(const point of joined){if(simplified.length&&Math.abs(point.x-simplified.at(-1).x)+Math.abs(point.y-simplified.at(-1).y)<1e-6)continue;while(simplified.length>1){const a=simplified.at(-2),b=simplified.at(-1);if(Math.abs((b.x-a.x)*(point.y-b.y)-(b.y-a.y)*(point.x-b.x))>1e-6)break;simplified.pop();}simplified.push(point);}
 return simplified;
}
function academicAutomaticLayout(model,library){
 const order={__start__:0,initialize:1,select_batch:1,extract_batch:2,merge_batch:3,split_or_retry:3,record_batch_issue:4,extraction_finish:5,await_confirmation:0,validate_confirmation:1,select_requirement:1,document_check:2,numeric_retrieve:2,functional_retrieve:2,judge_numeric:3,judge_functional:3,assess_evidence:4,supplement_retrieval:5,item_failure:5,save_item:6,checking_done:7,collect_report:0,polish_one:1,polish_fallback:2,advance_report:3,finalize_report:4,await_retry:0,restart_retry:1,__end__:2};
 const edgeId=index=>'E'+String(index+1).padStart(3,'0'),stages=new Map(),placedNodes=new Map(),placedEdges=new Map();
 const external=model.edges.map((edge,index)=>({...edge,id:edgeId(index)})).filter(edge=>model.byId.get(edge.source).stage!==model.byId.get(edge.target).stage);
 for(const stage of model.stages){
  const compact=['prepare','confirm','finish'].includes(stage.id);
  const graph=new library.graphlib.Graph({multigraph:true}).setGraph({rankdir:compact?'LR':'TB',ranksep:66,nodesep:48,edgesep:14,marginx:34,marginy:28,ranker:'network-simplex'}).setDefaultEdgeLabel(()=>({}));
  for(const node of stage.nodes){const lines=academicLines(academicLabel(node));graph.setNode(node.id,{width:264,height:52+lines.length*23,lines});}
  const feedback=[],maximum=Math.max(...stage.nodes.map(node=>order[leaf(node.id)]??0));
  model.edges.forEach((edge,index)=>{if(model.byId.get(edge.source).stage!==stage.id||model.byId.get(edge.target).stage!==stage.id)return;const delta=(order[leaf(edge.target)]??0)-(order[leaf(edge.source)]??0);if(delta>0)graph.setEdge(edge.source,edge.target,{weight:10,minlen:delta},edgeId(index));else feedback.push({...edge,id:edgeId(index)});});
  for(const edge of external){
   if(model.byId.get(edge.source).stage===stage.id){const port='academic-out-'+edge.id;graph.setNode(port,{width:2,height:2});graph.setEdge(edge.source,port,{weight:20,minlen:maximum-(order[leaf(edge.source)]??0)+1},edge.id+'-out');for(const node of stage.nodes)if(node.id!==edge.source)graph.setEdge(node.id,port,{weight:20,minlen:maximum-(order[leaf(node.id)]??0)+1},'guide-out-'+edge.id+'-'+node.code);}
   if(model.byId.get(edge.target).stage===stage.id){const port='academic-in-'+edge.id;graph.setNode(port,{width:2,height:2});graph.setEdge(port,edge.target,{weight:20,minlen:(order[leaf(edge.target)]??0)+1},edge.id+'-in');for(const node of stage.nodes)if(node.id!==edge.target)graph.setEdge(port,node.id,{weight:20,minlen:(order[leaf(node.id)]??0)+1},'guide-in-'+edge.id+'-'+node.code);}
  }
  library.layout(graph);const railPadding=feedback.filter(edge=>edge.source!==edge.target).length*20+20;
  for(const id of graph.nodes())graph.node(id).x+=railPadding;
  for(const edge of graph.edges())for(const point of graph.edge(edge).points)point.x+=railPadding;
  graph.graph().width+=railPadding;
  feedback.forEach((edge,index)=>{const source=graph.node(edge.source),target=graph.node(edge.target);let points;if(edge.source===edge.target)points=[{x:source.x+source.width/2,y:source.y+10},{x:source.x+source.width/2+24,y:source.y+10},{x:source.x+source.width/2+24,y:source.y-source.height/2-18},{x:source.x+source.width/4,y:source.y-source.height/2-18},{x:source.x+source.width/4,y:source.y-source.height/2}];else{const rail=14+index*18,sourceY=source.y-source.height/2-18,targetY=target.y+target.height/2+18;points=[{x:source.x,y:source.y-source.height/2},{x:source.x,y:sourceY},{x:rail,y:sourceY},{x:rail,y:targetY},{x:target.x,y:targetY},{x:target.x,y:target.y+target.height/2}];}graph.setEdge(edge.source,edge.target,{points},edge.id);});
  stages.set(stage.id,{stage,graph});
 }
 const panelWidth=Math.max(...[...stages.values()].map(item=>item.graph.graph().width))+64;
 let cursor=0;
 for(const item of stages.values()){
  const dimensions=item.graph.graph(),height=dimensions.height+70;item.x=(panelWidth-dimensions.width)/2;item.y=cursor+44;item.top=cursor;item.bottom=cursor+height;
  placedNodes.set('academic-stage-'+item.stage.id,{x:panelWidth/2,y:cursor+height/2,width:panelWidth,height});
  for(const node of item.stage.nodes){const box=item.graph.node(node.id);placedNodes.set(node.id,{...box,x:box.x+item.x,y:box.y+item.y});}
  cursor+=height+82;
 }
 const shifted=(points,item)=>points.map(point=>({x:point.x+item.x,y:point.y+item.y}));
 let railCount=0;
 model.edges.forEach((edge,index)=>{
  const id=edgeId(index),source=stages.get(model.byId.get(edge.source).stage),target=stages.get(model.byId.get(edge.target).stage);
  if(source===target){placedEdges.set(id,{points:shifted(source.graph.edge({v:edge.source,w:edge.target,name:id}).points,source)});return;}
  const out=shifted(source.graph.edge({v:edge.source,w:'academic-out-'+id,name:id+'-out'}).points,source),incoming=shifted(target.graph.edge({v:'academic-in-'+id,w:edge.target,name:id+'-in'}).points,target),a=out.at(-1),b=incoming[0];
  const adjacent=model.stages.findIndex(stage=>stage.id===target.stage.id)===model.stages.findIndex(stage=>stage.id===source.stage.id)+1;
  let middle;
  if(adjacent){const y=(source.bottom+target.top)/2;middle=[{x:a.x,y},{x:b.x,y}];}
  else{const rail=panelWidth+32+railCount++*26;middle=[{x:a.x,y:source.bottom+22},{x:rail,y:source.bottom+22},{x:rail,y:target.top-22},{x:b.x,y:target.top-22}];}
  placedEdges.set(id,{points:[...out,...middle,...incoming]});
 });
 model.edges.forEach((edge,index)=>{const id=edgeId(index),placed=placedEdges.get(id);placed.points=avoidAcademicNodes(placed.points,edge.source,edge.target,placedNodes);});
 return {graph:()=>({width:panelWidth+58+railCount*26,height:cursor-82}),node:id=>placedNodes.get(id),edge:edge=>placedEdges.get(edge.name)};
}
function academicLayeredLayout(model,library){
 // Known workflow stages share one compact coordinate system. This removes the
 // virtual-port spacing of six independent graphs without changing any edge.
 const C=700,L=390,R=1010,plans={
  prepare:[[['__start__',L],['initialize',C]]],
  extract:[[['__start__',C]],[['select_batch',C]],[['extract_batch',C]],[['merge_batch',L],['split_or_retry',R]],[['extraction_finish',C],['record_batch_issue',R]]],
  confirm:[[['await_confirmation',C],['validate_confirmation',R]]],
  check:[[['__start__',C]],[['select_requirement',C]],[['document_check',L],['numeric_retrieve',C],['functional_retrieve',R]],[['judge_numeric',C],['judge_functional',R]],[['item_failure',L],['assess_evidence',C],['supplement_retrieval',R]],[['save_item',C],['checking_done',R]]],
  report:[[['collect_report',C]],[['polish_one',C]],[['advance_report',C],['polish_fallback',R]],[['finalize_report',C]]],
  finish:[[['await_retry',C]],[['__end__',C],['restart_retry',R]]]
 };
 if(model.stages.some(stage=>!plans[stage.id]||new Set(stage.nodes.map(node=>leaf(node.id))).size!==stage.nodes.length||stage.nodes.some(node=>!plans[stage.id].flat().some(([key])=>key===leaf(node.id)))))return academicAutomaticLayout(model,library);
 const nodes=new Map(),edges=new Map();let cursor=0;
 for(const stage of model.stages){
  const top=cursor;cursor+=14;
  for(const row of plans[stage.id]){
   const entries=row.map(([key,x])=>({node:stage.nodes.find(node=>leaf(node.id)===key),x})).filter(entry=>entry.node);
   if(!entries.length)continue;
   const boxes=entries.map(({node,x})=>{const lines=academicLines(academicLabel(node),13);return {node,x,width:264,height:42+lines.length*21,lines};}),rowHeight=Math.max(...boxes.map(box=>box.height));
   for(const box of boxes)nodes.set(box.node.id,{x:box.x,y:cursor+rowHeight/2,width:box.width,height:box.height,lines:box.lines});
   cursor+=rowHeight+26;
  }
  cursor-=26;cursor+=14;
  nodes.set('academic-stage-'+stage.id,{x:650,y:(top+cursor)/2,width:1300,height:cursor-top});
 }
 let leftLane=0,rightLane=0;
 model.edges.forEach((edge,index)=>{
  const id='E'+String(index+1).padStart(3,'0'),a=nodes.get(edge.source),b=nodes.get(edge.target),sourceTop=a.y-a.height/2,sourceBottom=a.y+a.height/2,targetTop=b.y-b.height/2;
  let points;
  if(edge.source===edge.target){const right=a.x+a.width/2;points=[{x:right,y:a.y+8},{x:right+22,y:a.y+8},{x:right+22,y:sourceTop-14},{x:a.x+a.width/4,y:sourceTop-14},{x:a.x+a.width/4,y:sourceTop}];}
  else if(Math.abs(a.y-b.y)<1){
   const opposite=model.edges.some(other=>other.source===edge.target&&other.target===edge.source);
   if(opposite){const sign=a.x<b.x?1:-1,y=a.y+sign*(a.height/2+13);points=[{x:a.x,y:a.y+sign*a.height/2},{x:a.x,y},{x:b.x,y},{x:b.x,y:b.y+sign*b.height/2}];}
   else{const sign=a.x<b.x?1:-1;points=[{x:a.x+sign*a.width/2,y:a.y},{x:b.x-sign*b.width/2,y:b.y}];}
  }else if(b.y<a.y||model.stages.findIndex(stage=>stage.id===model.byId.get(edge.target).stage)-model.stages.findIndex(stage=>stage.id===model.byId.get(edge.source).stage)>1){
   const right=b.y<a.y&&(a.x>=C||model.byId.get(edge.source).stage==='finish'),rail=right?1192+rightLane++*12:224-leftLane++*10,sign=right?1:-1;
   points=[{x:a.x+sign*a.width/2,y:a.y},{x:rail,y:a.y},{x:rail,y:b.y},{x:b.x+sign*b.width/2,y:b.y}];
  }else{const middle=(sourceBottom+targetTop)/2;points=[{x:a.x,y:sourceBottom},{x:a.x,y:middle},{x:b.x,y:middle},{x:b.x,y:targetTop}];}
  points=avoidAcademicNodes(points,edge.source,edge.target,nodes);
  edges.set(id,{points});
 });
 return {graph:()=>({width:Math.max(1360,1216+rightLane*12),height:cursor,compact:true}),node:id=>nodes.get(id),edge:edge=>edges.get(edge.name)};
}
export async function workflowAcademicSvg(graph,{layoutEngine}={}){
 const model=workflowViewModel(graph);if(!model.nodes.length)throw new Error('当前没有可导出的工作流节点。');
 const engine=layoutEngine||await loadAcademicLayout(),library=engine.default||engine;
 if(!library.graphlib?.Graph||typeof library.layout!=='function')throw new Error('流程图排版组件不可用。');
 const layout=academicLayeredLayout(model,library);
 const padding=36,header=112,footer=146,contentWidth=layout.graph().width,contentHeight=layout.graph().height,width=Math.max(1120,contentWidth+padding*2),height=contentHeight+header+footer;
 const shiftX=(width-contentWidth)/2,shiftY=header;
 const nodeMap=model.nodes.map(node=>{const placed=layout.node(node.id);return {id:node.id,code:node.code,label:node.label,stage:node.stage,x:placed.x+shiftX,y:placed.y+shiftY,width:placed.width,height:placed.height};});
 const edgeMap=model.edges.map((edge,index)=>({id:'E'+String(index+1).padStart(3,'0'),source:edge.source,target:edge.target,conditional:edge.conditional,label:edge.label}));
 let content=`<metadata id="workflow-academic-map">${esc(JSON.stringify({workflow_version:graph?.workflow_version||'',kind:'compiled_structure',presentation:layout.graph().compact?'compact-research-v2':'automatic-extended',nodes:nodeMap,edges:edgeMap,width,height}))}</metadata>`;
 content+=svgLines(['快速审查工作流'],padding,38,{size:28,weight:650})+svgLines(['LangChain × LangGraph · Quick-review workflow'],padding,64,{size:15,color:'#62748a'});
 content+=svgLines([`${model.nodes.length} 个节点 / ${model.edges.length} 条连接 · N 编号与“导出完整结构”一致 · ${graph?.workflow_version||'当前编译版本'}`],padding,90,{size:15,color:'#475569'});
 content+='<defs><marker id="academic-arrow" markerWidth="11" markerHeight="9" refX="10" refY="4.5" orient="auto" markerUnits="userSpaceOnUse"><path d="M0,0 L10,4.5 L0,9 Z" fill="#3e5a76"/></marker></defs>';
 content+=`<g transform="translate(${shiftX},${shiftY})">`;
 model.stages.forEach((stage,index)=>{const box=layout.node('academic-stage-'+stage.id),x=box.x-box.width/2,y=box.y-box.height/2,accent=stage.id==='check'?'#4f8587':stage.id==='report'?'#7a789d':'#5b7fa4',caption=({check:['按类型','逐项核查'],report:['整理并','保存报告'],finish:['结束或','单独重试']})[stage.id]||academicLines(stage.title,6);content+=`<g data-stage="${stage.id}"><rect x="${x}" y="${y}" width="${box.width}" height="${box.height}" fill="${index%2?'#f7f9fc':'#ffffff'}"/><rect x="${x}" y="${y+8}" width="4" height="${Math.max(1,box.height-16)}" rx="2" fill="${accent}"/>${svgLines([String.fromCharCode(65+index)],x+18,y+28,{size:18,weight:650,color:accent})}${svgLines(caption,x+18,y+52,{size:15,weight:600,color:'#3b526c',lineHeight:20})}</g>`;});
 model.edges.forEach((edge,index)=>{
  const id=edgeMap[index].id,placed=layout.edge({v:edge.source,w:edge.target,name:id}),points=placed.points.map(point=>({...point}));
  const source=layout.node(edge.source),target=layout.node(edge.target);
  for(const [point,box] of [[points[0],source],[points.at(-1),target]])if(point&&['__start__','__end__'].includes(point===points[0]?edge.source:edge.target)){
   const dx=point.x-box.x,dy=point.y-box.y,scale=1/Math.sqrt((dx/(box.width/2))**2+(dy/(box.height/2))**2);point.x=box.x+dx*scale;point.y=box.y+dy*scale;
  }
  const path=roundedAcademicPath(points),description=`${id} ${model.byId.get(edge.source).code} → ${model.byId.get(edge.target).code} · ${edge.conditional?'编译声明的条件路径':'顺序连接'}${edge.label?' · '+edge.label:''}`;
  content+=`<g data-source="${esc(edge.source)}" data-target="${esc(edge.target)}" data-edge-id="${id}" data-conditional="${edge.conditional}"><title>${esc(description)}</title><path d="${path}" fill="none" stroke="#ffffff" stroke-width="7" stroke-linejoin="round"/><path d="${path}" fill="none" stroke="${edge.conditional?'#60758b':'#304f70'}" stroke-width="${edge.conditional?2.2:2.8}" ${edge.conditional?'stroke-dasharray="7 5" ':''}stroke-linejoin="round" stroke-linecap="round" marker-end="url(#academic-arrow)"/></g>`;
 });
 model.nodes.forEach(node=>{
  const box=layout.node(node.id),x=box.x-box.width/2,y=box.y-box.height/2,key=leaf(node.id),pause=['await_confirmation','await_retry'].includes(key),terminal=['__start__','__end__'].includes(node.id),retrieval=['numeric_retrieve','functional_retrieve','supplement_retrieval'].includes(key),modelCall=['extract_batch','judge_numeric','judge_functional','polish_one'].includes(key),branch=['select_batch','select_requirement','assess_evidence'].includes(key),fill=pause?'#fff5e7':retrieval?'#edf6f5':modelCall?'#edf3fb':'#ffffff',stroke=pause?'#a88142':retrieval?'#528285':modelCall?'#5b7fa4':'#62768e';
  content+=`<g data-node-id="${esc(node.id)}" data-node-code="${node.code}" data-original-label="${esc(node.label)}"><title>${esc(node.code+' · '+node.label+'\n'+node.id)}</title>`;
  content+=terminal?`<ellipse cx="${box.x}" cy="${box.y}" rx="${box.width/2}" ry="${box.height/2}" fill="#f0f4fa" stroke="${stroke}" stroke-width="1.8"/>`:branch?`<path d="M${x+16},${y}H${x+box.width-16}L${x+box.width},${box.y}L${x+box.width-16},${y+box.height}H${x+16}L${x},${box.y}Z" fill="#f1f4fa" stroke="${stroke}" stroke-width="1.8"/>`:`<rect x="${x}" y="${y}" width="${box.width}" height="${box.height}" rx="9" fill="${fill}" stroke="${stroke}" stroke-width="1.8"/>`;
  if(pause)content+=`<rect x="${x+5}" y="${y+5}" width="${box.width-10}" height="${box.height-10}" rx="4" fill="none" stroke="${stroke}" stroke-width=".8"/>`;
  content+=`<text x="${box.x}" y="${y+19}" text-anchor="middle" fill="#5b7088" font-size="12" font-weight="600">${node.code}${pause?' · 人工等待':branch?' · 条件分支':''}</text><text x="${box.x}" y="${y+43}" text-anchor="middle" fill="#18344f" font-size="18" font-weight="550">${box.lines.map((line,index)=>`<tspan x="${box.x}" dy="${index?21:0}">${esc(line)}</tspan>`).join('')}</text></g>`;
 });
 content+='</g>';
 const legendY=height-footer+36;
 content+=`<path d="M${padding},${legendY-5}h38" stroke="#304f70" stroke-width="2.8" marker-end="url(#academic-arrow)"/>${svgLines(['顺序连接'],padding+52,legendY,{size:15,color:'#475569'})}<path d="M${padding+200},${legendY-5}h38" stroke="#60758b" stroke-width="2.2" stroke-dasharray="7 5" marker-end="url(#academic-arrow)"/>${svgLines(['条件路径（由运行状态选路）'],padding+252,legendY,{size:15,color:'#475569'})}`;
 content+=svgLines(['双边框：人工等待点。返回连线与自循环：重试、下一批次或下一条要求。'],padding,legendY+30,{size:14,color:'#64748b'});
 content+=svgLines(['图示为完整编译结构，保留全部声明连接；不表示所有路径每次都会经过，也不表示已执行。'],padding,legendY+57,{size:14,color:'#64748b'});
 if(graph?.workflow_version==='quick-review-v1')content+=svgLines(['本版本：输出检查失败最多尝试两次；数值与功能核查最多两轮检索；三类要求按类型分支。'],padding,legendY+84,{size:14,color:'#64748b'});
 return svgShell(width,height,'快速审查流程图：与完整结构逐节点对应',content);
}
