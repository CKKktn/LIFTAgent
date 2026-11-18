import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';

// Import the browser implementation without opening daily data or calling a model.
const modalNodes=new Map();
globalThis.document={getElementById:id=>{
  if(!modalNodes.has(id))modalNodes.set(id,{});
  return modalNodes.get(id);
}};
globalThis.fetch=()=>{throw Error('An HTML report export must never make a network call');};
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
const exportUrl=await moduleUrl('report-export.js',{'ui.js':uiUrl,'review.js':reviewUrl,'report-workspace.js':workspaceUrl});
const {buildReportHtml,reportHtmlFilename}=await import(exportUrl);

const verdicts=['已满足','部分满足','不满足','证据不足','待人工复核','证据冲突'];
const statuses=[...verdicts,'未核查'];
const clone=value=>structuredClone(value);
function requirement(index,overrides={}){
  return {id:'R'+String(index).padStart(3,'0'),type:'numeric',category:'数值指标',title:'合成指标'+index,
    criterion:'在0.5 MPa下续航不得低于8.5小时。',quote:'合同原文保留8.5小时。。',
    source:{document_id:'contract',document_name:'技术协议.txt',ordinal:index,page_num_int:[2]},...overrides};
}
function evidence(document='plan-1',overrides={}){
  return {source_id:'E1',quote:'项目方案原文：续航10.5小时。',analysis:'本段描述项目方案参数。',kind:'支持',
    source:{document_id:document,document_name:'项目方案.txt',dataset_id:'kb-fixture',page_num_int:[3],content:'项目方案原文：续航10.5小时。'},...overrides};
}
function result(id,status,overrides={}){
  return {requirementId:id,status,project_plan:'合成项目方案参数为10.5小时。',analysis:'按照原文比对，仍需核实实际实施情况。',
    gaps:'需要人工核实适用条件。',recommendation:'补充相应原始记录。',evidence:[evidence()],...overrides};
}
function report(overrides={}){
  const requirements=Array.from({length:7},(_,i)=>requirement(i+1));
  return {title:'合成研究项目技术符合性审查',mode:'three-stage',scope:'dataset',kb:'kb-fixture',kbName:'合成知识库',
    modelLabel:'合成测试模型',createdAt:'2026-10-26T02:00:00Z',finishedAt:'2026-10-26T02:30:00Z',complete:true,confirmed:true,
    baselineDocs:[{id:'contract',name:'技术协议.txt'}],targetDocs:[],requirements,
    results:verdicts.map((status,i)=>result(requirements[i].id,status)),issues:[],skippedFiles:[],...overrides};
}
const has=(html,expected)=>assert.ok(html.includes(expected),'missing report content: '+expected.slice(0,100));
function section(html,id){
  const start=html.indexOf('<section id="'+id+'"');
  assert.ok(start>=0,'missing section '+id);
  return html.slice(start,html.indexOf('<section id="',start+20)>0?html.indexOf('<section id="',start+20):html.length);
}
function plain(html){return html.replace(/<[^>]*>/g,'');}
function rawData(html){
  const encoded=html.match(/<pre>([\s\S]*?)<\/pre>/)?.[1];
  assert.ok(encoded,'missing machine-readable audit appendix');
  return JSON.parse(encoded.replaceAll('&quot;','"').replaceAll('&#39;',"'").replaceAll('&lt;','<').replaceAll('&gt;','>').replaceAll('&amp;','&'));
}
function freeze(value){
  if(value&&typeof value==='object'){for(const child of Object.values(value))freeze(child);Object.freeze(value);}
  return value;
}

