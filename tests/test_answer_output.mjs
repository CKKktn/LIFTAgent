import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';

// ui.js also binds the shared modal. Rendering tests need no browser or user data.
const modalNodes=new Map();
globalThis.document={getElementById:id=>{
  if(!modalNodes.has(id))modalNodes.set(id,{});
  return modalNodes.get(id);
}};
async function moduleUrl(name,links={}){
  let source=await readFile(new URL('../frontend/'+name,import.meta.url),'utf8');
  for(const [dependency,url] of Object.entries(links))source=source.replaceAll("'./"+dependency+"'",JSON.stringify(url));
  return 'data:text/javascript;base64,'+Buffer.from(source).toString('base64');
}
const uiUrl=await moduleUrl('ui.js'),apiUrl=await moduleUrl('api.js');
const reviewUrl=await moduleUrl('review.js',{'ui.js':uiUrl,'api.js':apiUrl});
const workflowUrl=await moduleUrl('three-stage-review.js',{'ui.js':uiUrl,'review.js':reviewUrl});
const {markdown,answerHtml,sourcesHtml,evidenceKindInfo,evidenceReference,evidenceDisplayEntries}=await import(uiUrl);
const {readSSE}=await import(apiUrl);
const {questionFor,reviewQuestion,reportMarkdown}=await import(reviewUrl);
const {rawReview,workflowHtml}=await import(workflowUrl);
const requirement={id:'R010',type:'numeric',category:'数值指标',title:'设备续航',criterion:'设备续航不得低于8小时。',quote:'设备续航不得低于8小时。'};
const oldQuestion=r=>`指标要求：${r.criterion}。当前知识库中的项目方案如何规定这一指标？请分“指标要求”“项目方案”“对比分析”回答，核对数值、单位、比较方向及适用条件，判断项目方案是否满足要求；方案符合不等同实测达标。结合所选知识库回答，引用真实来源；未检索到信息不等于未实现。`;
const savedRequirement={...requirement,question:oldQuestion(requirement)};
const report=()=>({mode:'three-stage',title:'合成审查任务',requirements:[structuredClone(savedRequirement)],results:[],baselineDocs:[],targetDocs:[],textExtraction:[]});

test('numeric questions add exactly one sentence boundary with or without a final stop',()=>{
  for(const criterion of ['设备续航不得低于8小时','设备续航不得低于8小时。','设备续航不得低于8小时。。  ']){
    assert.ok(questionFor({...requirement,criterion}).startsWith('指标要求：设备续航不得低于8小时。当前项目'));
  }
});
test('functional questions do not append a stop after an existing question or exclamation mark',()=>{
  for(const criterion of ['支持离线工作。','支持离线工作！','支持离线工作？']){
    const result=questionFor({type:'functional',criterion});
    assert.ok(result.startsWith('功能要求：'+criterion+'知识库是否'));
  }
});
test('decimal values, versions, units and quoted source punctuation remain unchanged',()=>{
  const input={...requirement,criterion:'在0.5 MPa下运行8.5小时，接口版本为1.2。',quote:'合同原文确实写了两个句号。。'};
  const before=structuredClone(input);
  assert.ok(questionFor(input).includes('在0.5 MPa下运行8.5小时，接口版本为1.2。当前项目'));
  assert.deepEqual(input,before);
});
test('existing generated questions render correctly without rewriting saved records',()=>{
  const before=structuredClone(savedRequirement);
  assert.match(savedRequirement.question,/8小时。。当前/);
  assert.match(reviewQuestion(savedRequirement),/8小时。当前/);
  assert.doesNotMatch(reviewQuestion(savedRequirement),/8小时。。当前/);
  assert.deepEqual(savedRequirement,before);
});
test('custom questions and document-name questions are preserved',()=>{
  const custom='请核对原文中的“8小时。。”，不要擅自修订。';
  assert.equal(reviewQuestion({...requirement,question:custom}),custom);
  assert.equal(questionFor({type:'document',title:'测试报告'}),'知识库文件清单中是否存在“测试报告”？只检查文件名，不检查文档内容。');
});
test('workflow and raw question views use the same corrected question and keep R010',()=>{
  const value=report(),before=structuredClone(value),html=workflowHtml(value);
  assert.doesNotMatch(html,/8小时。。当前/);
  assert.match(html,/R010/);
  assert.equal(rawReview(value)[0].question,questionFor(savedRequirement));
  assert.deepEqual(value,before);
});
test('Markdown export corrects generated question text while retaining verbatim quotations',()=>{
  const value=report();value.requirements[0].quote='合同原文保留重复句号。。';
  const output=reportMarkdown(value);
  assert.doesNotMatch(output,/8小时。。当前/);
  assert.ok(output.includes('> 合同原文保留重复句号。。'));
});
test('an old raw-report snapshot gets a corrected question view without changing its answers',()=>{
  const value=report();
  value.rawReport=[{...savedRequirement,answer:{analysis:'原始分析文本。。'}}];
  const before=structuredClone(value),output=workflowHtml(value);
  assert.doesNotMatch(output,/8小时。。当前/);
  assert.match(output,/原始分析文本。。/);
  assert.deepEqual(value,before);
});

