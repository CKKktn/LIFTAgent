import {esc,sourcesHtml,evidenceReference,evidenceDisplayEntries,drawingEvidenceOf} from './ui.js';
import {reviewQuestion,issueText} from './review.js';

const VERDICTS=['已满足','部分满足','不满足','证据不足','待人工复核','证据冲突'];
const STATUSES=[...VERDICTS,'未核查'];
const ATTENTION=new Set(VERDICTS.filter(status=>status!=='已满足'));
const CATEGORIES=['文档存在','数值指标','功能条件','其他要求'];
const TONES={'已满足':'positive','部分满足':'warning','不满足':'negative','证据不足':'info','待人工复核':'review','证据冲突':'conflict','未核查':'pending'};
const array=value=>Array.isArray(value)?value:[];
const object=value=>value&&typeof value==='object'&&!Array.isArray(value);
const text=value=>String(value??'');
const clip=(value,length=120)=>{const content=text(value);return content.length>length?content.slice(0,length)+'…':content;};
const TYPE_CATEGORIES={document:'文档存在',numeric:'数值指标',functional:'功能条件'};
const categoryOf=r=>TYPE_CATEGORIES[r.type]||(CATEGORIES.includes(r.category)?r.category:'其他要求');
const typeOf=(r,result)=>TYPE_CATEGORIES[r.type]?r.type:Object.keys(TYPE_CATEGORIES).find(type=>TYPE_CATEGORIES[type]===r.category)||(TYPE_CATEGORIES[result?.checkType]?result.checkType:'');
const chip=status=>`<span class="rw-chip rw-tone-${TONES[status]||'pending'}"><i aria-hidden="true"></i>${esc(status)}</span>`;

export function reportWorkspaceMetrics(report){
  const record=object(report)?report:{};
  const requirements=[],known=new Set();
  for(const [index,requirement] of array(record.requirements).entries()){
    if(!object(requirement))continue;
    const rawId=text(requirement.id).trim(),key=rawId||'__anonymous_'+index;
    if(known.has(key))continue;known.add(key);
    requirements.push({requirement,key,id:rawId||'条目 '+(index+1)});
  }
  const results=new Map();
  for(const result of array(record.results)){
    if(!object(result)||!VERDICTS.includes(result.status))continue;
    const key=text(result.requirementId).trim();
    if(key&&known.has(key))results.set(key,result);
  }
  const counts=Object.fromEntries(STATUSES.map(status=>[status,0])),files=new Set();
  const rows=requirements.map(({requirement,key,id},index)=>{
    const result=results.get(key),status=result?.status||'未核查';counts[status]++;
    const evidence=array(result?.evidence).filter(object);
    for(const item of evidence){const identifier=text(item.source?.document_id).trim();if(identifier)files.add(identifier);}
    return {index,key,id,requirement,result,evidence,status,category:categoryOf(requirement)};
  });
  const total=rows.length,pending=counts['未核查'],done=total-pending;
  return {total,done,pending,counts,attention:rows.filter(row=>ATTENTION.has(row.status)).length,sourceFiles:files.size,rows};
}

