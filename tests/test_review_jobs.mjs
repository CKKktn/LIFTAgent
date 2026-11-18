import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
import {loadReviewGraph} from './helpers/review_graph.mjs';
const source=await readFile(new URL('../frontend/review-jobs.js',import.meta.url),'utf8');
const {ReviewJobClient,isReviewJobBusy,reviewJobAction,workflowGraphHtml,workflowGraphSvg,workflowOverviewSvg,workflowViewModel}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const snapshot=(id='job-1',revision=1,status='running')=>({id,revision,status,report:{requirements:[],results:[],workflowJobId:id},last_event_id:revision});
function fixture(){
  let current=snapshot(),conflict=false;
  const calls=[],timers=new Map(),events=[];let timerId=0;
  class EventSourceMock{
    constructor(url){this.url=url;this.listeners={};this.closed=false;events.push(this);}
    addEventListener(name,fn){this.listeners[name]=fn;}
    close(){this.closed=true;}
    emit(name,value,id=''){this.listeners[name]?.({data:JSON.stringify(value),lastEventId:id});}
  }
  const client=new ReviewJobClient({fetchImpl:async(url,options)=>{
    calls.push({url,method:options.method,body:options.body?JSON.parse(options.body):undefined});
    if(options.method==='POST'&&url.endsWith('/resume')&&conflict)return {ok:false,status:409,json:async()=>({message:'revision mismatch'})};
    if(options.method==='POST'&&/\/(?:resume|stop|retry)$/.test(url))current={...current,revision:current.revision+1,status:url.endsWith('/stop')?'stopping':'running'};
    return {ok:true,status:200,json:async()=>url.includes('?active=true')?[current]:structuredClone(current)};
  },EventSourceImpl:EventSourceMock,onState:state=>states.push(state),onConnection:connected=>connections.push(connected),setTimer:(fn)=>{const id=++timerId;timers.set(id,fn);return id;},clearTimer:id=>timers.delete(id)});
  const states=[],connections=[];
  return {client,calls,events,timers,states,connections,set current(value){current=value;},set conflict(value){conflict=value;}};
}
test('task actions distinguish confirmation, retry and running states',()=>{
  assert.equal(isReviewJobBusy(snapshot()),true);assert.equal(isReviewJobBusy(snapshot('j',2,'waiting_confirmation')),false);
  assert.equal(reviewJobAction(snapshot('j',2,'waiting_confirmation')),'确认指标清单');
  assert.equal(reviewJobAction({...snapshot('j',2,'completed'),can_retry:true}),'重试异常项');
  assert.equal(reviewJobAction({...snapshot('j',2,'completed'),can_retry:false}),'');
  assert.equal(reviewJobAction(snapshot('j',2,'stale')),'');
});
test('creation sends task inputs to the backend and never calls a model endpoint',async()=>{
  const f=fixture();await f.client.create({kb:'kb',baseline_document_ids:['contract'],scope:'dataset',model_id:'configured-model',title:'Synthetic'});
  assert.equal(f.calls[0].url,'/api/v1/review-jobs');assert.equal(f.calls[0].method,'POST');
  assert.deepEqual(f.calls[0].body.baseline_document_ids,['contract']);assert.equal(f.client.job.id,'job-1');
  assert.ok(f.calls.every(call=>call.url.startsWith('/api/v1/review-jobs')));f.client.disconnect();
});
test('native browser fetch receives the global object rather than the client as its receiver',async()=>{
  let receiver;
  const client=new ReviewJobClient({fetchImpl:async function(){receiver=this;return {ok:true,status:200,json:async()=>[]};},EventSourceImpl:null});
  await client.list();assert.equal(receiver,globalThis);client.disconnect();
});
test('native browser timer functions also keep their global receiver',async()=>{
  const receivers=[];
  const client=new ReviewJobClient({EventSourceImpl:null,setTimer:function(){receivers.push(this);return 1;},clearTimer:function(){receivers.push(this);}});
  await client.attach(snapshot());client.disconnect();assert.ok(receivers.length>=2);assert.ok(receivers.every(value=>value===globalThis));
});
test('disconnecting the observer closes events and timers without stopping its task',async()=>{
  const f=fixture();await f.client.attach(snapshot());const count=f.calls.length;f.client.disconnect();
  assert.equal(f.calls.length,count);assert.equal(f.events[0].closed,true);assert.equal(f.timers.size,0);
  assert.ok(!f.calls.some(call=>call.url.endsWith('/stop')));
});
test('an event connection loss preserves the running task and a poll can reconnect progress',async()=>{
  const f=fixture();await f.client.attach(snapshot());f.events[0].onerror();
  assert.equal(f.connections.at(-1),false);assert.equal(f.client.job.status,'running');
  f.current=snapshot('job-1',3,'waiting_confirmation');const callback=[...f.timers.values()][0];await callback();
  assert.equal(f.client.job.status,'waiting_confirmation');assert.equal(f.connections.at(-1),true);
  assert.ok(!f.calls.some(call=>call.url.endsWith('/stop')));f.client.disconnect();
});
test('progress notification refreshes the report and tracks the durable event id',async()=>{
  const f=fixture();await f.client.attach(snapshot());f.current=snapshot('job-1',4,'waiting_confirmation');
  f.events[0].emit('state',{revision:4,status:'waiting_confirmation'},'12');await f.client.pendingRefresh;
  assert.equal(f.client.job.revision,4);assert.equal(f.client.lastEventId,12);f.client.disconnect();
});
test('confirmation submits the edited requirements with the dialog revision',async()=>{
  const f=fixture();await f.client.attach(snapshot('job-1',7,'waiting_confirmation'));
  const rows=[{id:'R001',type:'numeric',title:'合成指标',criterion:'至少8小时'}];
  await f.client.resume(rows,7);const call=f.calls.find(item=>item.url.endsWith('/resume'));
  assert.equal(call.body.revision,7);assert.deepEqual(call.body.requirements,rows);assert.ok(!Object.hasOwn(call.body,'report'));f.client.disconnect();
});
test('a conflicting command refreshes state and does not silently resend human confirmation',async()=>{
  const f=fixture();await f.client.attach(snapshot('job-1',2,'waiting_confirmation'));f.current=snapshot('job-1',5,'waiting_confirmation');f.conflict=true;
  await assert.rejects(()=>f.client.resume([{id:'R001'}],2),error=>error.status===409);
  assert.equal(f.client.job.revision,5);assert.equal(f.calls.filter(call=>call.url.endsWith('/resume')).length,1);f.client.disconnect();
});
test('late stale snapshots cannot overwrite a newer report',async()=>{
  const f=fixture();await f.client.attach(snapshot('job-1',9));f.current=snapshot('job-1',4);await f.client.refresh();
  assert.equal(f.client.job.revision,9);assert.equal(f.client.accept(snapshot('different-job',20)),false);f.client.disconnect();
});
test('an explicit stop uses a backend command and does not abort browser reads as a substitute',async()=>{
  const f=fixture();await f.client.attach(snapshot());await f.client.stop();
  assert.equal(f.calls.at(-1).url,'/api/v1/review-jobs/job-1/stop');assert.equal(f.calls.at(-1).body.revision,1);assert.equal(f.client.job.status,'stopping');f.client.disconnect();
});
test('only an explicit stop may retry once after a moving-node revision conflict',async()=>{
  let stops=0,server=snapshot('job-1',5);const calls=[];
  const client=new ReviewJobClient({fetchImpl:async(url,options)=>{calls.push({url,body:options.body?JSON.parse(options.body):undefined});if(url.endsWith('/stop')){stops++;if(stops===1)return {ok:false,status:409,json:async()=>({message:'node advanced'})};server=snapshot('job-1',6,'stopping');}return {ok:true,status:200,json:async()=>server};},EventSourceImpl:null,setTimer:()=>1,clearTimer:()=>{}});
  await client.attach(snapshot('job-1',1));await client.stop();
  const commands=calls.filter(call=>call.url.endsWith('/stop'));assert.equal(commands.length,2);assert.equal(commands[0].body.revision,1);assert.equal(commands[1].body.revision,5);assert.equal(client.job.status,'stopping');client.disconnect();
});
test('graph display draws actual edges and escapes untrusted node and trace strings',()=>{
  const graph={mermaid:'graph TD; A[<img onerror=bad>] --> B',nodes:[{id:'a',label:'<script>bad</script>'},{id:'b',label:'人工确认'}],edges:[{source:'a',target:'b',label:'<x>'},{source:'a',target:'missing'}],trace:[{node:'a',message:'<img onerror=bad>'}]};
  const html=workflowGraphHtml(graph),svg=workflowGraphSvg(graph);
  assert.ok(html.includes('&lt;script&gt;bad'));assert.ok(!html.includes('<script>'));assert.ok(!html.includes('<img'));
  assert.ok(svg.includes('data-source="a" data-target="b"'));assert.ok(svg.includes('人工确认'));assert.ok(!svg.includes('missing'));assert.ok(html.includes('真实连接'));
});
test('nested graph nodes match executed namespace paths and incomplete traces are identified',()=>{
  const graph={nodes:[{id:'extract_requirements:select_batch',label:'选择批次'},{id:'check_requirements:judge_numeric',label:'数值核查'}],edges:[],trace:[{node:'extract_requirements/select_batch'}],trace_truncated:true,trace_total:700};
  const svg=workflowGraphSvg(graph),html=workflowGraphHtml(graph);
  assert.ok(svg.includes('fill="#e5f5ec"'));assert.ok(svg.includes('fill="#f3f6fa"'));
  assert.ok(html.includes('仅显示前 1 / 700 条执行记录'));assert.ok(html.includes('未显示不代表未执行'));
});
test('the production quick-review entry no longer executes model workflow code in the browser',async()=>{
  const app=await readFile(new URL('../frontend/app.js',import.meta.url),'utf8');
  assert.ok(!/await\s+(?:extractProjectQuestions|runRetrievalReview|polishReport)\s*\(/.test(app));
  assert.ok(app.includes('ReviewJobClient'));assert.ok(app.includes('workflowJobId'));
});

test('stage grouping and complete export retain every actual node and every directed edge',()=>{
  const graph=loadReviewGraph();
  const model=workflowViewModel(graph),svg=workflowGraphSvg(graph);
  assert.equal(model.stages.length,6);
  assert.equal(new Set(model.stages.flatMap(stage=>stage.nodes.map(node=>node.id))).size,graph.nodes.length);
  assert.equal((svg.match(/data-node-id=/g)||[]).length,graph.nodes.length);
  assert.equal((svg.match(/data-source=/g)||[]).length,graph.edges.length);
  for(const edge of graph.edges)assert.ok(svg.includes(`data-source="${edge.source}" data-target="${edge.target}"`));
  assert.ok(!svg.includes('<path d="M'),'the connection index has no crossing wires');
});
test('overview is separate from technical nodes and traces and starts with only the overview visible',()=>{
  const graph=loadReviewGraph();
  const html=workflowGraphHtml(graph),svg=workflowOverviewSvg(graph);
  assert.match(html,/id="wf-pane-overview"[^>]*class="wf-pane wf-overview">/);
  assert.match(html,/id="wf-pane-nodes"[^>]* hidden>/);
  assert.match(html,/id="wf-pane-records"[^>]* hidden>/);
  assert.equal((html.match(/class="wf-stage-card /g)||[]).length,6);
  assert.equal((svg.match(/data-members=/g)||[]).length,6);
  assert.ok(html.includes('每条要求进入相应分支'));
  assert.ok(!html.includes('min-width:620px'));
});
test('truncated traces do not claim that the last loaded event is the current task status',()=>{
  const html=workflowGraphHtml({nodes:[],edges:[],trace:[{node:'created',status:'pending',message:'old-message'}],trace_truncated:true,trace_total:999});
  assert.ok(html.includes('仅显示前 1 / 999 条执行记录'));
  assert.ok(!html.includes('<strong>等待后台执行</strong>'));
  assert.ok(html.includes('已载入部分轨迹'));
});
test('complete structure export preserves long labels, unknown groups and self loops',()=>{
  const label='很长的节点名称'.repeat(10),graph={nodes:[{id:'future_node',label}],edges:[{source:'future_node',target:'future_node',conditional:true}]};
  const model=workflowViewModel(graph),svg=workflowGraphSvg(graph);
  assert.equal(model.stages[0].id,'other');
  assert.ok(svg.includes('data-source="future_node" data-target="future_node"'));
  assert.ok(!svg.includes('…'));
  assert.ok(svg.replace(/<[^>]*>/g,'').includes(label));
});
