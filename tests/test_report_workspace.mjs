import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';

// Load production browser modules without a real page, database, or model call.
const modalNodes=new Map();
globalThis.document={getElementById:id=>{
  if(!modalNodes.has(id))modalNodes.set(id,{});
  return modalNodes.get(id);
}};
globalThis.fetch=()=>{throw Error('Report rendering must not make network calls');};
async function moduleUrl(name,links={}){
  let source=await readFile(new URL('../frontend/'+name,import.meta.url),'utf8');
  for(const [dependency,url] of Object.entries(links)){
    source=source.replaceAll("'./"+dependency+"'",JSON.stringify(url));
    source=source.replaceAll('"./'+dependency+'"',JSON.stringify(url));
  }
  return 'data:text/javascript;base64,'+Buffer.from(source).toString('base64');
}
const uiUrl=await moduleUrl('ui.js'),apiUrl=await moduleUrl('api.js');
const reviewUrl=await moduleUrl('review.js',{'ui.js':uiUrl,'api.js':apiUrl});
const workspaceUrl=await moduleUrl('report-workspace.js',{'ui.js':uiUrl,'review.js':reviewUrl});
const {renderReportWorkspace,bindReportWorkspace,reportWorkspaceMetrics}=await import(workspaceUrl);

const verdicts=['已满足','部分满足','不满足','证据不足','待人工复核','证据冲突'];
const statuses=[...verdicts,'未核查'];
const clone=value=>structuredClone(value);
function requirement(index,overrides={}){
  return {id:'R'+String(index).padStart(3,'0'),type:'numeric',category:'数值指标',title:'合成指标'+index,
    criterion:'在0.5 MPa下续航不得低于8.5小时。',quote:'合同原文保留8.5小时。。',
    source:{document_id:'contract',document_name:'技术协议.txt',ordinal:index},...overrides};
}
function evidence(document='plan-1',overrides={}){
  return {source_id:'E1',quote:'项目方案原文：续航10.5小时。',analysis:'本段描述项目方案参数。',kind:'支持',
    source:{document_id:document,document_name:'项目方案.txt',dataset_id:'kb-fixture',content:'项目方案原文：续航10.5小时。'},...overrides};
}
function result(id,status,overrides={}){
  return {requirementId:id,status,project_plan:'合成项目方案参数为10.5小时。',analysis:'按照原文比对，仍需核实实际实施情况。',
    gaps:'需要人工核实适用条件。',recommendation:'补充相应原始记录。',evidence:[evidence()],...overrides};
}
function report(overrides={}){
  const requirements=Array.from({length:7},(_,i)=>requirement(i+1));
  return {title:'合成研究项目技术符合性审查',mode:'three-stage',scope:'dataset',kb:'kb-fixture',kbName:'合成知识库',
    modelLabel:'合成测试模型',createdAt:'2026-10-26T02:00:00Z',complete:false,confirmed:true,
    baselineDocs:[{id:'contract',name:'技术协议.txt'}],targetDocs:[],requirements,
    results:verdicts.map((status,i)=>result(requirements[i].id,status)),issues:[],skippedFiles:[],...overrides};
}
function assertMetricBounds(metrics){
  for(const name of ['total','done','pending','attention','sourceFiles']){
    assert.ok(Number.isFinite(metrics[name]),name+' must be finite');
    assert.ok(metrics[name]>=0,name+' must not be negative');
  }
  assert.ok(metrics.done<=metrics.total,'completed count must not exceed included requirements');
  assert.equal(metrics.done+metrics.pending,metrics.total);
  assert.ok(metrics.attention<=metrics.total);
  assert.equal(statuses.reduce((sum,status)=>sum+(metrics.counts[status]||0),0),metrics.total);
}
function renderedItem(html,id){
  const start=html.indexOf('<details class="rw-item" data-rw-item="'+id+'"');
  assert.ok(start>=0,'included requirement must render: '+id);
  const next=html.indexOf('<details class="rw-item" data-rw-item="',start+1);
  const end=next<0?html.indexOf('<div class="rw-filter-empty"',start):next;
  return html.slice(start,end);
}
const countReferences=html=>(html.match(/data-rw-evidence-key=/g)||[]).length;