function reportPhase(report,metrics){
  if(!metrics.total)return {name:'待提取指标',tone:'pending',headline:'从审查依据中建立核查清单',description:'完成文字提取并确认指标后，这里会展示结论、待处理事项和来源证据。'};
  if(report.awaitingConfirmation||(report.mode==='three-stage'&&!report.confirmed&&!metrics.done))return {name:'待人工确认',tone:'info',headline:'指标清单已提取，等待确认核查范围',description:'请核对指标名称、达标条件与原文，确认后再开始逐条问答。当前清单尚未形成审查结论。'};
  if(!report.complete)return {name:'审查进行中',tone:'info',headline:`已完成 ${metrics.done} 项核查，结果持续更新`,description:'已生成的结果可先查看；尚未核查的条目不会被计为已满足，整体结论需等待本轮处理结束。'};
  if(metrics.pending)return {name:'部分完成',tone:'warning',headline:`仍有 ${metrics.pending} 项要求尚未核查`,description:'本轮记录已结束，但核查清单尚未全部覆盖。请查看处理异常并继续核查或进行人工复核。'};
  if(report.limited||array(report.issues).length||array(report.skippedFiles).length)return {name:'处理结束 · 需复核',tone:'warning',headline:'本轮处理结束，仍需核对覆盖边界',description:'存在跳过文件、处理异常或其他范围限制，请结合原文和处理记录复核，不能据此确认整体完成。'};
  if(metrics.counts['不满足'])return {name:'核查已完成',tone:'negative',headline:`发现 ${metrics.counts['不满足']} 项明确不符合要求`,description:'请优先查看对应指标的事实依据及整改建议，再逐项核对其他证据缺口和待复核事项。'};
  if(metrics.attention)return {name:'核查已完成',tone:'warning',headline:`有 ${metrics.attention} 项需要进一步处理或复核`,description:'部分条件、证据或判定仍需确认。证据不足不等于未完成，方案符合也不等同实测或验收达标。'};
  return {name:'核查已完成',tone:'positive',headline:'已核查要求均有支持性结果',description:'请完成最终人工复核，并核对适用条件、证据性质和原文。这里的结果不替代项目验收结论。'};
}

