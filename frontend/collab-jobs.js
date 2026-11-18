// Durable collaborative-review client. Observing a task never starts or stops it.
const RUNNING=new Set(['pending','running','stopping']);
const UNRESOLVED=new Set(['pending','running','stopping','waiting_confirmation','stopped','failed']);
const TERMINAL=new Set(['completed','cancelled','stale']);
const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const rows=value=>Array.isArray(value)?value:[];
export const isCollabJobRunning=job=>Boolean(job&&RUNNING.has(job.status));
export const isCollabJobUnresolved=job=>Boolean(job&&UNRESOLVED.has(job.status));
export function collabReplyLabel(message){
  const status=message?.workflow?.status||message?.replyState;
  const decision=message?.workflow?.review?.decision;
  if(status==='completed'||status==='complete')return decision==='reject'?'已人工退回':'✓ 回复完成';
  return ({pending:'等待处理',running:'正在处理',stopping:'正在保存停止位置',waiting_confirmation:'待人工确认',stopped:'已暂停，可继续',failed:'处理失败，可检查后继续',error:'回复失败',stale:'资料已变化，请新建协审',cancelled:'本轮已结束'})[status]||(message?.pending?'正在处理':'✓ 回复完成');
}
export class CollabJobError extends Error{
  constructor(message,status=0){super(message);this.name='CollabJobError';this.status=status;}
}
export class CollabJobClient{
  constructor({fetchImpl=globalThis.fetch,EventSourceImpl=globalThis.EventSource,onState=()=>{},onConnection=()=>{},onError=()=>{},setTimer=setTimeout,clearTimer=clearTimeout,pollInterval=1500,idleInterval=6000}={}){
    this.fetch=(...args)=>fetchImpl.call(globalThis,...args);this.EventSource=EventSourceImpl;this.onState=onState;this.onConnection=onConnection;this.onError=onError;
    this.setTimer=(...args)=>setTimer.call(globalThis,...args);this.clearTimer=(...args)=>clearTimer.call(globalThis,...args);this.pollInterval=pollInterval;this.idleInterval=idleInterval;
    this.closed=true;this.epoch=0;this.job=null;this.lastEventId=0;this.pendingRefresh=null;
  }
  async request(method,path='',body){
    const response=await this.fetch('/api/v1/collab-jobs'+path,{method,credentials:'same-origin',headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
    let value;try{value=await response.json();}catch{throw new CollabJobError('协审服务未返回有效数据，请稍后重新连接。',response.status);}
    if(!response.ok)throw new CollabJobError(value.message||value.detail||('协审请求失败（'+response.status+'）'),response.status);
    return value;
  }
  async list(options={}){const query=new URLSearchParams(Object.entries(options).filter(([,value])=>value));const value=await this.request('GET',query.size?'?'+query:'');return Array.isArray(value)?value:rows(value?.jobs);}
  async create(payload){const epoch=this.epoch,job=await this.request('POST','',payload);return epoch===this.epoch?this.attach(job):null;}
  async attach(value){
    this.disconnect();this.closed=false;const epoch=this.epoch;
    try{
      const job=typeof value==='string'?await this.request('GET','/'+encodeURIComponent(value)):value;
      if(epoch!==this.epoch||this.closed)return null;
      if(!job||typeof job.id!=='string'||!Number.isInteger(job.revision))throw new CollabJobError('协审任务数据不完整，请重新打开会话。');
      this.job=null;this.lastEventId=0;this.accept(job);this.connect();this.schedule();return this.job;
    }catch(error){if(epoch===this.epoch)this.disconnect();throw error;}
  }
  accept(job){
    if(!job||typeof job.id!=='string'||!Number.isInteger(job.revision))return false;
    if(this.job&&(job.id!==this.job.id||job.revision<this.job.revision))return false;
    this.job=job;this.lastEventId=Math.max(this.lastEventId,Number(job.last_event_id)||0);this.onState(job);
    if(!isCollabJobRunning(job)){this.source?.close();this.source=null;}
    if(TERMINAL.has(job.status)){this.clearTimer(this.timer);this.timer=null;}
    return true;
  }
  async refresh(){
    if(this.closed||!this.job)return this.job;
    if(this.pendingRefresh)return this.pendingRefresh;
    const id=this.job.id,epoch=this.epoch;
    const task=(async()=>{try{const job=await this.request('GET','/'+encodeURIComponent(id));if(epoch===this.epoch&&!this.closed&&job.id===id){this.accept(job);this.onConnection(true);if(isCollabJobRunning(job)&&!this.source)this.connect();}return job;}finally{if(this.pendingRefresh===task)this.pendingRefresh=null;}})();
    this.pendingRefresh=task;return task;
  }
  connect(){
    if(this.closed||!this.job||!isCollabJobRunning(this.job)||!this.EventSource)return;
    this.source?.close();const epoch=this.epoch,id=this.job.id;
    const source=this.source=new this.EventSource('/api/v1/collab-jobs/'+encodeURIComponent(id)+'/events?after='+this.lastEventId,{withCredentials:true});
    source.onopen=()=>{if(epoch===this.epoch&&!this.closed)this.onConnection(true);};
    source.onerror=()=>{if(epoch===this.epoch&&!this.closed&&this.source===source&&isCollabJobRunning(this.job))this.onConnection(false);};
    const update=event=>{
      if(epoch!==this.epoch||this.closed||this.job?.id!==id)return;
      this.lastEventId=Math.max(this.lastEventId,Number(event.lastEventId)||0);
      try{const value=JSON.parse(event.data),job=value.job||value;this.lastEventId=Math.max(this.lastEventId,Number(value.cursor)||0);if(job.id===id&&Number.isInteger(job.revision))this.accept(job);else this.refresh().catch(error=>this.onConnection(false,error));}
      catch{this.refresh().catch(error=>this.onConnection(false,error));}
    };
    source.onmessage=update;source.addEventListener?.('state',update);
  }
  schedule(){
    this.clearTimer(this.timer);this.timer=null;
    if(this.closed||!this.job||TERMINAL.has(this.job.status))return;
    this.timer=this.setTimer(async()=>{this.timer=null;try{await this.refresh();}catch(error){this.onConnection(false,error);if([401,404].includes(error.status)){this.onError(error);this.disconnect();return;}}finally{this.schedule();}},isCollabJobRunning(this.job)?this.pollInterval:this.idleInterval);
  }
  async command(operation,body={}){
    if(!['stop','resume','review','cancel'].includes(operation)||!this.job)throw new CollabJobError('请先打开需要处理的协审任务。');
    const id=this.job.id,epoch=this.epoch,revision=body.revision??this.job.revision;
    try{
      const next=await this.request('POST','/'+encodeURIComponent(id)+'/'+operation,{...body,revision});
      if(epoch===this.epoch&&!this.closed&&next.id===id){this.accept(next);this.connect();this.schedule();}return next;
    }catch(error){
      if(error.status===409){await this.refresh().catch(()=>{});throw new CollabJobError('协审进度或草稿版本已变化，已同步最新状态。请重新查看内容后再操作；本次人工意见没有自动重交。',409);}
      throw error;
    }
  }
  async stop(){const id=this.job?.id;try{return await this.command('stop');}catch(error){if(error.status===409&&this.job?.id===id&&isCollabJobRunning(this.job))return this.command('stop');throw error;}}
  resume(){return this.command('resume');}
  review(body){return this.command('review',body);}
  cancel(){return this.command('cancel');}
  disconnect(){this.epoch++;this.closed=true;this.source?.close();this.source=null;this.clearTimer(this.timer);this.timer=null;this.pendingRefresh=null;}
}

function issue(value){return typeof value==='string'?value:String(value?.message||value?.description||value?.reason||JSON.stringify(value??''));}
export function collabWorkflowHtml(job,{connected=true}={}){
  if(!job)return '';
  const status=job.status,review=job.review||job.human_review||{},waiting=status==='waiting_confirmation';
  const labels={pending:'等待后台处理',running:'正在协审',stopping:'正在保存进度',waiting_confirmation:'等待人工确认',stopped:'已暂停',failed:'处理异常',stale:'资料或配置已变化',completed:review.decision==='reject'?'草稿已退回':'本轮已完成',cancelled:'本轮已结束'};
  const warnings=[...rows(job.validation?.issues),...rows(job.evidence_assessment?.missing),...rows(job.evidence_assessment?.conflicts)].map(issue).filter(Boolean);
  const uniqueWarnings=[...new Set(warnings)];
  const title=labels[status]||'协审状态已保存';
  const actions=[];
  const action=(id,text,primary=false,disabled=false)=>`<button type="button" class="btn ${primary?'primary':'subtle'} small" data-collab-action="${id}"${disabled?' disabled':''}>${text}</button>`;
  if(waiting){actions.push(action('approve','确认复核意见',true),action('revise','修改后确认'),action('supplement','补充检索',false,Number(job.manual_rounds)>=2),action('reject','退回草稿'));}
  if(['stopped','failed'].includes(status)&&job.can_resume!==false)actions.push(action('resume','继续处理',true));
  if(isCollabJobUnresolved(job)&&!isCollabJobRunning(job))actions.push(action('cancel','结束本轮'));
  if(isCollabJobRunning(job))actions.push(action('stop',status==='stopping'?'正在停止':'停止处理',false,status==='stopping'));
  const trace=rows(job.trace).slice(-12);
  const details=uniqueWarnings.length||trace.length||job.standalone_question;
  return `<div class="collab-flow-header"><div><span class="collab-flow-dot" aria-hidden="true"></span><strong>${escape(title)}</strong><span class="collab-flow-metrics">${Number(job.retrieval_rounds)||0} 轮检索 · ${rows(job.reference?.chunks).length} 条来源</span></div>${details?`<details class="collab-flow-details" data-collab-detail="process"><summary>处理记录${uniqueWarnings.length?` · ${uniqueWarnings.length} 项待核对`:''}</summary><div class="collab-flow-detail-body">${job.standalone_question?`<p><strong>本轮检索问题</strong><br>${escape(job.standalone_question)}</p>`:''}${uniqueWarnings.length?`<div class="collab-flow-warnings"><strong>需要关注</strong><ul>${uniqueWarnings.map(value=>`<li>${escape(value)}</li>`).join('')}</ul></div>`:''}${trace.length?`<ol class="collab-flow-trace">${trace.map(row=>`<li>${escape(row.message||row.node||'步骤已保存')}</li>`).join('')}</ol>`:''}</div></details>`:''}</div><p class="collab-flow-message" role="status">${escape(!connected&&isCollabJobRunning(job)?'进度连接暂时中断，正在重连；后台仍保留任务，请勿重复提交。':job.message||title)}</p>${waiting?'<p class="collab-flow-review-note">当前内容为待确认草稿，确认前不作为已复核结论。</p>':''}${status==='stale'?'<p class="collab-flow-review-note">本轮依据或配置已变化，请保留旧记录并提出新的协审问题。</p>':''}${actions.length?`<div class="collab-flow-actions">${actions.join('')}</div>`:''}`;
}

export function mergeCollabMessages(messages,job){
  const output=rows(messages).slice(),userId=job.user_message_id,assistantId=job.assistant_message_id;
  if(!userId||!assistantId)return output;
  const userIndex=output.findIndex(message=>message.id===userId&&userId),assistantIndex=output.findIndex(message=>message.id===assistantId&&assistantId);
  const user=Object.assign(userIndex>=0?output[userIndex]:{}, {id:userId,role:'user',content:job.question||'',...(job.image?{image:job.image}:{})});
  const workflow={...(assistantIndex>=0?output[assistantIndex]?.workflow:{}),job_id:job.id,status:job.status,phase:job.phase,review:job.review||job.human_review||{},validation:job.validation};
  const assistant=Object.assign(assistantIndex>=0?output[assistantIndex]:{}, {id:assistantId,role:'assistant',content:job.answer||'',reference:job.reference||{chunks:[]},pending:isCollabJobRunning(job),replyState:job.status==='completed'?'complete':job.status==='failed'?'error':job.status,workflow});
  if(userIndex>=0)output[userIndex]=user;else output.push(user);
  if(assistantIndex>=0)output[assistantIndex]=assistant;else output.push(assistant);
  return output;
}