test('export produces a complete fixed-light independent HTML document with no active dependencies',()=>{
  const html=buildReportHtml(report());
  assert.ok(/^<!doctype html>\s*<html lang="zh-CN">/i.test(html));
  for(const token of ['<meta charset="utf-8">','name="color-scheme" content="light"','Content-Security-Policy',"default-src 'none'",'@page{size:A4','@media print'])has(html,token);
  assert.ok(!/<(?:script|iframe|object|embed|link|img|video|audio)\b/i.test(html),'export must have no external or executable elements');
  assert.ok(!/<[a-z][^>]*\s(?:on[a-z]+|src|srcset)\s*=/i.test(html),'export must have no event handlers or active sources');
  assert.ok(!/url\s*\(|@import\b/i.test(html),'report CSS cannot load a remote asset');
  for(const [,href] of html.matchAll(/\shref="([^"]*)"/g))assert.ok(href.startsWith('#'),'all report links are internal anchors');
});
test('four real SVG figures contain numeric labels and accompanying tabular evidence',()=>{
  const html=buildReportHtml(report());
  assert.equal((html.match(/<svg\b/g)||[]).length,4);
  for(const name of ['verdict-distribution','category-verdicts','evidence-coverage','source-frequency'])has(html,'data-report-chart="'+name+'"');
  has(html,'已核查 6 项，未核查 1 项');has(html,'85.7%');has(html,'14.3%');
  has(html,'6 项 · 100%'); // Six saved references among six processed requirements, not among all seven.
  has(html,'保存引用的已核查要求为 6 / 6 项');
  for(const status of statuses)has(html,status);
  has(html,'要求类型与判定矩阵');has(html,'判定构成，不代表项目验收通过率');
  assert.ok(!html.includes('NaN')&&!html.includes('Infinity'));
});
test('empty reports and incomplete records never imply completion or a false 100 percent',()=>{
  for(const value of [{},null,report({requirements:[],results:[],complete:true}),report({complete:false,results:[]})]){
    const html=buildReportHtml(value),summary=section(html,'summary');
    assert.ok(!/100\s*%/.test(plain(summary)),'empty or zero-processed overview cannot be 100 percent');
    assert.ok(!html.includes('NaN')&&!html.includes('Infinity'));
    has(html,'未核查');has(html,'未记录');
  }
  has(buildReportHtml({}),'尚未形成可核查的要求清单');
});
test('partial, awaiting-confirmation, interrupted, and limited reports retain their boundaries',()=>{
  has(buildReportHtml(report()),'仍有 1 项要求未形成有效判定');
  has(buildReportHtml(report({confirmed:false,awaitingConfirmation:true,results:[]})),'指标已提取，尚待人工确认');
  has(buildReportHtml(report({complete:false,interruption:'合成中断原因'})),'审查已中断 · 阶段结果');
  const html=buildReportHtml(report({requirements:[requirement(1)],results:[result('R001','已满足')],limited:true}));
  has(html,'本轮存在材料或处理覆盖缺口');
  assert.ok(!section(html,'summary').includes('已核查要求均记录为已满足'));
});
test('six verdicts, pending items, duplicates and orphan results use the same conservative metrics',()=>{
  const value=report();
  value.requirements.push(clone(value.requirements[0]));
  value.results.push(result('R001','不满足'),result('removed-requirement','已满足',{evidence:[evidence('orphan-file')]}));
  const html=buildReportHtml(value),summary=plain(section(html,'summary'));
  assert.ok(summary.includes('纳入 7 项要求，已核查 6 项，未核查 1 项'));
  assert.ok(summary.includes('已满足 0 项，部分满足 1 项，不满足 2 项'));
  assert.equal((html.match(/<article class="requirement"/g)||[]).length,7);
  assert.ok(!section(html,'statistics').includes('orphan-file'),'orphan evidence cannot inflate graphs');
  assert.equal(rawData(html).results.length,8,'audit appendix preserves raw duplicate and orphan records');
});
test('unknown raw states are pending, remain traceable and cannot become a satisfied verdict',()=>{
  const html=buildReportHtml(report({requirements:[requirement(1)],results:[result('R001','未来自定义状态')]}));
  has(html,'已核查 0 项，未核查 1 项');has(html,'原记录状态为“未来自定义状态”');
  assert.ok(!section(html,'summary').includes('已满足 1 项'));
  assert.equal(rawData(html).results[0].status,'未来自定义状态');
});
test('full requirement text, decimal values and all analysis fields are preserved without truncation',()=>{
  const long='完整内容与数值8.5小时、0.05 MPa。'.repeat(70)+'全文结尾必须可核对';
  const value=report({requirements:[requirement(1,{criterion:'指标条件'+long,quote:'合同引文'+long})],results:[
    result('R001','不满足',{project_plan:'方案'+long,analysis:'分析'+long,gaps:'缺口'+long,recommendation:'建议'+long,
      evidence:[evidence('full-source',{quote:'证据原文'+long,analysis:'证明作用'+long,source:{document_id:'full-source',document_name:'非常长的完整来源文件名称不可被表格或附录截断的技术方案与实测记录.txt',page_num_int:[4,5]}})]})]});
  const html=buildReportHtml(value);
  for(const prefix of ['指标条件','合同引文','方案','分析','缺口','建议','证据原文','证明作用'])has(html,prefix+long);
  has(html,'非常长的完整来源文件名称不可被表格或附录截断的技术方案与实测记录.txt');
  has(html,'第 4、5 页');has(html,'8.5小时');has(html,'0.05 MPa');
  assert.ok(!/<details[^>]*>[\s\S]*<article class="requirement"/.test(html),'core analysis should be directly readable');
});
test('missing material details do not add filler sections to the report',()=>{
  const value=report({requirements:[requirement(1),requirement(2,{type:'functional',category:'功能条件'}),requirement(3,{type:'document',category:'文档存在'})],
    results:[result('R001','证据不足',{project_plan:''}),result('R002','部分满足',{project_plan:undefined}),result('R003','已满足',{project_plan:'',checkType:'document'})]});
  const html=buildReportHtml(value);
  assert.doesNotMatch(section(html,'details'),/class="analysis-materials"/);
  assert.equal((html.match(/class="analysis-analysis"/g)||[]).length,3);
});
test('explicit numeric and functional requirements cannot inherit an existence-only legacy result',()=>{
  const html=buildReportHtml(report({requirements:[requirement(1),requirement(2,{type:'functional',category:'功能条件'}),requirement(3,{type:'numeric',category:'文档存在'})],
    results:[result('R001','已满足',{checkType:'document',project_plan:''}),result('R002','不满足',{checkType:'document',project_plan:''}),result('R003','证据不足',{checkType:'document',project_plan:''})]}));
  assert.doesNotMatch(section(html,'details'),/class="analysis-materials"/);
  assert.equal((html.match(/核查类型记录存在差异/g)||[]).length,3);
  assert.ok(!section(html,'details').includes('本项仅核对文档名称与存在性，不代表文档内容合格。'));
});
test('source frequency deduplicates excerpts per requirement while preserving every quotation',()=>{
  const html=buildReportHtml(report({requirements:[requirement(1),requirement(2)],results:[
    result('R001','已满足',{evidence:[evidence('plan-1',{quote:'第一引文'}),evidence('plan-1',{quote:'第二引文'}),evidence('plan-2')]}),
    result('R002','证据不足',{evidence:[evidence('plan-1')]})]}));
  has(html,'保存引用的已核查要求为 2 / 2 项');has(html,'保存证据条目 <b>4 条</b>');
  const statistics=section(html,'statistics');
  assert.ok(/plan-1<\/td><td class="num">2<\/td>/.test(statistics));
  assert.ok(/plan-2<\/td><td class="num">1<\/td>/.test(statistics));
  has(html,'第一引文');has(html,'第二引文');
});
test('saved-reference coverage uses processed rows as denominator and displays genuine incomplete percentages',()=>{
  const html=buildReportHtml(report({requirements:[requirement(1),requirement(2),requirement(3)],results:[
    result('R001','已满足'),result('R002','证据不足',{evidence:[]}),result('R003','不满足',{evidence:[]})]}));
  has(html,'保存引用的已核查要求为 1 / 3 项');has(html,'1 项 · 33.3%');has(html,'2 项 · 66.7%');
  assert.ok(!section(html,'summary').includes('已满足占全部要求的 100%'));
});
test('all report navigation and citation links resolve to unique internal anchors',()=>{
  const html=buildReportHtml(report());
  const identifiers=[...html.matchAll(/\sid="([^"]+)"/g)].map(match=>match[1]);
  assert.equal(new Set(identifiers).size,identifiers.length,'anchor IDs must be unique');
  for(const [,href] of html.matchAll(/\shref="#([^"]+)"/g))assert.ok(identifiers.includes(href),'unresolved report anchor '+href);
  assert.ok(!/href="(?:\/proxy|https?:|file:|javascript:|data:)/i.test(html));
});
test('an archived thirteen-requirement report includes every identifier and full evidence source names',()=>{
  const requirements=Array.from({length:13},(_,i)=>requirement(i+1));
  const html=buildReportHtml(report({requirements,results:requirements.map((item,i)=>result(item.id,verdicts[i%6]))}));
  for(const item of requirements)has(html,'<span class="requirement-id">'+item.id+'</span>');
  has(html,'技术协议.txt');has(html,'项目方案.txt');has(html,'纳入 13 项要求，已核查 13 项，未核查 0 项');
});
test('all dynamic title, quote, status, source and analysis text is escaped against HTML injection',()=>{
  const attack='<img src=x onerror="evil()"><script>__HTML_ATTACK__()</script></style><svg onload=evil()>';
  const value=report({title:attack,kbName:attack,modelLabel:attack,interruption:attack,
    requirements:[requirement(1,{title:attack,criterion:attack,quote:attack,source:{document_id:attack,document_name:attack}})],
    results:[result('R001','不满足',{analysis:attack,gaps:attack,recommendation:attack,project_plan:attack,
      evidence:[evidence('test',{quote:attack,analysis:attack,source:{document_id:attack,document_name:attack,ordinal:attack}})]})],
    issues:[{reason:attack,phase:attack,sources:[{file:attack}]}],skippedFiles:[{name:attack,reason:attack}]});
  const html=buildReportHtml(value);
  has(html,'&lt;img src=x onerror=&quot;evil()&quot;&gt;');
  assert.ok(!/<(?:script|img)\b/i.test(html),'user tags must never become markup');
  assert.ok(!/<[a-z][^>]*\son[a-z]+\s*=/i.test(html),'no inline event handler can appear');
  assert.equal((html.match(/<style>/g)||[]).length,1);assert.equal((html.match(/<\/style>/g)||[]).length,1);
});
test('unrelated credentials and application configuration are omitted at every exported object level',()=>{
  const secret='DECOY_CREDENTIAL_VALUE_NEVER_EXPORT';
  const value=report({api_key:secret,cookie:secret,modelConfig:{secret},session:{token:secret}});
  value.baselineDocs[0].apiKey=secret;value.requirements[0].api_key=secret;value.requirements[0].source.secret=secret;
  value.results[0].cookie=secret;value.results[0].evidence[0].apiKey=secret;value.results[0].evidence[0].source.api_key=secret;
  value.rawReport=[{id:'R001',api_key:secret,answer:{...value.results[0],secret}}];
  value.polished=[[{id:'R001',status:'已满足',headline:'辅助摘要原文',api_key:secret}]];
  const html=buildReportHtml(value),raw=rawData(html);
  assert.ok(!html.includes(secret),'credentials must not leak via raw appendices or metadata');
  assert.ok(!('api_key' in raw.report));assert.ok(!('cookie' in raw.results[0]));
  assert.equal(raw.polished[0].headline,'辅助摘要原文');
});
test('raw result verdicts remain authoritative even if earlier polished summaries disagree',()=>{
  const html=buildReportHtml(report({requirements:[requirement(1)],results:[result('R001','不满足')],
    polished:[[{id:'R001',status:'已满足',headline:'历史标题',summary:'历史辅助摘要',action:'历史建议'}]]}));
  has(section(html,'summary'),'发现 1 项不满足要求');has(section(html,'summary'),'已满足 0 项');
  assert.equal(rawData(html).polished[0].summary,'历史辅助摘要');
});
test('scope, extraction, skipped materials and processing errors are kept with explicit missing-field text',()=>{
  const html=buildReportHtml(report({limited:true,textExtraction:[{name:'扫描技术协议.pdf',method:'合成解析记录',characters:1249}],
    skippedFiles:[{role:'证据',name:'不可解析文件.pdf',reason:'合成解析失败'}],
    issues:[{phase:'合成核查阶段',requirementId:'R004',reason:'合成超时',retained:2,sources:[{file:'异常来源.txt',id:'error-doc'}]}]}));
  for(const value of ['扫描技术协议.pdf','合成解析记录','1249','不可解析文件.pdf','合成解析失败','合成核查阶段','合成超时','异常来源.txt','保留有效记录 2 条'])has(html,value);
  has(html,'不包含原始附件');has(html,'文档存在型仅核对指定技术文件是否存在');
});
test('legacy records with missing arrays, fields, evidence bodies and anonymous IDs remain readable',()=>{
  for(const value of [{requirements:[{id:'legacy',title:'历史指标'}],results:[{requirementId:'legacy',status:'证据不足'}]},
    report({requirements:[{title:'未编号的指标'}],results:undefined,baselineDocs:undefined,skippedFiles:undefined,issues:undefined}),
    report({requirements:[requirement(1)],results:[result('R001','已满足',{evidence:[{}]})]})]){
    const html=buildReportHtml(value);has(html,'未记录');
    assert.ok(!html.includes('NaN')&&!html.includes('Infinity'));
  }
});
test('report filenames are HTML, filesystem safe, bounded, and follow the recorded task date',()=>{
  const filename=reportHtmlFilename(report({title:'../CON:<项目>\\file|?*\n\u202E危险',finishedAt:'2026-10-25T17:00:00Z'}));
  assert.ok(filename.endsWith('-20261026.html'),filename);
  assert.ok(!/[<>:"/\\|?*\x00-\x1f\u202a-\u202e]/.test(filename));
  assert.ok(!filename.includes('../'));assert.ok(reportHtmlFilename({}).endsWith('.html'));
  assert.ok(reportHtmlFilename({title:'项目'.repeat(150)}).length<140);
});
test('export does not mutate daily or historical report data, including immutable nested records',()=>{
  const value=report({polished:[[{id:'R001',headline:'历史摘要',summary:'原摘要',action:'原建议'}]]}),before=clone(value);
  freeze(value);buildReportHtml(value);reportHtmlFilename(value);buildReportHtml(value);
  assert.deepEqual(value,before);
});


test('main evidence cards retain the references and original text without repeated explanations',()=>{
  const requirements=[requirement(1),requirement(2),requirement(3,{type:'document',category:'文档存在'})];
  const html=buildReportHtml(report({requirements,results:[result('R001','部分满足',{evidence:[evidence('a',{source_id:'E1',kind:'支持'}),evidence('b',{source_id:'E2',kind:'反证'}),evidence('c',{source_id:'E3',kind:'计划'}),evidence('d',{source_id:'E4',kind:'背景'})]}),
    result('R002','证据不足'),result('R003','已满足')]}));
  const details=section(html,'details');
  for(const value of ['R001 · E1','R001 · E2','R001 · E3','R001 · E4','R002 · E1','R003 · E1','项目方案原文'])has(details,value);
  assert.doesNotMatch(details,/evidence-guide|该段原文的证明作用|class="quote-header"[^]*?<span class="tag">/);
  assert.deepEqual(rawData(html).results[0].evidence.map(item=>item.kind),['支持','反证','计划','背景']);
  has(html,'已满足 1 项，部分满足 1 项，不满足 0 项，证据不足 1 项');
});

test('exact duplicate citations combine for display with aliases while saved-record statistics and raw data stay intact',()=>{
  const first=evidence('same-file',{source_id:'E1',quote:'完整引文不删除。'}),duplicate={...clone(first),source_id:'E8'};
  const html=buildReportHtml(report({requirements:[requirement(1)],results:[result('R001','已满足',{evidence:[first,duplicate]})]}));
  assert.equal((html.match(/class="quote-card"/g)||[]).length,1);
  has(html,'1 组展示 / 2 条保存记录');has(html,'R001 · E1');has(section(html,'details'),'R001 · E1 · E8');
  has(html,'保存证据条目 <b>2 条</b>');has(html,'条数沿用保存记录口径');
  assert.equal(rawData(html).results[0].evidence.length,2);
  const ids=[...html.matchAll(/\sid="([^"]+)"/g)].map(match=>match[1]);
  for(const [,href] of html.matchAll(/\shref="#([^"]+)"/g))assert.ok(ids.includes(href),'duplicate citation produced an unresolved anchor '+href);
});

test('source location, quote whitespace, role, and analytical differences prevent citation merging',()=>{
  const first=evidence('same-file',{source_id:'E1',quote:'同一个完整引用片段。'});
  const entries=[first,{...clone(first),source_id:'E2',source:{...first.source,page_num_int:[99]}},
    {...clone(first),source_id:'E3',kind:'反证'}, {...clone(first),source_id:'E4',analysis:'不同的证明作用。'},
    {...clone(first),source_id:'E5',quote:'同一个完整引用片段。 '}, {...clone(first),source_id:'E6',source:{...first.source,ordinal:99}}];
  const html=buildReportHtml(report({requirements:[requirement(1)],results:[result('R001','证据冲突',{evidence:entries})]}));
  assert.equal((html.match(/class="quote-card"/g)||[]).length,6);
  for(const item of entries)has(html,'R001 · '+item.source_id);
  has(html,'第 99 页');has(html,'不同的证明作用。');
});

test('one source used by different requirements retains a separate reference in each requirement',()=>{
  const same=evidence('one-shared-file',{source_id:'E1',quote:'同一文件可以服务多个指标。'});
  const html=buildReportHtml(report({requirements:[requirement(1),requirement(2)],results:[result('R001','已满足',{evidence:[same]}),result('R002','部分满足',{evidence:[clone(same)]})]}));
  assert.equal((html.match(/class="quote-card"/g)||[]).length,2);
  has(html,'R001 · E1');has(html,'R002 · E1');
});

test('duplicate material is displayed once under judgment and all raw fields remain available',()=>{
  const repeated='相同模型说明：额定参数0.5 MPa，需复核。';
  const html=buildReportHtml(report({requirements:[requirement(1)],results:[result('R001','不满足',{project_plan:repeated,analysis:repeated,gaps:repeated,recommendation:repeated})]}));
  const grid=html.match(/<div class="analysis-grid">([\s\S]*?)<\/div>/)?.[1]||'';
  assert.equal((grid.match(/<p class="prose">/g)||[]).length,1);
  has(grid,'判断依据');
  assert.doesNotMatch(grid,/材料要点|缺口与冲突|后续行动/);
  assert.equal((grid.match(/相同模型说明/g)||[]).length,1);
  const raw=rawData(html).results[0];for(const key of ['project_plan','analysis','gaps','recommendation'])assert.equal(raw[key],repeated);
});

test('different numerical facts stay distinct and empty or whitespace-only duplicates are omitted',()=>{
  const html=buildReportHtml(report({requirements:[requirement(1)],results:[result('R001','部分满足',{project_plan:'参数为0.5 MPa。',analysis:'参数为0.6 MPa。',gaps:'参数为0.5 MPa。 ',recommendation:''})]}));
  const grid=html.match(/<div class="analysis-grid">([\s\S]*?)<\/div>/)?.[1]||'';
  assert.equal((grid.match(/<p class="prose">/g)||[]).length,2);has(grid,'0.5 MPa');has(grid,'0.6 MPa');
  has(grid,'class="analysis-analysis"');has(grid,'class="analysis-materials"');
});

test('appendix copies may collapse on screen while full quotations and print expansion rules remain embedded',()=>{
  const html=buildReportHtml(report());
  has(html,'<details class="source-copy"><summary>展开该来源保存的');
  has(html,'class="source-copy-content"');has(html,'正文已完整呈现引文');
  has(html,'.source-copy::details-content{display:block!important;content-visibility:visible!important}');
  assert.ok(!html.includes('<details class="source-copy" open'));
  const appendix=section(html,'sources');has(appendix,'项目方案原文：续航10.5小时。');has(appendix,'R001 · E1');
});
