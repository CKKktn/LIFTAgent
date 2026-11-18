import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';

// Load the browser module graph without a package-type setting or a browser.
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
const {confirmedRequirements}=await import(workflowUrl);

const numeric=()=>({id:'R012',type:'numeric',category:'数值指标',title:'设备续航时间',subject:'设备续航',criterion:'在0.5 MPa下续航不得低于8.5小时。',quote:'原文保留：在0.5 MPa下续航不得低于8.5小时。。',source:{document_id:'source-1',document_name:'技术协议.txt',content:'连续原文。。'},rationale:'合同明确规定量化性能要求'});
const documentRequirement=()=>({id:'R013',type:'document',category:'文档存在',title:'接口设计说明书',subject:'交付文档',document_name:'接口设计说明书',criterion:'须提交接口设计说明书。',quote:'乙方应提交接口设计说明书。',source:{document_id:'source-1',document_name:'技术协议.txt'}});
const manual=()=>({title:'',subject:'',criterion:'',category:'功能条件',type:'functional',userAdded:true,quote:'人工补充指标，请人工核对合同依据',source:{document_name:'人工补充'}});
function form(rows,overrides={}){
  const data=new FormData();
  for(const [index,row] of rows){
    data.append('include',String(index));
    data.set('category_'+index,row.category);
    data.set('title_'+index,row.title);
    data.set('criterion_'+index,row.criterion);
    data.set('document_'+index,row.document_name||'');
  }
  for(const [key,value] of Object.entries(overrides))data.set(key,value);
  return data;
}