test('workspace exports a renderer, interaction binder, and pure metrics function',()=>{
  assert.equal(typeof renderReportWorkspace,'function');
  assert.equal(typeof bindReportWorkspace,'function');
  assert.equal(typeof reportWorkspaceMetrics,'function');
});
test('all six verdicts and a pending requirement remain distinct in overview counts',()=>{
  const metrics=reportWorkspaceMetrics(report());
  assert.equal(metrics.total,7);assert.equal(metrics.done,6);assert.equal(metrics.pending,1);
  assert.equal(metrics.attention,5);
  for(const status of statuses)assert.equal(metrics.counts[status],1,status);
  assertMetricBounds(metrics);
});
test('orphan results cannot inflate counts, attention, or evidence file totals',()=>{
  const value=report();value.results.push(result('removed-requirement','不满足',{evidence:[evidence('orphan-only-file')]}));
  const metrics=reportWorkspaceMetrics(value);
  assert.equal(metrics.total,7);assert.equal(metrics.done,6);assert.equal(metrics.attention,5);
  assert.equal(metrics.counts['不满足'],1);assert.equal(metrics.sourceFiles,1);
  assertMetricBounds(metrics);
});
test('duplicate requirement and result IDs do not double count the same included item',()=>{
  const original=requirement(1);
  const value=report({requirements:[original,clone(original)],results:[result(original.id,'已满足'),result(original.id,'不满足')]});
  const metrics=reportWorkspaceMetrics(value);
  assert.equal(metrics.total,1);assert.equal(metrics.done,1);assert.equal(metrics.pending,0);
  assert.equal(verdicts.reduce((sum,status)=>sum+(metrics.counts[status]||0),0),1);
  assertMetricBounds(metrics);
});
test('source file count uses unique document IDs rather than number of excerpts',()=>{
  const value=report({requirements:[requirement(1),requirement(2)],results:[
    result('R001','已满足',{evidence:[evidence('plan-1'),evidence('plan-1'),evidence('plan-2')]}),
    result('R002','证据不足',{evidence:[evidence('plan-2'),evidence('plan-3')]}),
  ]});
  const metrics=reportWorkspaceMetrics(value);
  assert.equal(metrics.sourceFiles,3);assert.equal(metrics.attention,1);
});
test('an unrecognized verdict is treated conservatively rather than as satisfied',()=>{
  const metrics=reportWorkspaceMetrics(report({requirements:[requirement(1)],results:[result('R001','未来自定义状态')]}));
  assert.equal(metrics.total,1);assert.equal(metrics.counts['已满足'],0);
  assert.equal((metrics.counts['待人工复核']||0)+(metrics.counts['未核查']||0),1);
  assertMetricBounds(metrics);
});
test('polished summaries cannot replace raw verdicts or manufacture satisfied totals',()=>{
  const value=report({requirements:[requirement(1)],results:[result('R001','不满足')],
    polished:[[{id:'R001',status:'已满足',headline:'旧摘要标题',summary:'旧摘要内容',action:'旧建议'}]]});
  const metrics=reportWorkspaceMetrics(value);
  assert.equal(metrics.counts['不满足'],1);assert.equal(metrics.counts['已满足'],0);
});
test('empty or awaiting-confirmation reports do not produce a false 100 percent result',()=>{
  for(const value of [report({requirements:[],results:[],complete:true}),report({confirmed:false,awaitingConfirmation:true,results:[]})]){
    const metrics=reportWorkspaceMetrics(value),html=renderReportWorkspace(value);
    assert.equal(metrics.done,0);assert.equal(metrics.counts['已满足'],0);
    assertMetricBounds(metrics);
    assert.ok(!html.includes('NaN')&&!html.includes('Infinity'));
    assert.match(html,/role="progressbar"[^>]*aria-valuenow="0"/);
    assert.doesNotMatch(html.replace(/<[^>]*>/g,''),/100\s*%/);
  }
});
test('a partially processed report preserves its pending item and all known verdict labels',()=>{
  const html=renderReportWorkspace(report());
  for(const status of verdicts)assert.ok(html.includes(status),status);
  assert.ok(html.includes('R007'),'pending requirement must remain accessible');
  assert.ok(html.includes('未核查')||html.includes('待核查'));
});
test('legacy reports with absent arrays or optional result fields can still render safely',()=>{
  for(const value of [{},report({requirements:[],results:[],baselineDocs:undefined,issues:undefined,skippedFiles:undefined}),
    report({requirements:[{id:'old-1',title:'旧指标'}],results:[{requirementId:'old-1',status:'证据不足'}]})]){
    const html=renderReportWorkspace(value),metrics=reportWorkspaceMetrics(value);
    assert.equal(typeof html,'string');assert.ok(html.length>0);
    assert.ok(!html.includes('NaN')&&!html.includes('Infinity'));
    assertMetricBounds(metrics);
  }
});
test('report titles, original quotations, and analysis are escaped rather than executed',()=>{
  const value=report({title:'<script>window.__reportProbe=1</script>',requirements:[requirement(1,{title:'<img src=x onerror=alert(1)>',
    quote:'原文：8.5小时。。 <script>quoted()</script>'})],results:[result('R001','不满足',{analysis:'<img src=x onerror=bad()> 分析内容'})]});
  const html=renderReportWorkspace(value);
  assert.ok(html.includes('&lt;script&gt;'));
  assert.ok(html.includes('8.5小时。。'),'verbatim source punctuation must not be globally normalized');
  assert.doesNotMatch(html,/<script>window\.__reportProbe/);
  assert.doesNotMatch(html,/<img\b[^>]*onerror=/i);
});
test('detailed evidence keeps source-file links for the existing file-location handler',()=>{
  const html=renderReportWorkspace(report({requirements:[requirement(1)],results:[result('R001','已满足')]}));
  assert.ok(html.includes('source-file-link'));
  assert.ok(html.includes('data-file-source='));
  assert.ok(html.includes('项目方案.txt'));
  assert.ok(html.includes('技术协议.txt'));
});