function streamResponse(text,byteSize=17){
  const bytes=new TextEncoder().encode(text);
  return new Response(new ReadableStream({start(controller){
    for(let i=0;i<bytes.length;i+=byteSize)controller.enqueue(bytes.slice(i,i+byteSize));
    controller.close();
  }}),{headers:{'Content-Type':'text/event-stream; charset=utf-8'}});
}
test('SSE handles every-byte UTF-8 and CRLF splits without duplicating punctuation or text',async()=>{
  const input=': heartbeat\r\n\r\ndata: {"code":0,"data":{"answer":"续航8小时。"}}\r\n\r\ndata: {"code":0,"data":{"answer":"续航8小时。需要核对。"}}\r\n\r\ndata: {"code":0,"data":true}\r\n\r\n';
  const rows=[];await readSSE(streamResponse(input,1),row=>rows.push(row));
  assert.deepEqual(rows.map(r=>r.answer),['续航8小时。','续航8小时。需要核对。']);
});
test('SSE accepts DONE without a trailing empty line',async()=>{
  const rows=[];await readSSE(streamResponse('data: {"answer":"完整回答"}\n\ndata: [DONE]'),row=>rows.push(row));
  assert.equal(rows[0].answer,'完整回答');
});
test('SSE rejects a partial answer followed by an unexpected connection close',async()=>{
  const rows=[];
  await assert.rejects(readSSE(streamResponse('data: {"code":0,"data":{"answer":"尚未完成。"}}\n\n'),row=>rows.push(row)),/连接提前中断/);
  assert.equal(rows[0].answer,'尚未完成。');
});
test('SSE rejects an empty response instead of marking it complete',async()=>{
  await assert.rejects(readSSE(streamResponse(''),()=>{}),/连接提前中断/);
});
test('SSE preserves a provider error rather than replacing it with the interrupted-stream error',async()=>{
  await assert.rejects(readSSE(streamResponse('data: {"code":1,"message":"合成测试：模型服务超时"}\n\n'),()=>{}),/合成测试：模型服务超时/);
});
test('SSE stops at a completion event and ignores later bytes',async()=>{
  const rows=[];await readSSE(streamResponse('data: {"code":0,"data":true}\n\ndata: invalid\n\n'),row=>rows.push(row));
  assert.deepEqual(rows,[]);
});