test('confirming preserves existing numeric values, conditions and verbatim evidence',()=>{
  const input=[numeric()],before=structuredClone(input);
  const rows=confirmedRequirements(input,form([[0,input[0]]]));
  assert.equal(rows.length,1);
  assert.equal(rows[0].id,'R001');
  assert.equal(rows[0].type,'numeric');
  assert.equal(rows[0].category,'数值指标');
  assert.equal(rows[0].criterion,before[0].criterion);
  assert.equal(rows[0].quote,before[0].quote);
  assert.deepEqual(rows[0].source,before[0].source);
  assert.equal(rows[0].document_name,'');
  assert.match(rows[0].question,/8\.5小时。当前项目/);
  assert.doesNotMatch(rows[0].question,/8\.5小时。。当前项目/);
  assert.deepEqual(input,before);
});
test('unselected empty manual drafts do not block confirmation or mutate the draft data',()=>{
  const input=[numeric(),manual()],before=structuredClone(input);
  const rows=confirmedRequirements(input,form([[0,input[0]]]));
  assert.equal(rows.length,1);
  assert.deepEqual(input,before);
});
test('deleted draft positions are skipped and included rows receive consecutive report IDs',()=>{
  const added={...manual(),title:'远程故障告警',criterion:'系统应支持远程故障告警。'};
  const input=[numeric(),null,added],before=structuredClone(input);
  const rows=confirmedRequirements(input,form([[0,input[0]],[2,added]]));
  assert.deepEqual(rows.map(row=>row.id),['R001','R002']);
  assert.deepEqual(rows.map(row=>row.title),[input[0].title,added.title]);
  assert.deepEqual(input,before);
});
test('a functional manual card keeps its provenance and uses its entered title as subject',()=>{
  const input=[manual()],before=structuredClone(input);
  const data=form([[0,{...input[0],title:'  远程故障告警  ',criterion:'  应支持故障远程告警。  '}]]) ;
  const row=confirmedRequirements(input,data)[0];
  assert.equal(row.title,'远程故障告警');
  assert.equal(row.subject,'远程故障告警');
  assert.equal(row.criterion,'应支持故障远程告警。');
  assert.equal(row.category,'功能条件');
  assert.equal(row.type,'functional');
  assert.equal(row.document_name,'');
  assert.equal(row.userAdded,true);
  assert.equal(row.quote,before[0].quote);
  assert.deepEqual(row.source,before[0].source);
  assert.match(row.question,/功能要求：应支持故障远程告警。知识库是否/);
  assert.deepEqual(input,before);
});
test('a manual card can become a numeric requirement without a stale document name',()=>{
  const input=[manual()];
  const data=form([[0,{...input[0],category:'数值指标',title:'接口吞吐量',criterion:'吞吐量不低于100 MB/s。',document_name:'不应带入的旧文档名'}]]);
  const row=confirmedRequirements(input,data)[0];
  assert.equal(row.type,'numeric');
  assert.equal(row.category,'数值指标');
  assert.equal(row.subject,'接口吞吐量');
  assert.equal(row.document_name,'');
  assert.equal(row.userAdded,true);
  assert.match(row.question,/指标要求：吞吐量不低于100 MB\/s。当前项目/);
});
test('a manual document requirement stores a trimmed document name and generates the filename question',()=>{
  const input=[manual()];
  const data=form([[0,{...input[0],category:'文档存在',title:'交付测试报告',criterion:'须提供测试报告。',document_name:'  设备测试报告  '}]]);
  const row=confirmedRequirements(input,data)[0];
  assert.equal(row.type,'document');
  assert.equal(row.category,'文档存在');
  assert.equal(row.subject,'交付测试报告');
  assert.equal(row.document_name,'设备测试报告');
  assert.equal(row.userAdded,true);
  assert.equal(row.question,'知识库文件清单中是否存在“设备测试报告”？只检查文件名，不检查文档内容。');
});
test('an existing edit flag remains true when a previously edited requirement is confirmed again',()=>{
  const input=[{...numeric(),userEdited:true}];
  assert.equal(confirmedRequirements(input,form([[0,input[0]]]))[0].userEdited,true);
});
test('changing an existing category marks the requirement as edited and updates its query type',()=>{
  const input=[documentRequirement()],before=structuredClone(input);
  const data=form([[0,input[0]]],{category_0:'功能条件'});
  const row=confirmedRequirements(input,data)[0];
  assert.equal(row.userEdited,true);
  assert.equal(row.type,'functional');
  assert.equal(row.document_name,'');
  assert.deepEqual(row.source,before[0].source);
  assert.equal(row.quote,before[0].quote);
  assert.deepEqual(input,before);
});
test('changing only a document name also marks the requirement as edited',()=>{
  const input=[documentRequirement()];
  const row=confirmedRequirements(input,form([[0,input[0]]],{document_0:'接口设计与测试说明书'}))[0];
  assert.equal(row.userEdited,true);
  assert.equal(row.document_name,'接口设计与测试说明书');
  assert.ok(row.question.includes('接口设计与测试说明书'));
});
test('changing a title or criterion marks an existing requirement as edited',()=>{
  for(const overrides of [{title_0:'最低续航时间'},{criterion_0:'续航时间不得低于10.5小时。'}]){
    const input=[numeric()];
    assert.equal(confirmedRequirements(input,form([[0,input[0]]],overrides))[0].userEdited,true);
  }
});
test('confirm rejects empty selections and selected cards without names or conditions',()=>{
  const input=[numeric()];
  assert.throws(()=>confirmedRequirements(input,new FormData()));
  for(const overrides of [{title_0:''},{title_0:'   '},{criterion_0:''},{criterion_0:'\n  '}]){
    assert.throws(()=>confirmedRequirements(input,form([[0,input[0]]],overrides)));
  }
});
test('document existence requires its own nonblank document-name field',()=>{
  const input=[documentRequirement()];
  for(const value of ['', '  '])assert.throws(()=>confirmedRequirements(input,form([[0,input[0]]],{document_0:value})));
});
test('confirm rejects invalid or deleted include indexes and unsupported categories',()=>{
  const input=[numeric(),null];
  for(const index of ['not-an-index','-1','5','0.5','1']){
    const data=form([[0,input[0]]]);data.set('include',index);
    assert.throws(()=>confirmedRequirements(input,data),index);
  }
  for(const category of ['', '商务条件', 'numeric']){
    assert.throws(()=>confirmedRequirements(input,form([[0,input[0]]],{category_0:category})));
  }
});
test('failed validation leaves all original requirements and their evidence unchanged',()=>{
  const input=[numeric(),documentRequirement()],before=structuredClone(input);
  const data=form([[0,input[0]],[1,input[1]]],{title_0:'先改名称',criterion_1:''});
  assert.throws(()=>confirmedRequirements(input,data));
  assert.deepEqual(input,before);
});

test('renumbering after exclusions preserves the server origin ID used for source validation',()=>{
  const first=numeric(),second=documentRequirement();
  const selected=confirmedRequirements([first,second],form([[1,second]]));
  assert.equal(selected[0].id,'R001');assert.equal(selected[0].originId,'R013');
  assert.deepEqual(selected[0].source,second.source);assert.equal(selected[0].quote,second.quote);
});

test('a manual item never claims an original source merely because it is renumbered R001',()=>{
  const first=numeric(),added={...manual(),id:'R014',title:'故障记录',criterion:'系统应保留故障记录。'};
  const selected=confirmedRequirements([first,added],form([[1,added]]));
  assert.equal(selected[0].id,'R001');assert.equal(selected[0].originId,null);
  assert.equal(selected[0].userAdded,true);assert.equal(selected[0].source.document_name,'人工补充');
});