test('each evidence number is scoped to its requirement and the citation shows source text and link',()=>{
  const value=report({requirements:[requirement(1),requirement(2)],results:[
    result('R001','已满足',{evidence:[evidence('shared',{kind:'支持'})]}),
    result('R002','不满足',{evidence:[evidence('shared',{kind:'反证',analysis:'本段参数低于第二条要求。'})]}),
  ]});
  const html=renderReportWorkspace(value),first=renderedItem(html,'R001'),second=renderedItem(html,'R002');
  assert.ok(first.includes('引用 R001 · E1'));assert.ok(second.includes('引用 R002 · E1'));
  for(const item of [first,second]){
    assert.match(item,/引用原文/);
    assert.match(item,/source-file-link/);
    assert.match(item,/项目方案原文/);
    assert.doesNotMatch(item,/本条引用说明|本段参数低于第二条要求。|rw-evidence-kind/);
  }
  assert.equal(reportWorkspaceMetrics(value).counts['已满足'],1);
  assert.equal(reportWorkspaceMetrics(value).counts['不满足'],1);
});

test('report references show the file link and a complete quotation without an excerpt',()=>{
  const quote='无关的背景句。\n额定压力不得低于0.5 MPa。。\n结尾原文保留。';
  const value=report({requirements:[requirement(1,{title:'设备约束',criterion:'额定压力不得低于0.5 MPa。'})],
    results:[result('R001','部分满足',{evidence:[evidence('pressure',{quote,source:{document_id:'pressure',document_name:'压力参数.txt',dataset_id:'fixture'}})]})]});
  const item=renderedItem(renderReportWorkspace(value),'R001');
  const card=item.slice(item.indexOf('<section class="rw-evidence-block"'));
  assert.ok(card.includes('<div class="sources sources-compact">'));
  assert.ok(card.indexOf('source-file-link')<card.indexOf('<details class="source-full"'));
  assert.doesNotMatch(card,/source-excerpt|原文摘要|词语定位/);
  assert.match(card,/<summary>查看完整引用片段<\/summary>/);
  assert.ok(card.includes('<p>'+quote+'</p>'),'full original quotation and line breaks remain unchanged');
  assert.ok(card.includes('data-file-source='));assert.ok(card.includes('data-rw-detail='));
  assert.doesNotMatch(item,/<details class="sources"|查看 1 条来源证据/);
});