test('numbered answers render as an ordered list, including a non-one starting number',()=>{
  assert.equal(markdown('1. 第一条\n2. 第二条'),'<ol><li>第一条</li><li>第二条</li></ol>');
  assert.equal(markdown('3) 第三条\r\n4) 第四条'),'<ol start="3"><li>第三条</li><li>第四条</li></ol>');
});
test('switching between numbered and bullet lists produces separate balanced lists',()=>{
  assert.equal(markdown('1. 有序\n- 无序\n+ 补充\n正文'),'<ol><li>有序</li></ol><ul><li>无序</li><li>补充</li></ul><p>正文</p>');
});
test('inline code keeps literal Markdown and escapes HTML while outer bold still works',()=>{
  assert.equal(markdown('**结果**：`**原样** <tag> 8.5。。`'),'<p><strong>结果</strong>：<code>**原样** &lt;tag&gt; 8.5。。</code></p>');
  assert.equal(markdown('``包含 ` 单反引号``'),'<p><code>包含 ` 单反引号</code></p>');
});
test('fenced code is literal and balanced even while a stream has not closed its fence',()=>{
  assert.equal(markdown('```\n**文字**\n1. 原样\n8.5。。'),'<pre><code>**文字**\n1. 原样\n8.5。。\n</code></pre>');
});
test('answer rendering escapes untrusted markup and keeps verbatim source punctuation',()=>{
  const output=answerHtml('原文：8.5小时。。\n<script>alert(1)</script>');
  assert.ok(output.includes('8.5小时。。'));
  assert.ok(output.includes('&lt;script&gt;'));
  assert.ok(!output.includes('<script>'));
});

test('evidence roles distinguish requirement compliance from background and file presence',()=>{
  assert.equal(evidenceKindInfo('支持').label,'符合要求的依据');
  assert.equal(evidenceKindInfo('反证').label,'不符合要求的依据');
  assert.match(evidenceKindInfo('背景').description,/不能单独证明/);
  assert.match(evidenceKindInfo('计划').description,/不能仅凭承诺/);
  assert.equal(evidenceKindInfo('支持',{document:true}).label,'文件存在记录');
  assert.match(evidenceKindInfo('支持',{document:true}).description,/不代表文件内容/);
  assert.equal(evidenceKindInfo('<script>').label,'未分类参考');
});

test('reference labels are local to an item and do not reuse bare E1 across requirements',()=>{
  assert.equal(evidenceReference('R001',{source_id:'E1'}),'R001 · E1');
  assert.equal(evidenceReference('R002',{source_id:'E1'}),'R002 · E1');
  assert.equal(evidenceReference('R003',{},2),'R003 · 引用 3');
});

test('exact duplicate references collapse in the view and retain aliases without changing saved records',()=>{
  const first={source_id:'E1',kind:'支持',analysis:'方案参数符合。',quote:'续航10.5小时。。',source:{id:'chunk1',document_id:'doc1',dataset_id:'kb1'}};
  const data=[first,{...structuredClone(first),source_id:'E4'},structuredClone(first)];
  const before=structuredClone(data),entries=evidenceDisplayEntries(data);
  assert.equal(entries.length,1);assert.equal(entries[0].index,0);assert.equal(entries[0].duplicateCount,2);
  assert.deepEqual(entries[0].aliases,['E1','E4']);assert.equal(entries[0].item.quote,'续航10.5小时。。');
  assert.deepEqual(data,before);
});

test('different views, source locations, actual numbers and unidentified excerpts are never merged',()=>{
  const first={source_id:'E1',kind:'支持',analysis:'参数符合',quote:'续航10小时',source:{id:'chunk1',document_id:'doc1',page_num_int:[1]}};
  const entries=evidenceDisplayEntries([first,{...first,kind:'反证'},{...first,analysis:'只描述计划'},
    {...first,quote:'续航1小时'},{...first,source:{...first.source,page_num_int:[2]}},
    {...first,source:{...first.source,document_id:'doc2'}}, {source_id:'E8'},{source_id:'E9'}]);
  assert.equal(entries.length,8);
});