function dateLabel(value){
  if(!value)return '未记录';const date=new Date(value);
  return Number.isNaN(date.getTime())?text(value):new Intl.DateTimeFormat('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(date);
}
function sourceFor(evidence){
  const source=object(evidence?.source)?evidence.source:{};
  return {...source,document_name:text(source.document_name||source.doc_name||source.name||'来源文件'),content:text(evidence?.quote??source.content??source.content_with_weight??'')};
}
function jsonText(value){try{return JSON.stringify(value,null,2);}catch{return '原始数据暂时无法展开，请使用页面上方的原始数据导出。';}}
function contentKey(value){
  const input=text(value);let hash=2166136261;
  for(let index=0;index<input.length;index++)hash=Math.imul(hash^input.charCodeAt(index),16777619);
  return (hash>>>0).toString(36)+':'+input.length;
}
function sourceBlock(source,question,key){
  // Each call renders one original source. Semantic keys do not shift when
  // another requirement or an earlier evidence block receives new content.
  return sourcesHtml([source],question,{compact:true}).replace(/<details class="(sources|source-full)"/g,(_,kind)=>`<details class="${kind}" data-rw-detail="${esc(key+':'+kind)}"`);
}

function questionForView(requirement){try{return reviewQuestion(requirement);}catch{return text(requirement.question||requirement.criterion);}}
function scopeIssues(report){
  const issues=array(report.issues).filter(object).map(item=>({...item,sources:array(item.sources).filter(object)}));
  return issueText({...report,issues,skippedFiles:array(report.skippedFiles).filter(object)});
}

function overview(report,metrics,phase){
  const ordered=metrics.rows.filter(row=>ATTENTION.has(row.status));
  const rank={'不满足':0,'证据冲突':1,'部分满足':2,'待人工复核':3,'证据不足':4};
  ordered.sort((a,b)=>(rank[a.status]-rank[b.status])||(a.index-b.index));
  const attentionRows=ordered.slice(0,4);
  const distribution=STATUSES.map(status=>`<span class="rw-distribution-segment rw-tone-${TONES[status]}" style="width:${metrics.total?metrics.counts[status]/metrics.total*100:0}%" title="${esc(status)} ${metrics.counts[status]} 项"></span>`).join('');
  const assessment=`<section class="rw-panel rw-assessment rw-assessment-${phase.tone}"><div class="rw-section-kicker">本轮结论</div><h3>${esc(phase.headline)}</h3><p>${esc(phase.description)}</p><div class="rw-assessment-progress"><div class="rw-progress-label"><span>核查进度</span><strong>${metrics.done} / ${metrics.total} 项</strong></div><div class="rw-progress" role="progressbar" aria-label="核查进度" aria-valuemin="0" aria-valuemax="${Math.max(1,metrics.total)}" aria-valuenow="${metrics.done}"><span style="width:${metrics.total?metrics.done/metrics.total*100:0}%"></span></div></div><div class="rw-assessment-note">核查进度表示已处理的条目数量，不表示项目验收通过率。</div></section>`;
  const attention=`<section class="rw-panel rw-attention-panel"><div class="rw-section-heading"><div><div class="rw-section-kicker">处理与复核${metrics.attention>4?' · 先展示 4 项':''}</div><h3>需要关注的事项 <span class="rw-count">${metrics.attention}</span></h3></div>${metrics.attention?'<button type="button" class="rw-text-button" data-rw-attention>查看全部事项 ↗</button>':''}</div>${attentionRows.length?`<div class="rw-attention-list">${attentionRows.map(row=>`<button type="button" class="rw-attention-row" data-rw-open-item="${esc(row.id)}"><span class="rw-row-id">${esc(row.id)}</span><span class="rw-attention-copy"><strong>${esc(row.requirement.title||'未命名要求')}</strong><span>${esc(clip(row.result?.recommendation||row.result?.gaps||row.result?.analysis||'请结合原文进一步复核。',115))}</span></span>${chip(row.status)}<span class="rw-row-arrow" aria-hidden="true">↗</span></button>`).join('')}</div>`:`<div class="rw-quiet-empty"><span class="rw-empty-icon" aria-hidden="true">${metrics.pending||!report.complete?'○':'✓'}</span><div><strong>${metrics.pending||!report.complete?'暂无已确认的关注事项':'当前已核查项暂无关注事项'}</strong><p>${metrics.pending||!report.complete?'未核查项与尚未形成的结论不计入此处，请继续完成核查。':'请继续核对证据性质和适用条件，并完成最终人工复核。'}</p></div></div>`}</section>`;
  const verdicts=`<section class="rw-panel rw-distribution-panel"><div class="rw-section-heading"><div><div class="rw-section-kicker">全部纳入要求</div><h3>判定分布</h3></div><button type="button" class="rw-text-button" data-rw-show-items>查看核查清单 ↗</button></div><div class="rw-distribution" role="img" aria-label="${esc(STATUSES.map(status=>status+metrics.counts[status]+'项').join('，'))}">${distribution}</div><div class="rw-distribution-legend">${STATUSES.map(status=>`<button type="button" data-rw-filter-status="${esc(status)}" class="rw-legend-item rw-tone-${TONES[status]}"><i aria-hidden="true"></i><span>${esc(status)}</span><strong>${metrics.counts[status]}</strong></button>`).join('')}</div></section>`;
  const categories=`<section class="rw-panel rw-category-panel"><div class="rw-section-heading"><div><div class="rw-section-kicker">范围构成</div><h3>核查对象</h3></div><span class="rw-small-label">${metrics.total} 项要求</span></div><div class="rw-category-list">${CATEGORIES.filter(category=>category!=='其他要求'||metrics.rows.some(row=>row.category===category)).map(category=>{const rows=metrics.rows.filter(row=>row.category===category);return `<div class="rw-category-row"><div><strong>${esc(category)}</strong><span>${category==='文档存在'?'核对文件名称与存在性':category==='数值指标'?'比较参数、单位和条件':category==='功能条件'?'核对功能与实现描述':'按原始条件逐项核对'}</span></div><b>${rows.length}<small>项</small></b></div>`;}).join('')}</div></section>`;
  return assessment+attention+`<div class="rw-overview-secondary">${verdicts}${categories}</div>`;
}

export function reportDetailFields(result, isDocument){
  const analysis=text(result.analysis).trim()||'历史记录未保存判断依据。';
  const fields=[
    {key:'analysis',label:'判断依据',value:analysis},
    {key:'materials',label:'材料要点',value:isDocument?'':text(result.project_plan).trim()},
    {key:'gaps',label:'缺口与冲突',value:text(result.gaps).trim()},
    {key:'action',label:'后续行动',value:text(result.recommendation).trim()}
  ];
  const seen=new Set(['', '无', '无。', '无（仅文件存在性）', '无需进行文档内容核查', '模型未提供项目方案或实现情况说明', '模型未提供缺口说明', '模型未提供补证建议']);
  return fields.filter(field=>{
    if(field.key!=='analysis'&&seen.has(field.value))return false;
    seen.add(field.value);
    return true;
  });
}

function resultSections(requirement,result){
  return reportDetailFields(result,typeOf(requirement,result)==='document').map(field=>{
    let content=`<p>${esc(field.value)}</p>`;
    if(field.key==='analysis'&&field.value.length>180){
      const boundary=field.value.slice(0,140).lastIndexOf('。');
      const end=boundary>=45?boundary+1:140;
      content=`<p>${esc(field.value.slice(0,end))}…</p><details class="rw-analysis-full" data-rw-detail="judgment"><summary>查看完整判断依据</summary><p>${esc(field.value)}</p></details>`;
    }
    return `<section class="rw-detail-box rw-detail-${field.key}"><h4>${esc(field.label)}</h4>${content}</section>`;
  }).join('');
}

function itemDetails(row){
  const r=row.requirement,a=row.result,question=[r.title,r.criterion].map(text).filter(Boolean).join(' ');
  const origin=sourceFor({source:r.source,quote:r.quote}),entries=evidenceDisplayEntries(row.evidence),occurrences=new Map();
  const evidence=entries.map(({item,index,aliases})=>{
    const source=sourceFor(item);
    const drawing=drawingEvidenceOf(source);
    const base=['evidence',text(item.source_id),text(source.id),text(source.dataset_id||source.kb_id),text(source.document_id||source.doc_id||source.document_name),contentKey(JSON.stringify([source.content,source.page,source.page_num_int,source.positions||source.position_int,drawing?.id,drawing?.version,item.kind,item.analysis]))].join(':');
    const occurrence=occurrences.get(base)||0;occurrences.set(base,occurrence+1);const key=base+':'+occurrence;
    const aliasText=aliases.length>1?'；原编号：'+aliases.map(source_id=>evidenceReference(row.id,{source_id},index)).join('、'):'';
    return `<section class="rw-evidence-block" data-rw-evidence-key="${esc(key)}"><div class="rw-evidence-heading"><strong class="rw-evidence-reference">引用 ${esc(evidenceReference(row.id,item,index))}${esc(aliasText)}</strong></div>${sourceBlock(source,question,key)}</section>`;
  }).join('');
  return `<details class="rw-item" data-rw-item="${esc(row.id)}" data-rw-key="${esc(row.key)}" data-rw-index="${row.index}"><summary><span class="rw-item-chevron" aria-hidden="true"></span><span class="rw-row-id">${esc(row.id)}</span><span class="rw-item-name"><strong>${esc(r.title||'未命名要求')}</strong><small>${esc(row.category)}${r.userAdded?' · 人工补充':''}${a?.humanEdited?' · 判定经人工修订':''}</small></span>${chip(row.status)}<span class="rw-evidence-count">${entries.length} 处引用</span></summary><div class="rw-item-body"><section class="rw-requirement-box"><div class="rw-section-kicker">核查要求</div><p>${esc(r.criterion||'历史记录未保存具体条件，请人工核对原文。')}</p>${r.quote?`<details class="rw-origin" data-rw-detail="origin"><summary>查看依据原文与来源</summary>${sourceBlock(origin,question,'origin')}</details>`:''}</section>${a?`<div class="rw-detail-grid">${resultSections(r,a)}</div><section class="rw-evidence-section"><div class="rw-section-heading"><h4>引用原文</h4><span class="rw-small-label">${entries.length} 处引用</span></div>${evidence||'<p class="rw-muted">本条未记录可展开的直接引文。文档存在型请核对文件清单；其他类型需要补充或复核来源。</p>'}</section>`:'<div class="rw-item-pending"><strong>本条尚未形成有效核查结果</strong><p>请完成指标确认并继续审查；历史记录中缺失或无法识别的判定也会保守显示为未核查。</p></div>'}<details class="rw-question" data-rw-detail="question"><summary>查看本条核查问句</summary><p>${esc(questionForView(r))}</p></details></div></details>`;
}

function items(metrics){
  return `<section class="rw-panel rw-items-panel"><div class="rw-section-heading"><div><div class="rw-section-kicker">逐项核对 · 点击条目展开</div><h3>核查清单</h3></div><span data-rw-filter-count class="rw-small-label" role="status" aria-live="polite">显示 ${metrics.total} / ${metrics.total} 项</span></div><div class="rw-filterbar"><label class="rw-search"><span class="rw-sr-only">搜索指标名称、编号或条件</span><svg aria-hidden="true" viewBox="0 0 20 20"><circle cx="8.5" cy="8.5" r="5.5"/><path d="m13 13 4 4"/></svg><input data-rw-search type="search" placeholder="名称 / 编号 / 条件" aria-label="搜索指标名称、编号或条件" autocomplete="off"></label><label><span class="rw-sr-only">筛选判定状态</span><select data-rw-status aria-label="筛选判定状态"><option value="all">全部判定</option><option value="attention">需要关注</option>${STATUSES.map(status=>`<option>${esc(status)}</option>`).join('')}</select></label><label><span class="rw-sr-only">筛选核查类型</span><select data-rw-category aria-label="筛选核查类型"><option value="all">全部类型</option>${CATEGORIES.map(category=>`<option>${esc(category)}</option>`).join('')}</select></label></div><div class="rw-list-toolbar"><p>原始判定与证据均保留，人工补充和修订单独标注。</p><div><button type="button" class="rw-text-button" data-rw-expand="open">展开所示条目</button><button type="button" class="rw-text-button" data-rw-expand="close">收起条目</button></div></div><div class="rw-item-list">${metrics.rows.map(itemDetails).join('')}</div><div class="rw-filter-empty" data-rw-filter-empty${metrics.total?' hidden':''}><strong>${metrics.total?'没有匹配的核查条目':'尚未建立核查清单'}</strong><p>${metrics.total?'可以调整判定、类型或关键词。':'请先提取并确认审查指标，生成的要求会在这里逐项列出。'}</p>${metrics.total?'<button type="button" class="rw-button" data-rw-reset>重置筛选</button>':''}</div></section>`;
}

function scope(report,metrics){
  const baseline=array(report.baselineDocs).filter(object),targets=array(report.targetDocs).filter(object);
  const issueCount=array(report.issues).length+array(report.skippedFiles).length;
  const raw={requirements:array(report.requirements),results:array(report.results)};
  const methods=report.scope==='dataset'?`所选知识库：${text(report.kbName||report.kb||'未记录')}。审查依据文件不作为自身的完成证明。`:targets.length?targets.map(file=>text(file.name||file.id)).join('、'):'历史记录未保存限定的证明材料清单。';
  return `<div class="rw-scope-grid"><section class="rw-panel"><div class="rw-section-kicker">审查边界</div><h3>范围与方法</h3><dl class="rw-facts"><div><dt>审查依据</dt><dd>${baseline.length?baseline.map(file=>esc(file.name||file.id||'未命名文件')).join('<br>'):'未记录依据文件名称'}</dd></div><div><dt>证明材料范围</dt><dd>${esc(methods)}</dd></div><div><dt>审查对话模型</dt><dd>${esc(report.modelLabel||'历史记录未保存模型名称')}</dd></div>${report.extractionVisionLabel?`<div><dt>指标提取视觉模型</dt><dd>${esc(report.extractionVisionLabel)}</dd></div>`:''}<div><dt>报告时间</dt><dd>${esc(dateLabel(report.finishedAt||report.createdAt))}</dd></div>${report.editedAt?`<div><dt>最近人工修订</dt><dd>${esc(dateLabel(report.editedAt))}</dd></div>`:''}</dl><div class="rw-scope-method"><h4>如何阅读本轮结果</h4><p>文档存在型核对文件名称；数值型比较方案参数及适用条件；功能型核对实现描述。方案符合不等于实测或验收达标。</p><p>${['three-stage','retrieval'].includes(report.mode)?'按每条要求检索已解析资料，未对全部证明材料逐字穷尽；未召回不能判为未完成。':'按本次报告保存的范围和原始结果核对，处理边界以资料读取记录、证据与异常说明为准。'}</p><p>图表按纳入要求统计，重复编号和未对应到要求的孤立结果不重复计数；无法识别的判定保守列为未核查。</p></div></section><section class="rw-panel rw-issue-panel"><div class="rw-section-heading"><div><div class="rw-section-kicker">完整性检查</div><h3>跳过文件与处理异常</h3></div><span class="rw-count">${issueCount}</span></div>${issueCount||report.limited?`<div class="rw-notice rw-notice-warning">本轮存在处理范围或结果边界，请结合下方记录复核。</div>`:'<div class="rw-notice">本轮记录未报告跳过文件或处理异常，仍需核对解析与引用质量。</div>'}<pre class="rw-issue-text">${esc(scopeIssues(report))}</pre>${report.interruption?`<h4>中断说明</h4><p>${esc(report.interruption)}</p>`:''}${array(report.textExtraction).length?`<details class="rw-record-disclosure" data-rw-extra="extraction"><summary>文字提取记录</summary><ul class="rw-extraction-list">${array(report.textExtraction).filter(object).map(item=>`<li><strong>${esc(item.name||'文件')}</strong><span>${esc(item.method||'提取方式未记录')} · ${esc(item.characters??'—')} 字符</span></li>`).join('')}</ul></details>`:''}</section></div><section class="rw-panel rw-record-panel"><div class="rw-section-heading"><div><div class="rw-section-kicker">复核留档</div><h3>指标问句与原始汇报</h3></div><span class="rw-small-label">${metrics.total} 项</span></div><p class="rw-muted">保留原始条件、问句和判定，供逐项核对；页面上方可导出原始数据与完整 HTML 报告。</p><details class="rw-record-disclosure" data-rw-extra="questions"><summary>查看全部指标问句</summary><ol class="rw-question-list">${metrics.rows.map(row=>`<li><span class="rw-row-id">${esc(row.id)}</span><div><strong>${esc(row.requirement.title||'未命名要求')}</strong><p>${esc(questionForView(row.requirement))}</p></div></li>`).join('')}</ol></details><details class="rw-record-disclosure" data-rw-extra="raw"><summary>展开原始问答数据</summary><pre class="rw-raw">${esc(jsonText(raw))}</pre></details>${array(report.rawReport).length?`<details class="rw-record-disclosure" data-rw-extra="snapshot"><summary>查看保存时的原始汇报快照</summary><pre class="rw-raw">${esc(jsonText(report.rawReport))}</pre></details>`:''}</section>`;
}

export function renderReportWorkspace(value){
  const report=object(value)?value:{},metrics=reportWorkspaceMetrics(report),phase=reportPhase(report,metrics);
  const key=text(report.dbId||report.id||[report.createdAt,report.kb,report.title].join('|'));
  const issueCount=array(report.issues).length+array(report.skippedFiles).length;
  return `<div class="rw-workspace rw-macos" data-report-workspace data-rw-report-key="${esc(key)}"><div class="rw-mac-titlebar"><div class="rw-mac-window-dots" aria-hidden="true"><i></i><i></i><i></i></div><span class="rw-mac-window-caption">审查报告</span><span class="rw-mac-window-mode">报告视图</span></div><header class="rw-header"><div><div class="rw-eyebrow"><span aria-hidden="true"></span>技术符合性审查 · 结果报告</div><h2>${esc(report.title||'合同与技术协议审查')}</h2><p>${esc(report.kbName||'本次审查工作空间')}<span aria-hidden="true">·</span>${esc(dateLabel(report.finishedAt||report.createdAt))}</p></div><span class="rw-phase rw-tone-${phase.tone}"><i aria-hidden="true"></i>${esc(phase.name)}</span></header><div class="rw-metrics">${[['纳入要求',metrics.total,'当前核查清单'],['已核查',metrics.done,metrics.pending?'尚有 '+metrics.pending+' 项待核查':'按有效结果统计'],['需要关注',metrics.attention,'处理与人工复核事项'],['证据文件',metrics.sourceFiles,'按来源文件去重']].map(([label,number,note],index)=>`<div class="rw-metric${index===2&&number?' rw-metric-attention':''}"><span>${label}</span><strong>${number}<small>${index===3?'份':'项'}</small></strong><p>${esc(note)}</p></div>`).join('')}</div>${issueCount||report.limited||report.interruption?`<div class="rw-integrity-banner"><span aria-hidden="true">!</span><p>${esc(report.interruption?'本轮有中断或未完成说明，请检查处理记录。':`本轮${issueCount?'记录 '+issueCount+' 项跳过文件或处理异常':'标记了处理范围限制'}，请复核结果边界。`)}</p><button type="button" data-rw-show-scope>查看记录 ↗</button></div>`:''}<nav class="rw-tabs" role="tablist" aria-label="审查报告视图"><button type="button" role="tab" id="rw-tab-overview" aria-controls="rw-panel-overview" aria-selected="true" data-rw-tab="overview">报告总览</button><button type="button" role="tab" id="rw-tab-items" aria-controls="rw-panel-items" aria-selected="false" tabindex="-1" data-rw-tab="items">逐项核查 <span>${metrics.total}</span></button><button type="button" role="tab" id="rw-tab-scope" aria-controls="rw-panel-scope" aria-selected="false" tabindex="-1" data-rw-tab="scope">范围与记录${issueCount?` <span>${issueCount}</span>`:''}</button></nav><div class="rw-panels"><section id="rw-panel-overview" role="tabpanel" aria-labelledby="rw-tab-overview" data-rw-panel="overview">${overview(report,metrics,phase)}</section><section id="rw-panel-items" role="tabpanel" aria-labelledby="rw-tab-items" data-rw-panel="items" hidden>${items(metrics)}</section><section id="rw-panel-scope" role="tabpanel" aria-labelledby="rw-tab-scope" data-rw-panel="scope" hidden>${scope(report,metrics)}</section></div><footer class="rw-footer"><span>结果用于辅助人工复核</span><p>已满足占比与核查进度均不等同于项目验收通过率。具体结论应结合原文、适用条件和证据性质确认。</p></footer></div>`;
}

export function bindReportWorkspace(container,report){
  const root=container.querySelector('[data-report-workspace]');if(!root)return;
  container._rwBinding?.abort();const binding=new AbortController();container._rwBinding=binding;
  const key=root.dataset.rwReportKey;
  let state=container._rwViewState;
  if(!state||state.key!==key)state={key,tab:'overview',status:'all',category:'all',query:'',open:new Set(),searchFocused:false,selection:null};
  container._rwViewState=state;
  const metrics=reportWorkspaceMetrics(report),query=root.querySelector('[data-rw-search]'),status=root.querySelector('[data-rw-status]'),category=root.querySelector('[data-rw-category]');
  const tabs=[...root.querySelectorAll('[data-rw-tab]')];
  query.value=state.query;status.value=state.status;category.value=state.category;
  if(!status.value)state.status=status.value='all';if(!category.value)state.category=category.value='all';
  const cards=[...root.querySelectorAll('details[data-rw-item]')];
  root.querySelectorAll('details').forEach(detail=>{const item=detail.closest('details[data-rw-item]');const semantic=detail.dataset.rwDetail||detail.id||detail.className||detail.querySelector('summary')?.textContent||'detail';detail.dataset.rwDetailKey=detail===item?'item:'+item.dataset.rwKey:detail.dataset.rwExtra?'extra:'+detail.dataset.rwExtra:'nested:'+(item?.dataset.rwKey||'scope')+':'+semantic;if(state.open.has(detail.dataset.rwDetailKey))detail.open=true;});
  function activate(tab){
    state.tab=['overview','items','scope'].includes(tab)?tab:'overview';
    for(const button of tabs){const selected=button.dataset.rwTab===state.tab;button.setAttribute('aria-selected',String(selected));button.tabIndex=selected?0:-1;}
    root.querySelectorAll('[data-rw-panel]').forEach(panel=>panel.hidden=panel.dataset.rwPanel!==state.tab);
  }
  function applyFilters(){
    const wanted=state.query.trim().toLocaleLowerCase();let visible=0;
    for(const card of cards){const row=metrics.rows[Number(card.dataset.rwIndex)];if(!row)continue;const searchable=[row.id,row.requirement.title,row.requirement.criterion].map(text).join(' ').toLocaleLowerCase();const show=(!wanted||searchable.includes(wanted))&&(state.category==='all'||row.category===state.category)&&(state.status==='all'||(state.status==='attention'?ATTENTION.has(row.status):row.status===state.status));card.hidden=!show;if(show)visible++;}
    const label=`显示 ${visible} / ${metrics.total} 项`,counter=root.querySelector('[data-rw-filter-count]');if(counter.textContent!==label)counter.textContent=label;
    root.querySelector('[data-rw-filter-empty]').hidden=visible>0;
  }
  function reset(){state.status='all';state.category='all';state.query='';status.value='all';category.value='all';query.value='';applyFilters();}
  function showItems(filter='all'){state.status=filter;status.value=filter;activate('items');applyFilters();}
  function openItem(id){
    reset();activate('items');const item=cards.find(card=>card.dataset.rwItem===id);if(!item)return;
    item.open=true;state.open.add(item.dataset.rwDetailKey);
    requestAnimationFrame(()=>{item.scrollIntoView({block:'start',behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth'});item.querySelector('summary').focus({preventScroll:true});});
  }
  root.addEventListener('click',event=>{
    const button=event.target.closest('button');if(!button||!root.contains(button))return;
    if(button.dataset.rwTab){activate(button.dataset.rwTab);return;}
    if(button.hasAttribute('data-rw-show-scope')){activate('scope');return;}
    if(button.hasAttribute('data-rw-show-items')){showItems();return;}
    if(button.hasAttribute('data-rw-attention')){showItems('attention');return;}
    if(button.hasAttribute('data-rw-filter-status')){showItems(button.dataset.rwFilterStatus);return;}
    if(button.hasAttribute('data-rw-open-item')){openItem(button.dataset.rwOpenItem);return;}
    if(button.hasAttribute('data-rw-reset')){reset();return;}
    if(button.hasAttribute('data-rw-expand'))for(const card of cards){if(card.hidden)continue;card.open=button.dataset.rwExpand==='open';if(card.open)state.open.add(card.dataset.rwDetailKey);else state.open.delete(card.dataset.rwDetailKey);}
  },{signal:binding.signal});
  root.addEventListener('input',event=>{if(event.target===query){state.query=query.value;state.selection=[query.selectionStart,query.selectionEnd];applyFilters();}},{signal:binding.signal});
  root.addEventListener('change',event=>{if(event.target===status){state.status=status.value;applyFilters();}if(event.target===category){state.category=category.value;applyFilters();}},{signal:binding.signal});
  root.addEventListener('toggle',event=>{const detail=event.target;if(!detail.matches?.('details')||!detail.dataset.rwDetailKey)return;if(detail.open)state.open.add(detail.dataset.rwDetailKey);else state.open.delete(detail.dataset.rwDetailKey);},{capture:true,signal:binding.signal});
  root.addEventListener('keydown',event=>{const index=tabs.indexOf(event.target);if(index<0)return;let next;if(event.key==='ArrowRight')next=(index+1)%tabs.length;else if(event.key==='ArrowLeft')next=(index+tabs.length-1)%tabs.length;else if(event.key==='Home')next=0;else if(event.key==='End')next=tabs.length-1;else return;event.preventDefault();activate(tabs[next].dataset.rwTab);tabs[next].focus();},{signal:binding.signal});
  document.addEventListener('focusin',event=>{state.searchFocused=event.target===query;},{signal:binding.signal});
  activate(state.tab);applyFilters();
  if(state.searchFocused)requestAnimationFrame(()=>{if(binding.signal.aborted)return;query.focus({preventScroll:true});if(state.selection)try{query.setSelectionRange(...state.selection);}catch{}});
}
