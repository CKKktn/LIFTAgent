import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';

const source=await readFile(new URL('../frontend/collab-jobs.js',import.meta.url),'utf8');
const {CollabJobClient,isCollabJobRunning,isCollabJobUnresolved,collabReplyLabel,collabWorkflowHtml,mergeCollabMessages}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const job=(revision=1,status='running',id='job-1')=>({id,revision,status,chat_id:'chat-1',session_id:'session-1',question:'设备续航是否符合要求？',answer:'合成草稿 [1]',user_message_id:'user-1',assistant_message_id:'assistant-1',reference:{chunks:[{id:'chunk-1',document_id:'doc-1',content:'设备续航10小时。'}]},last_event_id:revision,retrieval_rounds:1});
function fixture(){
  let current=job(),timerId=0,postConflict=false,stopConflicts=0;
  const calls=[],events=[],timers=new Map(),states=[],connections=[],errors=[];
  class EventSourceMock{
    constructor(url){this.url=url;this.listeners={};events.push(this);}
    close(){this.closed=true;}
    addEventListener(name,fn){this.listeners[name]=fn;}
    emit(value,id=''){this.onmessage?.({data:JSON.stringify(value),lastEventId:id});}
  }
  const client=new CollabJobClient({fetchImpl:async(url,options)=>{
    const body=options.body?JSON.parse(options.body):undefined;calls.push({url,method:options.method,body});
    if(options.method==='POST'&&url.endsWith('/stop')&&stopConflicts-->0)return {ok:false,status:409,json:async()=>({message:'revision mismatch'})};
    if(options.method==='POST'&&postConflict)return {ok:false,status:409,json:async()=>({message:'revision mismatch'})};
    if(options.method==='POST'&&/\/(stop|resume|review|cancel)$/.test(url))current={...current,revision:current.revision+1,status:url.endsWith('/stop')?'stopping':url.endsWith('/cancel')?'cancelled':'running'};
    return {ok:true,status:200,json:async()=>url.includes('?')?[structuredClone(current)]:structuredClone(current)};
  },EventSourceImpl:EventSourceMock,onState:value=>states.push(value),onConnection:value=>connections.push(value),onError:value=>errors.push(value),setTimer:fn=>{const id=++timerId;timers.set(id,fn);return id;},clearTimer:id=>timers.delete(id)});
  return {client,calls,events,timers,states,connections,errors,set current(value){current=value;},set conflict(value){postConflict=value;},set stopConflicts(value){stopConflicts=value;}};
}

test('waiting and stopped workflows remain unresolved without appearing to run or finish',()=>{
  for(const status of ['pending','running','stopping'])assert.equal(isCollabJobRunning(job(1,status)),true);
  for(const status of ['waiting_confirmation','stopped','failed']){assert.equal(isCollabJobRunning(job(1,status)),false);assert.equal(isCollabJobUnresolved(job(1,status)),true);}
  for(const status of ['completed','cancelled','stale'])assert.equal(isCollabJobUnresolved(job(1,status)),false);
  assert.equal(collabReplyLabel({replyState:'waiting_confirmation'}),'待人工确认');
  assert.ok(!collabReplyLabel({workflow:{status:'stopped'}}).includes('完成'));
});

test('formal rejection is displayed as a rejected draft rather than a confirmed conclusion',()=>{
  assert.equal(collabReplyLabel({workflow:{status:'completed',review:{decision:'reject'}}}),'已人工退回');
  assert.equal(collabReplyLabel({workflow:{status:'completed',review:{decision:'approve'}}}),'✓ 回复完成');
});

test('creating an exchange sends only job inputs to the durable backend',async()=>{
  const f=fixture();await f.client.create({chat_id:'chat-1',question:'synthetic',require_confirmation:true,request_id:'stable-request'});
  assert.equal(f.calls[0].url,'/api/v1/collab-jobs');assert.equal(f.calls[0].method,'POST');
  assert.equal(f.calls[0].body.request_id,'stable-request');assert.ok(!Object.hasOwn(f.calls[0].body,'messages'));
  assert.ok(f.calls.every(call=>call.url.startsWith('/api/v1/collab-jobs')));f.client.disconnect();
});