test('only exact duplicate evidence is merged for display while all original aliases remain recoverable',()=>{
  const original=evidence('same-file',{source_id:'E1'}),duplicate={...clone(original),source_id:'E2'};
  const value=report({requirements:[requirement(1)],results:[result('R001','部分满足',{evidence:[original,duplicate]})]}),before=clone(value);
  const html=renderReportWorkspace(value),item=renderedItem(html,'R001'),metrics=reportWorkspaceMetrics(value);
  assert.equal(countReferences(item),1);assert.ok(item.includes('1 处引用'));
  assert.ok(item.includes('原编号：R001 · E1、R001 · E2'));
  assert.equal(metrics.rows[0].evidence.length,2,'the saved evidence collection is never deduplicated');
  assert.equal(metrics.total,1);assert.equal(metrics.counts['部分满足'],1);assert.equal(metrics.sourceFiles,1);
  assert.deepEqual(value,before);
});

test('identical quotations with different roles, explanations or locations remain separate',()=>{
  const base=evidence('shared',{source_id:'E1',source:{document_id:'shared',document_name:'同一方案.txt',positions:[[1,0,0,10,10]]}});
  const entries=[base,{...clone(base),source_id:'E2',kind:'反证'},
    {...clone(base),source_id:'E3',analysis:'另一种适用条件下仍有缺口。'},
    {...clone(base),source_id:'E4',source:{...base.source,positions:[[2,0,0,10,10]]}}];
  const value=report({requirements:[requirement(1)],results:[result('R001','证据冲突',{evidence:entries})]});
  const item=renderedItem(renderReportWorkspace(value),'R001');
  assert.equal(countReferences(item),4);assert.ok(item.includes('4 处引用'));
  for(const id of ['E1','E2','E3','E4'])assert.ok(item.includes('引用 R001 · '+id));
  assert.doesNotMatch(item,/另一种适用条件下仍有缺口。|完全重复记录已合并/);
  assert.equal(reportWorkspaceMetrics(value).counts['证据冲突'],1);
});

test('citation presentation omits role explanations without mutating saved evidence',()=>{
  const entries=['背景','计划','支持','历史未知分类'].map((kind,index)=>evidence('same-plan',{source_id:'E'+(index+1),kind,
    source:{document_id:'same-plan',document_name:'项目建设计划.txt'}}));
  const value=report({requirements:[requirement(1)],results:[result('R001','证据不足',{evidence:entries})]});
  const before=clone(value),item=renderedItem(renderReportWorkspace(value),'R001');
  assert.doesNotMatch(item,/背景参考|计划或承诺|符合要求的依据|未分类参考|本条引用说明/);
  assert.deepEqual(value,before);
  assert.equal(countReferences(item),4);
});

