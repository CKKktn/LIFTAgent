// Pure state checks shared by the review UI and the Node regression tests.
function canonical(value){
  if(Array.isArray(value))return '['+value.map(canonical).join(',')+']';
  if(value&&typeof value==='object')return '{'+Object.keys(value).sort().map(key=>JSON.stringify(key)+':'+canonical(value[key])).join(',')+'}';
  return JSON.stringify(value??null);
}
function signature(text){
  let first=2166136261,second=2246822519;
  for(let i=0;i<text.length;i++){first=Math.imul(first^text.charCodeAt(i),16777619);second=Math.imul(second^text.charCodeAt(i),3266489917);}
  return text.length+':'+(first>>>0).toString(16)+':'+(second>>>0).toString(16);
}
export function baselineSignature(baseline){
  // Keep the original format so existing drafts can validate their source text.
  const text=JSON.stringify(baseline.map(source=>[source.document_id,source.id,source.content||source.content_with_weight]));
  let hash=2166136261;for(let i=0;i<text.length;i++)hash=Math.imul(hash^text.charCodeAt(i),16777619);
  return text.length+':'+(hash>>>0).toString(16);
}
export function scopeSignature(documents){
  const rows=documents.map(document=>({id:document.id,name:document.name,size:document.size,
    updated_at:document.updated_at??document.update_time??'',chunk_count:document.chunk_count,
    run:document.run,status:document.status,parser_config:document.parser_config??{}}));
  rows.sort((left,right)=>String(left.id).localeCompare(String(right.id))||String(left.name).localeCompare(String(right.name)));
  return 'scope-v1:'+signature(canonical(rows));
}
export function refreshReviewMaterials(report,baseline,documents){
  const currentBaseline=baselineSignature(baseline),currentScope=scopeSignature(documents);
  const hasResults=Boolean(report.results?.length);
  if(hasResults&&(!report.signature||!report.scopeSignature))throw Error('旧任务缺少完整材料版本记录，不能复用已有结论，请新建审查任务。');
  const changed=Boolean((report.signature&&report.signature!==currentBaseline)||(report.scopeSignature&&report.scopeSignature!==currentScope));
  if(hasResults&&changed)throw Error('依据或知识库资料已变更，不能混用旧结论，请新建审查任务。');
  const mustReconfirm=changed||Boolean(report.confirmed&&!report.signature);
  const next={...report,signature:currentBaseline,scopeSignature:currentScope};
  if(!hasResults&&mustReconfirm)Object.assign(next,{confirmed:false,awaitingConfirmation:false,extractionComplete:false,
    requirements:[],results:[],extractedParts:[],splitParts:[],filteredRequirements:[],issues:[],polished:[],
    rawReport:null,rawComplete:false,complete:false,finishedAt:null,targetVersion:null});
  return next;
}
export function assertScopeUnchanged(report,documents){
  if(!report.scopeSignature||report.scopeSignature!==scopeSignature(documents))throw Error('审查过程中知识库资料已变更，不能整理或复用本轮结论，请新建审查任务。');
}
export function getDefaultChatModel(models,defaults){
  return models.find(model=>model.model_id===defaults.find(item=>item.model_type==='chat')?.model_id);
}