test('switching away while creation is pending does not adopt the old exchange',async()=>{
  let release;const states=[];
  const client=new CollabJobClient({fetchImpl:()=>new Promise(resolve=>{release=()=>resolve({ok:true,status:200,json:async()=>job()});}),EventSourceImpl:null,onState:value=>states.push(value)});
  const task=client.create({question:'synthetic'});client.disconnect();release();assert.equal(await task,null);assert.equal(states.length,0);
});

test('disconnecting closes the observer and timers without cancelling the durable workflow',async()=>{
  const f=fixture();await f.client.attach(job());const count=f.calls.length;f.client.disconnect();
  assert.equal(f.calls.length,count);assert.equal(f.events[0].closed,true);assert.equal(f.timers.size,0);
});

test('loss of progress connection preserves the task and falls back to a read',async()=>{
  const f=fixture();await f.client.attach(job());f.events[0].onerror();assert.equal(f.connections.at(-1),false);
  f.current=job(3,'waiting_confirmation');await [...f.timers.values()][0]();
  assert.equal(f.client.job.status,'waiting_confirmation');assert.equal(f.connections.at(-1),true);assert.ok(!f.calls.some(call=>call.url.endsWith('/stop')));f.client.disconnect();
});

test('SSE state carries the durable cursor and closes normally at human confirmation',async()=>{
  const f=fixture();await f.client.attach(job());const source=f.events[0];source.emit({type:'state',job:job(4,'waiting_confirmation'),cursor:18},'17');
  assert.equal(f.client.lastEventId,18);assert.equal(f.client.job.status,'waiting_confirmation');assert.equal(source.closed,true);
  const before=f.connections.length;source.onerror();assert.equal(f.connections.length,before);f.client.disconnect();
});

test('older events never replace a newer draft or a task belonging to another session',async()=>{
  const f=fixture();await f.client.attach(job(8));assert.equal(f.client.accept({...job(7),answer:'old'}),false);assert.equal(f.client.accept(job(9,'running','other-job')),false);
  assert.equal(f.client.job.revision,8);assert.notEqual(f.client.job.answer,'old');f.client.disconnect();
});

test('a pending read cannot redraw the session after its observer was detached',async()=>{
  let release;const states=[];
  const client=new CollabJobClient({fetchImpl:()=>new Promise(resolve=>{release=()=>resolve({ok:true,status:200,json:async()=>job(3)});}),EventSourceImpl:null,onState:value=>states.push(value),setTimer:()=>1,clearTimer:()=>{}});
  await client.attach(job());const task=client.refresh();client.disconnect();release();await task;assert.equal(states.length,1);
});

test('human approval submits the displayed draft revision and is never silently repeated',async()=>{
  const f=fixture();await f.client.attach(job(4,'waiting_confirmation'));f.conflict=true;f.current=job(9,'waiting_confirmation');
  await assert.rejects(()=>f.client.review({revision:4,decision:'approve',notes:'人工确认'}),error=>error.status===409);
  const commands=f.calls.filter(call=>call.method==='POST');assert.equal(commands.length,1);assert.equal(commands[0].body.revision,4);assert.equal(f.client.job.revision,9);f.client.disconnect();
});

test('only an idempotent stop retries once after a state conflict',async()=>{
  const f=fixture();await f.client.attach(job(2));f.current=job(5);f.stopConflicts=1;await f.client.stop();
  const commands=f.calls.filter(call=>call.url.endsWith('/stop'));assert.equal(commands.length,2);assert.deepEqual(commands.map(call=>call.body.revision),[2,5]);f.client.disconnect();
});

test('resuming a stopped workflow reconnects to later progress without creating a new task',async()=>{
  const f=fixture();await f.client.attach(job(2,'stopped'));assert.equal(f.events.length,0);await f.client.resume();assert.equal(f.events.length,1);
  assert.equal(f.client.job.id,'job-1');assert.ok(f.calls.some(call=>call.url.endsWith('/resume')));assert.ok(!f.calls.some(call=>call.url==='/api/v1/collab-jobs'&&call.method==='POST'));f.client.disconnect();
});

