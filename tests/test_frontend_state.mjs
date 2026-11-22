import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';

// A data URL loads this browser ES module without requiring a Node package type.
const source=await readFile(new URL('../frontend/review-state.js',import.meta.url),'utf8');
const {baselineSignature,scopeSignature,refreshReviewMaterials,assertScopeUnchanged,getDefaultChatModel}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const filterSource=await readFile(new URL('../frontend/upload-filter.js',import.meta.url),'utf8');
const {uploadSkipReason}=await import('data:text/javascript;base64,'+Buffer.from(filterSource).toString('base64'));
const baseline=[{document_id:'contract',content:'设备续航时间不得低于8小时。'}];
const documents=[{id:'contract',name:'技术协议.txt',size:40,update_time:'2026-10-23T01:00:00Z',chunk_count:1,run:'DONE',status:'1',parser_config:{chunk_size:1000,chunk_overlap:120}},
  {id:'evidence',name:'项目方案.txt',size:30,update_time:'2026-10-23T01:00:00Z',chunk_count:1,run:'DONE',status:'1',parser_config:{chunk_size:1000,chunk_overlap:120}}];
const clone=value=>structuredClone(value);
function ready(){return refreshReviewMaterials({dbId:'record',dbRevision:3,signature:baselineSignature(baseline),confirmed:true,requirements:[{id:'R001'}],results:[],extractedParts:['B1:P1'],polished:[],issues:[]},baseline,documents);}

test('a new default response selects B for the next review instead of the previous A',()=>{
  const models=[{model_id:'A',name:'旧模型'},{model_id:'B',name:'新模型'}];
  assert.equal(getDefaultChatModel(models,[{model_type:'chat',model_id:'A'}]).name,'旧模型');
  const defaultsFromPatch=[{model_type:'chat',model_id:'B'},{model_type:'embedding',model_id:'local-features'}];
  assert.equal(getDefaultChatModel(models,defaultsFromPatch).name,'新模型');
  assert.equal(getDefaultChatModel(models,[]),undefined);
});
test('catalog order, parser option order, and timestamp alias do not change material versions',()=>{
  const reversed=clone(documents).reverse();
  for(const document of reversed){document.parser_config={chunk_overlap:120,chunk_size:1000};document.updated_at=document.update_time;delete document.update_time;}
  assert.equal(scopeSignature(reversed),scopeSignature(documents));
});
test('every material field, adding a file, and deleting a file changes the catalog signature',()=>{
  for(const [field,value] of Object.entries({id:'changed-id',name:'其他方案.txt',size:31,update_time:'2026-10-23T02:00:00Z',chunk_count:2,run:'RUNNING',status:'0',parser_config:{chunk_size:2000}})){
    const changed=clone(documents);changed[1][field]=value;
    assert.notEqual(scopeSignature(changed),scopeSignature(documents),field);
  }
  assert.notEqual(scopeSignature([...documents,{...documents[1],id:'additional'}]),scopeSignature(documents));
  assert.notEqual(scopeSignature(documents.slice(0,1)),scopeSignature(documents));
});
test('an unchanged confirmed task keeps its questions and answers on resume',()=>{
  const report={...ready(),results:[{requirementId:'R001',status:'已满足'}]};
  const next=refreshReviewMaterials(report,baseline,documents);
  assert.equal(next.confirmed,true);assert.deepEqual(next.requirements,report.requirements);assert.deepEqual(next.results,report.results);
});
test('changed source text with no answers clears extraction progress and requires confirmation again',()=>{
  const report={...ready(),awaitingConfirmation:true,extractionComplete:true,rawComplete:true,polished:[['旧摘要']]};
  const next=refreshReviewMaterials(report,[{...baseline[0],content:'设备续航时间不得低于12小时。'}],documents);
  assert.equal(next.confirmed,false);assert.equal(next.awaitingConfirmation,false);assert.equal(next.extractionComplete,false);
  assert.deepEqual(next.requirements,[]);assert.deepEqual(next.extractedParts,[]);assert.deepEqual(next.polished,[]);
  assert.equal(next.rawComplete,false);assert.equal(next.dbId,'record');assert.equal(next.dbRevision,3);
  assert.equal(report.confirmed,true);assert.equal(report.requirements.length,1,'input report must not be mutated');
});
test('changed evidence catalog with no answers also starts extraction and confirmation afresh',()=>{
  const changed=clone(documents);changed[1].chunk_count=2;
  const next=refreshReviewMaterials(ready(),baseline,changed);
  assert.equal(next.confirmed,false);assert.deepEqual(next.requirements,[]);
});
test('answered tasks reject changed sources or evidence rather than keeping mixed conclusions',()=>{
  const report={...ready(),results:[{requirementId:'R001',status:'已满足'}]};
  assert.throws(()=>refreshReviewMaterials(report,[{...baseline[0],content:'新的合同条件'}],documents),/请新建审查任务/);
  const changed=clone(documents);changed[1].update_time='2026-10-23T03:00:00Z';
  assert.throws(()=>refreshReviewMaterials(report,baseline,changed),/请新建审查任务/);
});
test('an old answered draft without a scope version cannot certify its old evidence',()=>{
  const legacy={signature:baselineSignature(baseline),confirmed:true,results:[{requirementId:'R001'}]};
  assert.throws(()=>refreshReviewMaterials(legacy,baseline,documents),/旧任务缺少完整材料版本/);
});
test('an unversioned confirmed draft without answers must extract and confirm again',()=>{
  const next=refreshReviewMaterials({confirmed:true,awaitingConfirmation:true,requirements:[{id:'old'}],results:[],extractionComplete:true},baseline,documents);
  assert.equal(next.confirmed,false);assert.equal(next.awaitingConfirmation,false);assert.deepEqual(next.requirements,[]);
});
test('material changes during RAG are rejected before report polishing',()=>{
  const report=ready();assert.doesNotThrow(()=>assertScopeUnchanged(report,documents));
  const changed=clone(documents);changed[1].status='0';
  assert.throws(()=>assertScopeUnchanged(report,changed),/审查过程中知识库资料已变更/);
});

test('supported YAML and TSV files remain uploadable inside selected folders',()=>{
  assert.equal(uploadSkipReason({name:'条款.yaml',webkitRelativePath:'资料/条款.yaml',size:100}),'');
  assert.equal(uploadSkipReason({name:'参数.TSV',size:100}),'');
  assert.equal(uploadSkipReason({name:'协议.odt',size:100}),'');
});
test('unsupported image and audio files are rejected before calling the parser',()=>{
  for(const name of ['图纸.gif','录音.mp3'])assert.match(uploadSkipReason({name,size:100}),/本系统暂不支持/);
});
test('temporary files and empty documents are still excluded',()=>{
  assert.equal(uploadSkipReason({name:'~$协议.docx',size:100}),'临时文件或系统文件');
  assert.equal(uploadSkipReason({name:'参数.csv',size:0}),'空文件');
});
