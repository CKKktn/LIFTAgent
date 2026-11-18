import {ask,verifiedQuote,fatalReviewError,noteIssue,questionFor,reviewQuestion} from './review.js';
import {$,esc,sourcesHtml} from './ui.js';

export {questionFor,QUESTION_RULES} from './review.js';
const requirementTypes={'文档存在':'document','数值指标':'numeric','功能条件':'functional'};
export function confirmedRequirements(drafts,form){
  const seen=new Set();
  const rows=form.getAll('include').map(value=>{
    if(!/^\d+$/.test(String(value)))throw Error('条目已变更，请重新打开指标清单');
    const index=Number(value),origin=drafts[index];
    if(!origin||seen.has(index))throw Error('条目已变更，请重新打开指标清单');seen.add(index);
    const label=origin.id||'补充条目',category=String(form.get('category_'+index)||'');
    const type=Object.hasOwn(requirementTypes,category)?requirementTypes[category]:null;if(!type)throw Error(label+' 的分类无效');
    const title=String(form.get('title_'+index)||'').trim(),criterion=String(form.get('criterion_'+index)||'').trim();
    if(!title||!criterion)throw Error('请填写 '+label+' 的指标名称和达标条件');
    const documentName=type==='document'?String(form.get('document_'+index)||'').trim():'';
    if(type==='document'&&!documentName)throw Error('请填写 '+label+' 的文档名称');
    const changed=title!==origin.title||criterion!==origin.criterion||type!==origin.type||category!==origin.category||documentName!==(origin.document_name||'');
    return {...origin,originId:origin.userAdded?null:(origin.originId||origin.id),title,criterion,category,type,document_name:documentName,...(origin.userAdded?{subject:title}:{}),userEdited:Boolean(origin.userEdited||changed)};
  });
  if(!rows.length)throw Error('至少保留一项指标');
  return rows.map((r,i)=>({...r,id:'R'+String(i+1).padStart(3,'0'),question:questionFor(r)}));
}
export function requirementEditorHtml(drafts){
  return '<p class="hint">核对类型、条件和筛选理由，可修改或取消无关项。点击底部“＋”逐条添加补充条件，默认分类为功能条件。</p><p id="requirementEditorCount" class="requirement-editor-count" role="status" aria-live="polite"></p><div id="requirementEditor">'+drafts.map((r,i)=>requirementCard(r,i)).join('')+'</div>';
}
function requirementCard(r,i){
  const category=Object.hasOwn(requirementTypes,r.category)?r.category:({'document':'文档存在','numeric':'数值指标','functional':'功能条件'}[r.type]||'功能条件');
  return `<fieldset class="requirement-edit${r.userAdded?' is-manual':''}" data-requirement-row="${i}"><div class="requirement-edit-heading"><label class="requirement-include"><input type="checkbox" name="include" value="${i}" checked> <b>${esc(r.id)}</b> · 纳入审查${r.userAdded?'<span class="badge">人工补充</span>':''}</label>${r.userAdded?'<button type="button" class="btn subtle small requirement-remove" aria-label="删除 '+esc(r.id)+' 补充条件">删除</button>':''}</div><label for="requirementCategory${i}">分类</label><select id="requirementCategory${i}" name="category_${i}">${Object.keys(requirementTypes).map(c=>`<option ${category===c?'selected':''}>${c}</option>`).join('')}</select><label for="requirementTitle${i}">指标名称</label><input id="requirementTitle${i}" name="title_${i}" value="${esc(r.title)}" placeholder="例如：远程故障告警" required><label for="requirementCriterion${i}">达标条件</label><textarea id="requirementCriterion${i}" name="criterion_${i}" rows="3" placeholder="填写需要核查的具体功能、参数或条件" required>${esc(r.criterion)}</textarea><div class="requirement-document-field"${category==='文档存在'?'':' hidden'}><label for="requirementDocument${i}">文档名称</label><input id="requirementDocument${i}" name="document_${i}" value="${esc(r.document_name||(category==='文档存在'?r.title:''))}" placeholder="例如：接口设计说明书"></div><p class="hint">筛选理由：${esc(r.rationale||(r.userAdded?'人工补充条件，请自行核对合同依据。':'人工指定'))}</p><p class="hint">${r.userAdded?'依据说明':'原文'}：${esc(r.quote||(r.userAdded?'尚未关联合同原文。':''))}</p><details class="requirement-question"><summary>查看本条问句提示词</summary><p class="hint"></p></details></fieldset>`;
}
export function mountRequirementEditor(drafts){
  const editor=$('requirementEditor'),dialog=$('modal');
  dialog.classList.add('requirement-editor-modal');
  const add=document.createElement('button');add.type='button';add.className='btn subtle requirement-add';add.dataset.modalExtra='';add.id='addRequirement';add.textContent='＋';add.title='添加补充条件';add.setAttribute('aria-label','添加补充条件');
  $('modalCancel').before(add);
  dialog.addEventListener('close',()=>add.remove(),{once:true});
  function refreshCount(){const cards=editor.querySelectorAll('[data-requirement-row]');$('requirementEditorCount').textContent=`已纳入 ${[...cards].filter(card=>card.querySelector('[name="include"]').checked).length} / ${cards.length} 项条件`;
  }
  function bindCard(card){
    const index=Number(card.dataset.requirementRow),origin=drafts[index];
    const include=card.querySelector('[name="include"]'),category=card.querySelector('select'),title=card.querySelector(`[name="title_${index}"]`),criterion=card.querySelector('textarea'),documentName=card.querySelector(`[name="document_${index}"]`),documentField=card.querySelector('.requirement-document-field');
    function update(){
      const enabled=include.checked,isDocument=category.value==='文档存在';
      title.required=criterion.required=enabled;documentName.required=enabled&&isDocument;documentName.disabled=!isDocument;documentField.hidden=!isDocument;
      card.classList.toggle('is-excluded',!enabled);
      const current={...origin,title:title.value.trim(),criterion:criterion.value.trim(),category:category.value,type:requirementTypes[category.value],document_name:documentName.value.trim()};
      card.querySelector('.requirement-question p').textContent=current.title&&current.criterion?questionFor(current):'填写指标名称和达标条件后，将自动生成本条问句。';
      refreshCount();
    }
    card.addEventListener('input',update);card.addEventListener('change',update);
    const remove=card.querySelector('.requirement-remove');if(remove)remove.onclick=()=>{drafts[index]=null;card.remove();refreshCount();add.focus();};
    update();
  }
  editor.querySelectorAll('[data-requirement-row]').forEach(bindCard);
  let nextNumber=Math.max(0,...drafts.map(r=>Number(/^R(\d+)$/.exec(r.id||'')?.[1])||0))+1;
  add.onclick=()=>{
    const index=drafts.length,item={id:'R'+String(nextNumber++).padStart(3,'0'),type:'functional',category:'功能条件',title:'',criterion:'',document_name:'',userAdded:true,rationale:'人工补充条件，请自行核对合同依据。',quote:'人工补充指标，请人工核对合同依据',source:{document_name:'人工补充'}};
    drafts.push(item);editor.insertAdjacentHTML('beforeend',requirementCard(item,index));const card=editor.lastElementChild;bindCard(card);
    requestAnimationFrame(()=>{card.scrollIntoView({block:'start',behavior:'instant'});card.querySelector(`[name="title_${index}"]`).focus({preventScroll:true});});
  };
  refreshCount();
}
export function outsideTechnicalScope(r){
  const subject=[r.title,r.subject,r.document_name].filter(Boolean).join(' ');
  return /合同总价|合同价款|首付款|预付款|尾款|付款条件|支付条件|支付金额|发票|税率|违约金|履约保证金|合作伙伴.*申报|廉洁|保密承诺|授权委托|营业执照|资质证书|银行账户|收款账户|行政审批|供应商注册/.test(subject);
}
export async function extractProjectQuestions({report,baseline,signal,progress,onUpdate}){
  if(report.extractionComplete&&report.extractionRevision===5)return;
  report.requirements??=[];report.extractedParts??=[];
  // New paragraph IDs cannot reuse old batch offsets. Keep valid requirements, deduplicate below.
  if(report.extractionRevision!==5){report.requirements=[];report.extractedParts=[];report.splitParts=[];report.extractionRevision=5;report.issues=(report.issues||[]).filter(x=>x.phase!=='提取要求');}
  const parts=[];
  for(const source of baseline){
    const paragraphs=[];let n=0;
    for(const line of source.content.split(/\n+/).filter(x=>x.trim()))for(let start=0;start<line.length;start+=600)paragraphs.push({id:'P'+(++n),text:line.slice(start,start+600)});
    let rows=[],size=0;
    for(const row of paragraphs){if(size+row.text.length>6000&&rows.length){parts.push({source,rows,key:source.reviewId+':'+rows[0].id});rows=[];size=0;}rows.push(row);size+=row.text.length;}
    if(rows.length)parts.push({source,rows,key:source.reviewId+':'+rows[0].id});
  }
  function retain(value,part){
    const map=new Map(part.rows.map(r=>[r.id,r.text]));let kept=0,rejected=0;
    for(const field of ['technical_indicators','closure_conditions'])for(const r of Array.isArray(value?.[field])?value[field]:[]){
      let quote='';
      if(Array.isArray(r.source_ids)&&r.source_ids.length&&r.source_ids.every(id=>map.has(id)))quote=[...new Set(r.source_ids)].map(id=>map.get(id)).join('\n');
      else if(verifiedQuote(r.quote,part.rows.map(x=>x.text).join('\n')))quote=r.quote;
      if(typeof r.title!=='string'||!r.title.trim()||typeof r.criterion!=='string'||!r.criterion.trim()||!quote){rejected++;continue;}
      if(!['document','numeric','functional'].includes(r.type)||typeof r.subject!=='string'||!r.subject.trim()||typeof r.rationale!=='string'||!r.rationale.trim()||(r.type==='document'&&(!r.document_name||typeof r.document_name!=='string'))){rejected++;continue;}
      if(outsideTechnicalScope(r)){(report.filteredRequirements??=[]).push({title:r.title,reason:'商务、财务或行政事项，不属于技术核查范围'});continue;}
      const category={document:'文档存在',numeric:'数值指标',functional:'功能条件'}[r.type];
      const keyValue=r.type+'|'+String(r.type==='document'?r.document_name:r.subject).replace(/\s/g,'')+'|'+r.criterion.replace(/\s/g,'');
      if(report.requirements.some(x=>x.key===keyValue||(x.category===category&&x.subject===r.subject&&x.document_name===r.document_name&&x.criterion.replace(/\s/g,'')===r.criterion.replace(/\s/g,''))))continue;
      const item={...r,quote,category,key:keyValue,id:'R'+String(report.requirements.length+1).padStart(3,'0'),source:{...part.source,content:quote}};
      item.question=questionFor(item);report.requirements.push(item);kept++;
    }return {kept,rejected};
  }
  async function process(part,depth=0){
    if(report.extractedParts.includes(part.key))return;
    signal.throwIfAborted();
    const sources=[{file:part.source.document_name,id:part.key}];
    async function split(){
      const mid=Math.ceil(part.rows.length/2);
      for(const [i,rows] of [part.rows.slice(0,mid),part.rows.slice(mid)].entries())await process({...part,rows,key:part.key+'/'+i},1);
      if([0,1].every(i=>report.extractedParts.includes(part.key+'/'+i)))report.extractedParts.push(part.key);
    }
    if(report.splitParts?.includes(part.key)){await split();onUpdate(report);return;}
    try{
      const value=await ask(`从合同或技术协议筛选真正需要核查的项目技术要求。此次是技术方案符合性审查，不是合同履约或行政合规审计。只保留类似“无人车续航时间”“线缆功率容量”的设备性能、技术参数、功能实现、工程约束、接口兼容、环境适应性、容量及安全技术条件。明确排除合同总价、首付款金额及支付条件、付款节点、发票、税费、违约金、资质/廉洁/保密/合作伙伴主动申报事项表、供应商手续及其他商务行政文书，即使有数字或写了“应提交”也不能当成技术指标。document 类型仅保留直接证明设计/实现/测试/验收的技术交付物（设计方案、测试报告、操作手册、图纸等），不提取行政表单。不能只看“应、须、不得低于”等词，需要先判断核查对象是否属于工程技术范围。仅纳入三类：document 文档存在（明确要求提交某份文件，每份一项，document_name 为文件名称核心词，不含“提交/一份”等）；numeric 数值指标（具体对象、明确量值/范围/单位/方向/适用条件）；functional 功能条件（明确对象必须支持某功能或具备某属性，无需数值）。判断语义，不依赖固定关键词。必须有本项目明确要求及可检查对象。排除目录/章节标题、背景介绍、研究意义、别的项目指标、例子、定义、泛泛研究目标、无具体验收对象的任务口号、付款/签字/进度等非这三类事项。不要把标题“技术指标”“研究内容”作为指标。相同义务合并，文档交付与文档内容指标分别归类。每项 subject 写核查对象，rationale 简短说明为什么属于明确要求。数值指标用于比较项目方案，不强制提供实测数据。功能条件用于判断是否描述实现，不强制数值。只引用真实 source_ids，程序回填原文。输出 JSON {"technical_indicators":[{"type":"numeric 或 functional","subject":"核查对象","title":"名称","criterion":"完整条件含单位和适用条件","rationale":"明确义务依据","source_ids":["P1"]}],"closure_conditions":[{"type":"document","subject":"交付文档","title":"文件名","document_name":"文件名称核心词","criterion":"须提供该文件","rationale":"明确交付要求","source_ids":["P2"]}]}。没有要求则对应数组为空。\n文件：${part.source.document_name}\n原文段落：${JSON.stringify(part.rows)}`,{model:report.model,signal,attempts:1,maxTokens:4096},v=>{
        if(!Array.isArray(v.technical_indicators)||!Array.isArray(v.closure_conditions))throw Error('输出缺少指标或结题条件数组');
      });
      const {kept,rejected}=retain(value,part);
      report.issues=(report.issues||[]).filter(x=>!(x.phase==='提取要求'&&x.sources?.some(s=>s.id===part.key)));
      if(rejected)noteIssue(report,{phase:'提取要求',sources,retained:kept,reason:`${rejected} 条记录缺少条件或有效原文编号，已隔离；请在指标清单中人工补充。有效记录不重跑。`});
      report.extractedParts.push(part.key);
    }catch(e){
      if(fatalReviewError(e,signal))throw e;
      // A failed large response gets one smaller pass, never three full regenerations.
      if(depth===0&&part.rows.length>1){
        (report.splitParts??=[]).push(part.key);await split();
      }else{const {kept}=retain(e.value,part);noteIssue(report,{phase:'提取要求',sources,retained:kept,reason:e.message+'；已保留有效指标，未完成部分可继续重试'});}
    }
    onUpdate(report);
  }
  for(let i=0;i<parts.length;i++){
    progress(1,`第一步 · 提取指标与结题条件 ${i+1}/${parts.length} · 已保留 ${report.requirements.length} 项`);
    await process(parts[i]);
  }
  if(!report.requirements.length)throw Error('未取得可验证指标或结题条件，请检查解析文字或模型响应；失败部分可重试');
  report.extractionComplete=parts.every(p=>report.extractedParts.includes(p.key));onUpdate(report);
}
export function rawReview(report){
  return report.requirements.map(r=>{const a=report.results.find(x=>x.requirementId===r.id);return {id:r.id,type:r.type,category:r.category,question:reviewQuestion(r),criterion:r.criterion,answer:a||{status:'未回答'}};});
}
export async function polishReport({report,signal,progress,onUpdate}){
  report.rawReport=rawReview(report);report.rawComplete=true;onUpdate(report);
  report.polished??=[];
  // Each batch includes complete answers and evidence. Never silently truncate the raw report.
  const editingMaterial=report.rawReport.map(x=>({...x,answer:{...x.answer,evidence:(x.answer.evidence||[]).map(e=>({source_id:e.source_id,quote:e.quote,analysis:e.analysis,kind:e.kind,file:e.source?.document_name}))}}));
  const groups=[];let group=[],size=0;for(const item of editingMaterial){const len=JSON.stringify(item).length;if(group.length&&size+len>22000){groups.push(group);group=[];size=0;}group.push(item);size+=len;}if(group.length)groups.push(group);
  for(let i=report.polished.length;i<groups.length;i++){
    signal.throwIfAborted();progress(3,`第三步 · 将原始汇报整理为网页 ${i+1}/${groups.length}`);
    const group=groups[i];
    try{
      const value=await ask(`你是审查汇报编辑。将下列原始问答语料整理成可视化网页的数据，先结论后分析，语言清楚、结构严谨。必须区分文档存在、数值方案符合、功能实现描述三种核查口径，方案符合不得写成实测达标。不得修改判定、数值、引文和来源，不得补充不存在的事实。不输出 HTML、脚本或思考过程。输出 JSON {"sections":[{"id":"原始id","status":"原始判定，必须完全一致","headline":"简洁结论标题","summary":"忠实概括原始分析，不添加事实","action":"原始建议的清楚表述"}]}，每个原始条目恰好对应一个 section。网页统计图由程序根据原始判定计算，禁止自行生成统计。\n原始汇报：${JSON.stringify(group)}`,{model:report.model,signal,attempts:1,maxTokens:4096},v=>{if(!Array.isArray(v.sections)||v.sections.length!==group.length)throw Error('整理条目数量不符');const seen=new Set();for(const x of v.sections){const origin=group.find(r=>r.id===x.id);if(!origin||seen.has(x.id)||x.status!==origin.answer.status)throw Error('润色不得改变判定或条目');seen.add(x.id);for(const k of ['headline','summary','action'])if(typeof x[k]!=='string'||!x[k].trim())throw Error('缺少 '+k);}});
      report.polished.push(value.sections);
    }catch(e){if(fatalReviewError(e,signal))throw e;noteIssue(report,{phase:'网页整理',reason:e.message});report.polished.push(group.map(x=>({id:x.id,status:x.answer.status,headline:x.question,summary:x.answer.analysis||'未回答',action:x.answer.recommendation||'人工复核'})));}
    onUpdate(report);
  }
  report.complete=true;report.limited=Boolean(report.issues?.length||report.skippedFiles?.length);report.finishedAt=new Date().toISOString();onUpdate(report);
}
export function summaryBoard(report){
  const statuses=['已满足','部分满足','不满足','证据不足','待人工复核','证据冲突','未核查'];
  const colors=['#38b878','#e6ad43','#e46868','#689bdb','#a68cce','#e28050','#858585'];
  const results=new Map(report.results.map(r=>[r.requirementId,r]));
  const count=status=>report.requirements.filter(r=>(results.get(r.id)?.status||'未核查')===status).length;
  const total=report.requirements.length,done=report.requirements.filter(r=>results.has(r.id)).length;
  const sources=new Set(report.results.flatMap(r=>(r.evidence||[]).map(e=>e.source?.document_id).filter(Boolean)));
  let pos=0;const slices=statuses.map((s,i)=>{const start=pos;pos+=total?count(s)/total*100:0;return `${colors[i]} ${start}% ${pos}%`;});
  const categories=['文档存在','数值指标','功能条件'];
  const summaries=new Map((report.polished||[]).flat().map(x=>[x.id,x]));
  const risks=report.requirements.filter(r=>['不满足','证据冲突','部分满足','证据不足','待人工复核'].includes(results.get(r.id)?.status));
  return `<section class="summary-board"><h2>第三步 · 技术审查看板</h2><p class="hint">${report.complete?'本轮结果':'阶段结果'} · 统计覆盖全部 ${total} 项要求；方案符合不等同实测或项目验收通过。</p><div class="board-kpis">${[['纳入要求',total],['已核查',done],['已满足',count('已满足')],['需关注',risks.length],['证据文件',sources.size]].map(([label,n])=>`<div><strong>${n}</strong><span>${label}</span></div>`).join('')}</div><div class="board-charts"><div class="board-box"><h3>判定分布</h3><div class="board-donut" style="background:conic-gradient(${total?slices.join(','):'#858585 0% 100%'})" role="img" aria-label="${esc(statuses.map(s=>s+count(s)+'项').join('，'))}"><div><strong>${total?Math.round(done/total*100):0}%</strong><span>核查进度</span></div></div><div class="board-legend">${statuses.map((s,i)=>`<span><i style="background:${colors[i]}"></i>${s}<b>${count(s)}</b></span>`).join('')}</div></div><div class="board-box"><h3>分类核查情况</h3>${categories.map(c=>{const rows=report.requirements.filter(r=>r.category===c),n=rows.length;return `<div class="board-category"><div>${esc(c)} <b>${n} 项</b></div><div class="board-stack" role="img" aria-label="${esc(c+'：'+statuses.map(s=>s+rows.filter(r=>(results.get(r.id)?.status||'未核查')===s).length+'项').join('，'))}">${statuses.map((s,i)=>{const num=rows.filter(r=>(results.get(r.id)?.status||'未核查')===s).length;return `<span title="${s} ${num} 项" style="width:${n?num/n*100:0}%;background:${colors[i]}">${num||''}</span>`;}).join('')}</div></div>`;}).join('')}<h3>阅读口径</h3><p>文档：仅确认存在。数值：对比项目方案。功能：核对实现描述。灰色为未核查；证据不足不等于不满足。</p></div></div><details class="board-box" open><summary>问题与后续行动 · ${risks.length} 项</summary><div class="board-table"><table><thead><tr><th>指标</th><th>判定</th><th>主要发现</th><th>下一步</th></tr></thead><tbody>${risks.map(r=>{const a=results.get(r.id),p=summaries.get(r.id);return `<tr><td>${esc(r.id+' '+r.title)}</td><td>${esc(a.status)}</td><td>${esc(p?.summary||a.analysis)}</td><td>${esc(p?.action||a.recommendation)}</td></tr>`;}).join('')||'<tr><td colspan="4">当前已核查项暂无需关注问题；尚未核查项不计入结论。</td></tr>'}</tbody></table></div></details><details class="board-box"><summary>全部指标摘要 · ${total} 项</summary><div class="polished-grid">${report.requirements.map(r=>{const a=results.get(r.id),p=summaries.get(r.id);return `<article><span class="badge">${esc(r.id)} · ${esc(a?.status||'未核查')}</span><h3>${esc(p?.headline||r.title)}</h3><p>${esc(p?.summary||a?.analysis||'尚未核查')}</p></article>`;}).join('')}</div></details></section>`;
}
export function workflowHtml(report){
  if(report.mode!=='three-stage')return '';
  const groups=['文档存在','数值指标','功能条件'];
  const raw=(report.rawReport||rawReview(report)).map(row=>{
    const requirement=report.requirements.find(r=>r.id===row.id);
    return requirement?{...row,question:reviewQuestion({...requirement,question:row.question})}:row;
  });
  return `<section class="workflow-output"><h2>第一步 · 指标与结题问句</h2><p class="hint">${(report.textExtraction||[]).map(x=>esc(x.name)+'：'+esc(x.method)+'（'+x.characters+' 字符）').join('；')}</p>${groups.map(category=>`<details><summary>${category} · ${report.requirements.filter(r=>r.category===category).length} 项</summary>${report.requirements.filter(r=>r.category===category).map(r=>`<div class="review-evidence"><b>${esc(r.id)} ${esc(r.title)}</b><p>${esc(reviewQuestion(r))}</p>${sourcesHtml([r.source||{}],r.title)}</div>`).join('')}</details>`).join('')}<details><summary>第二步 · 原始版审查汇报（已回答 ${report.results.length} 项）</summary><pre class="raw-review">${esc(JSON.stringify(raw,null,2))}</pre></details>${report.results.length?summaryBoard(report):''}</section>`;
}