test('cancelling ends the observer while keeping the cancelled answer available',async()=>{
  const f=fixture();await f.client.attach(job(2,'waiting_confirmation'));await f.client.cancel();assert.equal(f.client.job.status,'cancelled');assert.equal(f.client.job.answer,'合成草稿 [1]');assert.equal(f.timers.size,0);f.client.disconnect();
});

test('new messages have stable IDs and repeated progress cannot duplicate a turn',()=>{
  let messages=mergeCollabMessages([],job());const originalUser=messages[0],originalAssistant=messages[1];
  messages=mergeCollabMessages(messages,{...job(2),answer:'合成更新 [1]'});messages=mergeCollabMessages(messages,job(3,'waiting_confirmation'));
  assert.equal(messages.length,2);assert.equal(messages[0],originalUser);assert.equal(messages[1],originalAssistant);assert.equal(messages[1].pending,false);assert.equal(messages[1].replyState,'waiting_confirmation');
});

test('updating an exchange preserves previous messages and their annotation positions',()=>{
  const history=[{id:'old-user',role:'user',content:'历史问题'},{id:'old-answer',role:'assistant',content:'历史回复'}];
  let messages=mergeCollabMessages(history,job());assert.equal(messages.length,4);assert.equal(messages[0],history[0]);assert.equal(messages[1],history[1]);
  messages=mergeCollabMessages(messages,job(3,'completed'));assert.equal(messages[3].replyState,'complete');assert.equal(messages[3].workflow.job_id,'job-1');assert.equal(messages[1].content,'历史回复');
});

test('malformed task snapshots do not append anonymous phantom messages',()=>{
  const history=[{role:'user',content:'保留已有消息'}];assert.deepEqual(mergeCollabMessages(history,{id:'bad'}),history);
});

test('human review controls distinguish confirmation, bounded supplementation and resumption',()=>{
  const waiting=collabWorkflowHtml({...job(2,'waiting_confirmation'),manual_rounds:2});assert.ok(waiting.includes('当前内容为待确认草稿'));assert.ok(waiting.includes('data-collab-action="approve"'));assert.ok(waiting.includes('data-collab-action="supplement" disabled'));
  const stopped=collabWorkflowHtml(job(2,'stopped'));assert.ok(stopped.includes('data-collab-action="resume"'));assert.ok(!stopped.includes('data-collab-action="approve"'));
  const completed=collabWorkflowHtml(job(2,'completed'));assert.ok(!completed.includes('data-collab-action='));
});

test('progress summaries expose actual stage records and escape all model-originated markup',()=>{
  const markup=collabWorkflowHtml({...job(),message:'<img src=x onerror=alert(1)>',standalone_question:'<script>bad()</script>',validation:{passed:false,issues:[{message:'引用[99]缺少来源'}]},evidence_assessment:{missing:['缺少实测记录'],conflicts:['适用阶段不一致']},trace:[{message:'已进行第二轮检索'}]});
  assert.ok(markup.includes('&lt;img'));assert.ok(!markup.includes('<script>'));assert.ok(markup.includes('引用[99]缺少来源'));assert.ok(markup.includes('已进行第二轮检索'));assert.ok(!markup.includes('思考过程'));
});

test('native fetch and timer functions are called with their browser global receiver',async()=>{
  const receivers=[];const client=new CollabJobClient({fetchImpl:async function(){receivers.push(this);return {ok:true,status:200,json:async()=>[]};},EventSourceImpl:null,setTimer:function(){receivers.push(this);return 1;},clearTimer:function(){receivers.push(this);}});
  await client.list({chat_id:'chat-1'});await client.attach(job());client.disconnect();assert.ok(receivers.length>=3);assert.ok(receivers.every(value=>value===globalThis));
});

test('API errors retain authentication status for the UI login redirect',async()=>{
  const client=new CollabJobClient({fetchImpl:async()=>({ok:false,status:401,json:async()=>({message:'登录已过期'})}),EventSourceImpl:null});await assert.rejects(()=>client.list(),error=>error.status===401&&error.message==='登录已过期');
});