test('compact report references remove nested one-source counters and preserve exact complete quotations',()=>{
  const quote='续航10.5小时。\n第二行8.5小时。。\n<script>alert(1)</script>';
  const output=sourcesHtml([{document_id:'plan',document_name:'项目方案.txt',content:quote}],'续航10.5小时',{compact:true});
  assert.ok(output.includes('sources-compact'));assert.ok(output.includes('data-file-source='));
  assert.ok(!output.includes('查看 1 条来源证据'));assert.ok(!output.includes('[1]'));
  assert.ok(output.includes('查看完整引用片段'));assert.ok(!output.includes('完整切片'));
  assert.ok(output.includes('续航10.5小时。\n第二行8.5小时。。\n&lt;script&gt;alert(1)&lt;/script&gt;'));
  assert.ok(!output.includes('<script>'));assert.equal((output.match(/<mark>/g)||[]).length,1);
});

test('a file-name-only reference does not show a fake full document excerpt',()=>{
  const output=sourcesHtml([{document_id:'report',document_name:'测试报告.pdf',content:'测试报告.pdf'}],'测试报告',{compact:true});
  assert.ok(output.includes('本条引用来自知识库文件名清单'));assert.ok(!output.includes('source-full'));
  assert.equal((output.match(/>测试报告.pdf ↗/g)||[]).length,1);
});

test('PDF line wrapping cannot hide the value after a negation in a report excerpt',()=>{
  for(const lineBreak of ['\n','\r\n']){
    const quote='项目背景。\nT01 连续续航时间 在环境温度25±2℃、额定负载条件下，终端方案设计的连续续航时间不'+lineBreak+'得低于 8 小时。\nT02 工作温度应覆盖-10℃至+50℃。';
    const output=sourcesHtml([{document_id:'contract',document_name:'技术协议.pdf',content:quote}],'连续续航时间',{compact:true});
    const highlighted=/<mark><strong>([\s\S]*?)<\/strong><\/mark>/.exec(output)[1];
    assert.ok(highlighted.includes('不'+lineBreak+'得低于 8 小时。'));
    assert.ok(!highlighted.includes('T02'));
    assert.ok(output.includes('<p>'+quote+'</p>'));
  }
});

test('a condition spanning several PDF lines keeps units, endpoints and sentence punctuation',()=>{
  const quote='电源容量不得低\n于 8.5 Ah，测试温度为25±2\n℃；版本1.2的接口时延不\n超过200 ms。\n下一项要求不参与此处摘录。';
  const output=sourcesHtml([{document_id:'contract',document_name:'协议.pdf',content:quote}],'电源容量',{compact:true});
  const highlighted=/<mark><strong>([\s\S]*?)<\/strong><\/mark>/.exec(output)[1];
  assert.ok(highlighted.includes('8.5 Ah'));assert.ok(highlighted.includes('25±2\n℃'));
  assert.ok(highlighted.includes('版本1.2'));assert.ok(highlighted.endsWith('不\n超过200 ms。'));
});

test('a report excerpt without a keyword match ends at a sentence boundary rather than character 220',()=>{
  const first='甲'.repeat(225)+'参数应不低于8.5小时。';
  const output=sourcesHtml([{document_id:'contract',document_name:'协议.pdf',content:first+'\n下一项要求。'}],'无匹配词',{compact:true});
  assert.ok(output.includes('<div class="source-excerpt"><span class="source-excerpt-label">原文摘要 · 词语定位</span>'+first+'</div>'));
});

test('unclosed original fragments stay verbatim and are never completed with invented thresholds',()=>{
  const quote='连续续航时间不';
  const output=sourcesHtml([{document_id:'contract',document_name:'协议.pdf',content:quote}],'连续续航时间',{compact:true});
  assert.ok(output.includes('<mark><strong>'+quote+'</strong></mark>'));
  assert.ok(output.includes('<p>'+quote+'</p>'));assert.ok(!output.includes('8小时'));
});