test('explicit requirement types take precedence over conflicting legacy result and category hints',()=>{
  const value=report({requirements:[requirement(1,{type:'numeric',category:'文档存在'}),requirement(2,{type:'document',category:'数值指标'})],
    results:[result('R001','已满足',{checkType:'document'}),result('R002','已满足',{checkType:'numeric'})]});
  const html=renderReportWorkspace(value),numeric=renderedItem(html,'R001'),document=renderedItem(html,'R002');
  assert.ok(numeric.includes('<h4>材料要点</h4>'));
  assert.doesNotMatch(document,/<h4>材料要点<\/h4>/);
  assert.deepEqual(reportWorkspaceMetrics(value).rows.map(row=>row.category),['数值指标','文档存在']);
  assert.equal(reportWorkspaceMetrics(value).total,2);
});

test('judgment keeps its own section and identical supplemental material is omitted',()=>{
  const same='同一段完整说明保持原样。',different='需要补充原始测试记录。';
  const item=renderedItem(renderReportWorkspace(report({requirements:[requirement(1)],results:[result('R001','部分满足',
    {project_plan:same,analysis:same,gaps:same,recommendation:different})]})),'R001');
  assert.ok(item.includes('class="rw-detail-box rw-detail-analysis"><h4>判断依据</h4>'));
  assert.doesNotMatch(item,/<h4>材料要点<\/h4>|<h4>缺口与冲突<\/h4>/);
  assert.equal(item.split('<p>'+same+'</p>').length-1,1);
  assert.ok(item.includes('<h4>后续行动</h4><p>'+different+'</p>'));
});

test('four distinct sections are boxed and long historical reasoning remains expandable',()=>{
  const analysis=Array.from({length:20},(_,index)=>'第'+(index+1)+'项同工况实测低于要求。').join('');
  const value=report({requirements:[requirement(1)],results:[result('R001','不满足',{analysis})]});
  const item=renderedItem(renderReportWorkspace(value),'R001');
  for(const key of ['analysis','materials','gaps','action'])assert.ok(item.includes('class="rw-detail-box rw-detail-'+key+'"'));
  assert.equal(item.split(analysis.slice(0,60)).length-1,1,'reasoning is stored once instead of a preview followed by repeated full text');
  assert.match(item,/data-rw-analysis-toggle/);
  assert.ok(item.includes(analysis));
  assert.doesNotMatch(item,/查看完整判断依据/);
});

test('source disclosure identity stays stable when earlier or duplicate references are added',()=>{
  const original=evidence('stable',{source_id:'E1'}),value=report({requirements:[requirement(1)],results:[result('R001','已满足',{evidence:[original]})]});
  const first=renderedItem(renderReportWorkspace(value),'R001');
  const details=html=>[...html.matchAll(/<details class="source-full" data-rw-detail="([^"]+)"/g)].map(match=>match[1]);
  const evidenceKey=details(first).find(key=>key.startsWith('evidence:E1:'));
  assert.ok(evidenceKey);
  value.results[0].evidence.unshift(evidence('earlier',{source_id:'E8'}));
  value.results[0].evidence.push({...clone(original),source_id:'E9'});
  const next=renderedItem(renderReportWorkspace(value),'R001');
  assert.ok(details(next).includes(evidenceKey));
  assert.equal(countReferences(next),2);
});
test('scope, skipped files and processing issues remain available in the rendered report',()=>{
  const html=renderReportWorkspace(report({issues:[{phase:'合成处理阶段',requirementId:'R003',reason:'合成错误需人工复核'}],
    skippedFiles:[{role:'依据',name:'合成跳过文件.txt',reason:'合成解析失败'}],limited:true}));
  for(const text of ['合成知识库','技术协议.txt','合成处理阶段','合成错误需人工复核','合成跳过文件.txt','合成解析失败']){
    assert.ok(html.includes(text),text);
  }
});
test('rendering and metrics never mutate the underlying report or historical summaries',()=>{
  const value=report({polished:[[{id:'R001',headline:'摘要标题',summary:'原始摘要',action:'复核建议'}]]}),before=clone(value);
  reportWorkspaceMetrics(value);renderReportWorkspace(value);reportWorkspaceMetrics(value);renderReportWorkspace(value);
  assert.deepEqual(value,before);
});
