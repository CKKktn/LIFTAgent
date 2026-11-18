import {esc,evidenceKindInfo,evidenceReference,evidenceDisplayEntries} from './ui.js';
import {reviewQuestion} from './review.js';
import {reportDetailFields,reportWorkspaceMetrics} from './report-workspace.js';

const VERDICTS=['已满足','部分满足','不满足','证据不足','待人工复核','证据冲突'];
const STATUSES=[...VERDICTS,'未核查'];
const CATEGORIES=['文档存在','数值指标','功能条件','其他要求'];
const COLORS={'已满足':'#2c8768','部分满足':'#b58a35','不满足':'#b8535a','证据不足':'#5681b1','待人工复核':'#8670ae','证据冲突':'#b47a54','未核查':'#a0aabb'};
const TONES={'已满足':'ok','部分满足':'warn','不满足':'bad','证据不足':'info','待人工复核':'review','证据冲突':'conflict','未核查':'pending'};
const list=value=>Array.isArray(value)?value:[];
const object=value=>value&&typeof value==='object'&&!Array.isArray(value);
const text=value=>String(value??'');
const numeric=value=>Number.isFinite(Number(value))?Number(value):0;
const percentage=(part,total)=>total?Math.round(part/total*1000)/10:0;
const percent=(part,total)=>percentage(part,total).toLocaleString('zh-CN',{maximumFractionDigits:1})+'%';
const nonempty=(value,fallback='未记录')=>text(value).trim()||fallback;
const truncate=(value,length)=>text(value).length>length?text(value).slice(0,length)+'…':text(value);
const badge=status=>`<span class="badge ${TONES[status]||'pending'}">${esc(status)}</span>`;
const paragraph=(value,fallback='未记录；请结合原文人工核对。')=>`<p class="prose">${esc(nonempty(value,fallback))}</p>`;

function checkKind(requirement,result){
  // Substantive requirements must never inherit an existence-only fallback
  // from a stale result. An inconsistent category is explicitly disclosed.
  const substantive=['numeric','functional'].includes(requirement.type)||['数值指标','功能条件'].includes(requirement.category);
  const document=requirement.type==='document'||requirement.category==='文档存在';
  const isDocument=!substantive&&(document||result?.checkType==='document');
  const mismatch=(substantive&&document)||(substantive&&result?.checkType==='document')||(isDocument&&['numeric','functional'].includes(result?.checkType));
  return {isDocument,mismatch};
}

function analysisFields(result,kind){
  return `<div class="analysis-grid">${reportDetailFields(result,kind.isDocument).map(field=>`<section class="analysis-${field.key}"><h4>${esc(field.label)}</h4>${paragraph(field.value)}</section>`).join('')}</div>`;
}

function dateLabel(value){
  if(!value)return '未记录';
  const date=new Date(value);
  return Number.isNaN(date.getTime())?text(value):new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(date)+'（北京时间）';
}

export function reportHtmlFilename(value){
  const report=object(value)?value:{};
  const title=nonempty(report.title,'未命名项目').normalize('NFKC').replace(/[<>:"/\\|?*\u0000-\u001f\u007f\u202a-\u202e\u2066-\u2069]/g,'_').replace(/[. ]+$/g,'').slice(0,90)||'未命名项目';
  const date=new Date(report.finishedAt||report.createdAt||'');
  const stamp=Number.isNaN(date.getTime())?'未注明日期':new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(date).replace(/-/g,'');
  return `审查报告-${title}-${stamp}.html`;
}

function phaseOf(report,metrics){
  if(!metrics.total)return {label:'待提取指标',tone:'pending',heading:'尚未形成可核查的要求清单',body:'本报告为当前记录的阶段快照。请先提取并确认指标，再开展逐项核查。'};
  if(report.awaitingConfirmation||(report.mode==='three-stage'&&!report.confirmed&&!metrics.done))return {label:'待人工确认',tone:'info',heading:'指标已提取，尚待人工确认',body:'当前条目还未完成范围确认，不能据此形成整体审查结论。请先核对指标、条件和依据原文。'};
  if(!report.complete)return {label:report.interruption?'审查已中断 · 阶段结果':'审查进行中 · 阶段结果',tone:'info',heading:`当前已核查 ${metrics.done} / ${metrics.total} 项要求`,body:'已保存的结果和证据随本文件完整保留，尚未处理的条目按未核查列示。整体判断需在本轮处理结束并经人工复核后形成。'};
  if(metrics.pending)return {label:'处理结束 · 仍有未核查项',tone:'warn',heading:`仍有 ${metrics.pending} 项要求未形成有效判定`,body:'不能将未核查或无法识别的历史状态视作已满足。请查看异常记录，继续核查或完成相应人工复核。'};
  if(report.limited||list(report.issues).length||list(report.skippedFiles).length)return {label:'处理结束 · 覆盖受限',tone:'warn',heading:'本轮存在材料或处理覆盖缺口',body:'逐项判定保留原始结果；跳过文件、处理异常和范围限制需要单独复核，不能据此确认整体完成。'};
  if(metrics.counts['不满足'])return {label:'核查已完成 · 有整改事项',tone:'bad',heading:`发现 ${metrics.counts['不满足']} 项不满足要求`,body:'应先核对不满足项的事实与适用条件，再处理部分满足、证据缺口、冲突和待人工复核事项。'};
  if(metrics.attention)return {label:'核查已完成 · 需补证或复核',tone:'warn',heading:`有 ${metrics.attention} 项需要进一步处理`,body:'需关注事项包含不同性质的判定。证据不足不等于实际未完成，应按每条原始结论安排补证或复核。'};
  return {label:'核查已完成 · 待最终人工复核',tone:'ok',heading:'已核查要求均记录为已满足',body:'该结果是已有材料对应的技术符合性判断。请继续核实原文、适用条件及证据性质，不将其直接等同于项目验收结论。'};
}

function pagesOf(source){
  const pages=[...list(source.page_num_int),...list(source.positions||source.position_int).map(position=>Array.isArray(position)?position[0]:null)];
  if(source.page!==undefined)pages.push(source.page);
  const valid=[...new Set(pages.map(Number).filter(page=>Number.isInteger(page)&&page>0))].sort((a,b)=>a-b);
  return valid.length?'第 '+valid.join('、')+' 页':source.ordinal!==undefined&&text(source.ordinal).trim()?'切片 '+text(source.ordinal)+'（页码未记录）':'页码与切片位置未记录';
}

function evidenceSource(evidence){
  const source=object(evidence?.source)?evidence.source:{};
  return {document_id:text(source.document_id||source.doc_id),dataset_id:text(source.dataset_id||source.kb_id),name:nonempty(source.document_name||source.doc_name||source.name,'文档名称未记录'),location:pagesOf(source),quote:text(evidence?.quote??source.content??source.content_with_weight??''),source};
}

function meaningfulEvidence(evidence){
  const source=evidenceSource(evidence);
  return !!(source.document_id||source.quote.trim()||source.name!=='文档名称未记录');
}

function sourceRegistry(metrics){
  const records=[],byKey=new Map();
  const add=(value,row,role,refId,entry=null)=>{
    const source=evidenceSource(value);
    if(!source.document_id&&!source.quote.trim()&&source.name==='文档名称未记录')return null;
    const key=source.document_id?'id:'+source.dataset_id+':'+source.document_id:'name:'+source.dataset_id+':'+source.name;
    let record=byKey.get(key);
    if(!record){record={...source,anchor:'source-'+(records.length+1),roles:new Set(),rows:new Map(),evidenceRows:new Set(),references:[]};records.push(record);byKey.set(key,record);}
    record.roles.add(role);record.rows.set(row.key,row);
    if(role==='结果证据')record.evidenceRows.add(row.key);
    record.references.push({row,role,refId,quote:source.quote,location:source.location,sourceId:text(value?.source_id),kind:text(value?.kind),analysis:text(value?.analysis),
      referenceLabel:entry?evidenceReference(row.id,value,entry.index):row.id+' · 依据原文',aliases:entry?.aliases||[],isDocument:checkKind(row.requirement,row.result).isDocument});
    return record;
  };
  const rowSources=new Map();
  for(const row of metrics.rows){
    const origin=add({source:row.requirement.source,quote:row.requirement.quote},row,'审查依据','origin-'+(row.index+1));
    const entries=evidenceDisplayEntries(row.evidence);
    const evidence=entries.map(entry=>add(entry.item,row,'结果证据','evidence-'+(row.index+1)+'-'+(entry.index+1),entry));
    rowSources.set(row.key,{origin,evidence,entries});
  }
  return {records,rowSources};
}

function svgStart(name,width,height){return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(name)}"><title>${esc(name)}</title>`;}

function statusChart(metrics){
  const radius=75,circumference=2*Math.PI*radius;let offset=0;
  const circles=STATUSES.map(status=>{const portion=metrics.total?metrics.counts[status]/metrics.total:0,length=portion*circumference;const shape=length?`<circle cx="110" cy="110" r="75" fill="none" stroke="${COLORS[status]}" stroke-width="25" stroke-dasharray="${length.toFixed(4)} ${(circumference-length).toFixed(4)}" stroke-dashoffset="${(-offset).toFixed(4)}" transform="rotate(-90 110 110)"/>`:'';offset+=length;return shape;}).join('');
  const svg=svgStart('六类判定及未核查分布',220,220)+`<circle cx="110" cy="110" r="75" fill="none" stroke="#edf1f7" stroke-width="25"/>${circles}<text x="110" y="106" text-anchor="middle" class="svg-number">${metrics.total}</text><text x="110" y="133" text-anchor="middle" class="svg-label">纳入要求 / 项</text></svg>`;
  return `<figure class="chart" data-report-chart="verdict-distribution"><figcaption><span>图 1</span> 判定分布</figcaption><div class="donut-layout"><div class="donut">${svg}</div><table class="legend"><thead><tr><th>判定</th><th class="num">项数</th><th class="num">占全部要求</th></tr></thead><tbody>${STATUSES.map(status=>`<tr><td><i class="color-dot" style="background:${COLORS[status]}"></i>${esc(status)}</td><td class="num">${metrics.counts[status]}</td><td class="num">${percent(metrics.counts[status],metrics.total)}</td></tr>`).join('')}</tbody></table></div><p class="caption">分母为 ${metrics.total} 项纳入要求；占比仅说明判定构成，不代表项目验收通过率。</p></figure>`;
}

function categoryChart(metrics){
  const groups=CATEGORIES.map(category=>({category,rows:metrics.rows.filter(row=>row.category===category)})).filter(group=>group.rows.length);
  const max=Math.max(1,...groups.map(group=>group.rows.length)),height=Math.max(128,groups.length*54+40);
  const bars=groups.map((group,index)=>{
    const y=index*54+26;let x=138;
    const segments=STATUSES.map(status=>{const count=group.rows.filter(row=>row.status===status).length,width=count/max*470;const shape=width?`<rect x="${x.toFixed(3)}" y="${y-16}" width="${width.toFixed(3)}" height="24" rx="2" fill="${COLORS[status]}"><title>${esc(group.category+'：'+status+' '+count+'项')}</title></rect>`:'';x+=width;return shape;}).join('');
    return `<text x="0" y="${y+1}" class="svg-label">${esc(group.category)}</text><rect x="138" y="${y-16}" width="470" height="24" rx="2" fill="#edf1f7"/>${segments}<text x="625" y="${y+1}" class="svg-label">${group.rows.length} 项</text>`;
  }).join('');
  const svg=svgStart('各类要求的判定构成',690,height)+(groups.length?bars:'<text x="12" y="55" class="svg-label">尚无已纳入的要求分类</text>')+'</svg>';
  const matrix=`<div class="table-wrap"><table class="matrix"><thead><tr><th>类型</th>${STATUSES.map(status=>`<th class="num">${status}</th>`).join('')}<th class="num">合计</th></tr></thead><tbody>${groups.map(group=>`<tr><th>${group.category}</th>${STATUSES.map(status=>`<td class="num">${group.rows.filter(row=>row.status===status).length}</td>`).join('')}<td class="num">${group.rows.length}</td></tr>`).join('')||'<tr><td colspan="9">尚未形成要求分类。</td></tr>'}<tr class="total"><th>全部要求</th>${STATUSES.map(status=>`<td class="num">${metrics.counts[status]}</td>`).join('')}<td class="num">${metrics.total}</td></tr></tbody></table></div>`;
  return `<figure class="chart" data-report-chart="category-verdicts"><figcaption><span>图 2 / 表 1</span> 要求类型与判定矩阵</figcaption>${svg}${matrix}<p class="caption">条形长度按项数绘制，同一颜色在各图中表示相同判定；文档存在型仅核对文件名称与存在性。</p></figure>`;
}

function coverageData(metrics){
  const withEvidence=metrics.rows.filter(row=>row.result&&row.evidence.some(meaningfulEvidence)).length;
  return {withEvidence,withoutEvidence:metrics.done-withEvidence,excerpts:metrics.rows.reduce((sum,row)=>sum+row.evidence.filter(meaningfulEvidence).length,0)};
}

function coverageChart(metrics,coverage){
  const width=430;
  const svg=svgStart('已核查条目的保存证据情况',680,136)+[
    ['已保存引用',coverage.withEvidence,'#5681b1'],['未保存引用',coverage.withoutEvidence,'#c0cad7']
  ].map(([label,count,color],index)=>{const y=24+index*54;return `<text x="0" y="${y+13}" class="svg-label">${label}</text><rect x="112" y="${y-2}" width="${width}" height="23" rx="3" fill="#edf1f7"/><rect x="112" y="${y-2}" width="${(metrics.done?count/metrics.done*width:0).toFixed(3)}" height="23" rx="3" fill="${color}"/><text x="558" y="${y+13}" class="svg-label">${count} 项 · ${percent(count,metrics.done)}</text>`;}).join('')+'</svg>';
  return `<figure class="chart" data-report-chart="evidence-coverage"><figcaption><span>图 3</span> 已核查条目的引用记录</figcaption>${svg}<div class="fact-row"><span>已核查要求 <b>${metrics.done} 项</b></span><span>保存引用的要求 <b>${coverage.withEvidence} 项</b></span><span>保存证据条目 <b>${coverage.excerpts} 条</b></span></div><p class="caption">分母为已形成六类有效判定的要求。这里统计“是否保存引用”，不是全文检索覆盖率，也不保证引用足以证明完成；文档名称匹配记录同样可能计入引用；条数沿用保存记录口径，即使相同引用在正文合并展示，也不会改变此处计数。</p></figure>`;
}

function frequencyChart(registry){
  const entries=registry.records.filter(record=>record.evidenceRows.size).sort((a,b)=>b.evidenceRows.size-a.evidenceRows.size);
  const shown=entries.slice(0,8),max=Math.max(1,...shown.map(record=>record.evidenceRows.size)),height=Math.max(120,shown.length*48+24);
  const svg=svgStart('证据来源涉及的要求数量',760,height)+(shown.map((record,index)=>{const y=24+index*48;return `<text x="0" y="${y+9}" class="svg-label"><title>${esc(record.name)}</title>${esc(truncate(record.name,22))}</text><rect x="294" y="${y-8}" width="350" height="23" rx="3" fill="#edf1f7"/><rect x="294" y="${y-8}" width="${(record.evidenceRows.size/max*350).toFixed(3)}" height="23" rx="3" fill="#6c8bad"/><text x="660" y="${y+9}" class="svg-label">${record.evidenceRows.size} 项</text>`;}).join('')||'<text x="12" y="52" class="svg-label">当前尚无保存的证据文件引用。</text>')+'</svg>';
  return `<figure class="chart" data-report-chart="source-frequency"><figcaption><span>图 4</span> 证据来源与涉及要求</figcaption>${svg}<div class="table-wrap"><table><thead><tr><th>证据来源</th><th>文档标识</th><th class="num">涉及要求数</th><th>要求编号</th></tr></thead><tbody>${entries.map(record=>`<tr><td><a href="#${record.anchor}">${esc(record.name)}</a></td><td class="mono">${esc(record.document_id||'未记录，以文件名归组')}</td><td class="num">${record.evidenceRows.size}</td><td>${[...record.evidenceRows].map(key=>record.rows.get(key)).map(row=>`<a href="#requirement-${row.index+1}">${esc(row.id)}</a>`).join('、')}</td></tr>`).join('')||'<tr><td colspan="4">未记录证据来源。</td></tr>'}</tbody></table></div><p class="caption">图中最多展示前 8 个来源，下表列出全部来源。同一文件在同一要求下被多次引用，只计 1 项；未保存文档 ID 的历史记录按名称归组，不能据此确认它们是不同原件。</p></figure>`;
}

function attentionSection(metrics){
  const rank={'不满足':0,'证据冲突':1,'部分满足':2,'待人工复核':3,'证据不足':4};
  const rows=metrics.rows.filter(row=>row.result&&row.status!=='已满足').sort((a,b)=>rank[a.status]-rank[b.status]||a.index-b.index);
  return `<section id="actions" class="section"><div class="section-head"><span>03</span><h2>关注事项与处理清单</h2></div><p class="section-intro">共 ${metrics.attention} 项需处理或复核。以下顺序按判定性质组织，不额外生成未核实的风险等级；“证据不足”不等于“不满足”。</p><div class="table-wrap"><table class="actions-table"><thead><tr><th>要求</th><th>判定</th><th>主要发现 / 缺口</th><th>后续行动</th></tr></thead><tbody>${rows.map(row=>`<tr><td><a href="#requirement-${row.index+1}"><b>${esc(row.id)}</b> ${esc(row.requirement.title||'未命名要求')}</a><small>${esc(row.category)}</small></td><td>${badge(row.status)}</td><td class="prose">${esc(nonempty(row.result.gaps,row.result.analysis||'未记录，需人工复核。'))}</td><td class="prose">${esc(nonempty(row.result.recommendation,'未记录具体建议，请由项目负责人逐项复核。'))}</td></tr>`).join('')||`<tr><td colspan="4">${metrics.pending||!metrics.total?'尚未形成可归入关注清单的有效判定；未核查条目仍需继续处理。':'当前已核查项未记录需关注判定，仍需核对原文并完成人工复核。'}</td></tr>`}</tbody></table></div></section>`;
}

function requirementTable(metrics){
  return `<div class="table-wrap"><table><thead><tr><th>编号与要求</th><th>类型</th><th>判定</th><th class="num">保存证据</th></tr></thead><tbody>${metrics.rows.map(row=>`<tr><td><a href="#requirement-${row.index+1}"><b>${esc(row.id)}</b> ${esc(row.requirement.title||'未命名要求')}</a>${row.requirement.userAdded?'<small>人工补充</small>':''}</td><td>${esc(row.category)}</td><td>${badge(row.status)}</td><td class="num">${row.evidence.filter(meaningfulEvidence).length} 条</td></tr>`).join('')||'<tr><td colspan="4">尚无已纳入的要求。</td></tr>'}</tbody></table></div>`;
}

function sourceRef(record,source){
  return `${record?`<a href="#${record.anchor}">${esc(source.name)}</a>`:esc(source.name)}<span class="source-meta">${esc(source.location)}${source.document_id?' · 文档 ID '+esc(source.document_id):''}</span>`;
}

function detailSection(report,metrics,registry){
  return `<section id="details" class="section"><div class="section-head"><span>04</span><h2>全部要求与逐项分析</h2></div><p class="section-intro">以下 ${metrics.total} 项按当前纳入顺序列示。条件、原文和数值按记录保留；未记录字段明确标注。未核查、无法识别状态和缺失结果不计为已满足。</p>${requirementTable(metrics)}<div class="requirements">${metrics.rows.map(row=>{
    const requirement=row.requirement,result=row.result,kind=checkKind(requirement,result),links=registry.rowSources.get(row.key),origin=evidenceSource({source:requirement.source,quote:requirement.quote});
    const unknown=!result?list(report.results).filter(object).findLast(item=>text(item.requirementId).trim()===row.key):null;
    let question='';try{question=reviewQuestion(requirement);}catch{question=text(requirement.question||requirement.criterion);}
    const quotes=links.entries.map((entry,groupIndex)=>{
      const evidence=entry.item,source=evidenceSource(evidence),record=links.evidence[groupIndex];
      const aliases=entry.aliases.filter(alias=>alias!==text(evidence.source_id).trim());
      return `<section class="quote-card" id="evidence-${row.index+1}-${entry.index+1}"><div class="quote-header"><b>${esc(evidenceReference(row.id,evidence,entry.index))}${aliases.length?' · '+esc(aliases.join('、')):''}</b></div><p class="source-name">${sourceRef(record,source)}</p><blockquote>${esc(nonempty(source.quote,'未保存原文引文。'))}</blockquote></section>`;
    }).join('');
    const countLabel=links.entries.length===row.evidence.length?row.evidence.length+' 条记录':links.entries.length+' 组展示 / '+row.evidence.length+' 条保存记录';
    return `<article class="requirement" id="requirement-${row.index+1}"><header class="requirement-head"><div><span class="requirement-id">${esc(row.id)}</span><h3>${esc(requirement.title||'未命名要求')}</h3><div class="tags"><span>${esc(row.category)}</span>${requirement.userAdded?'<span>人工补充</span>':''}${requirement.userEdited?'<span>要求经人工修订</span>':''}${result?.humanEdited?'<span>判定经人工修订</span>':''}</div></div>${badge(row.status)}</header><section class="criterion"><h4>核查条件</h4>${paragraph(requirement.criterion,'未记录具体达标条件，请核对合同或技术协议原文。')}</section><section class="origin" id="origin-${row.index+1}"><h4>依据原文</h4><p class="source-name">${sourceRef(links.origin,origin)}</p><blockquote>${esc(nonempty(requirement.quote,'未保存依据引文；人工补充要求需自行核对合同依据。'))}</blockquote>${requirement.rationale?`<p class="caption">纳入理由：${esc(requirement.rationale)}</p>`:''}</section>${kind.mismatch?`<p class="notice warning">核查类型记录存在差异：要求类型与历史核查方式不一致，请复核。此处按要求条件展示；数值与功能要求不能仅以文档存在性判断。</p>`:''}${result?`${analysisFields(result,kind)}${row.evidence.length?`<section class="evidence"><h4>来源证据 · ${countLabel}</h4>${quotes}</section>`:'<p class="notice">本项未保存可展开的来源证据。文档存在型请核对文件清单，其他类型需补充或复核原始材料。</p>'}`:`<section class="notice"><h4>本项尚未形成可识别的有效判定</h4><p>${unknown?`原记录状态为“${esc(unknown.status)}”，本报告按未核查列示。`:'该要求的结果尚未保存。'}请完成后续核查或人工复核，不将缺失结果视作已满足。</p></section>`}<details class="question"><summary>核查问句</summary>${paragraph(question)}</details><a class="backlink" href="#details">返回要求清单 ↑</a></article>`;
  }).join('')}</div></section>`;
}

function fileList(documents,fallback){
  const files=list(documents).filter(object);
  return files.length?`<ul class="file-list">${files.map(file=>`<li><b>${esc(nonempty(file.name||file.document_name,'文件名称未记录'))}</b>${file.id||file.document_id?`<small>文档 ID：${esc(file.id||file.document_id)}</small>`:''}</li>`).join('')}</ul>`:`<p class="muted">${esc(fallback)}</p>`;
}

function methodText(report){
  const mode=report.mode;
  return mode==='three-stage'?'三步审查：提取并人工确认指标与技术交付要求；逐项结合知识库检索并判定；依据原始问答整理报告。此过程不代表对所有证明材料逐字穷尽检查。':mode==='retrieval'?'指标检索核查：在选定范围内按要求检索证据并综合判定。未召回内容不等于实际未完成。':mode?'按当前记录保存的要求、判定及证据编制；历史模式的具体处理方法需结合原任务记录核对。':'历史记录未保存审查模式，本报告仅呈现已有要求、判定及证据。';
}

function scopeSection(report,metrics){
  const issues=list(report.issues).filter(object),skipped=list(report.skippedFiles).filter(object);
  return `<section id="scope" class="section"><div class="section-head"><span>05</span><h2>审查范围、方法与处理记录</h2></div><div class="two-columns"><section class="box"><h3>要求依据</h3>${fileList(report.baselineDocs,'未记录依据文件清单。')}</section><section class="box"><h3>证据范围</h3>${report.scope==='dataset'?`<p>所选知识库：<b>${esc(nonempty(report.kbName||report.kb,'名称未记录'))}</b></p><p class="caption">按已解析、可检索内容核查；合同或技术协议不作为自身已完成的证明。</p>`:fileList(report.targetDocs,'未记录待审文件清单或整库范围。')}</section></div><div class="box method"><h3>方法与判定边界</h3><p>${esc(methodText(report))}</p><ul><li>文档存在型仅核对指定技术文件是否存在，不推断其正文质量或验收效力。</li><li>数值型保留参数、单位、比较方向、条件及例外；方案符合不等同实测达标。</li><li>功能型区分实现描述、设计机制与未来计划；未检索到证据不直接等于未实现。</li><li>图表依据当前纳入要求及已保存的有效结果计算；孤儿结果和未知状态不制造已满足数量。</li><li>本报告用于辅助人工复核，具体结论应结合原件、项目适用范围及后续验收材料。</li></ul></div><dl class="facts"><div><dt>审查模型记录</dt><dd>${esc(nonempty(report.modelLabel,'未记录模型名称；仅保存配置 ID 时不能推断实际服务商。'))}</dd></div><div><dt>范围标识</dt><dd>${esc(report.scope==='dataset'?'所选知识库':report.scope?'选定文件范围':'未记录')}</dd></div><div><dt>核查条目</dt><dd>${metrics.done} / ${metrics.total} 项已形成有效判定</dd></div><div><dt>当前处理边界</dt><dd>${esc(report.limited?'记录标注覆盖受限。':'未额外标注覆盖受限；仍需结合以下异常和材料质量复核。')}</dd></div></dl>${report.interruption?`<div class="notice warning"><b>中断记录</b>${paragraph(report.interruption)}</div>`:''}<h3 class="subheading">文字提取记录</h3><div class="table-wrap"><table><thead><tr><th>文件</th><th>提取方式</th><th class="num">已记录字符数</th></tr></thead><tbody>${list(report.textExtraction).filter(object).map(item=>`<tr><td>${esc(nonempty(item.name))}</td><td>${esc(nonempty(item.method))}</td><td class="num">${Number.isFinite(Number(item.characters))&&Number(item.characters)>=0?Number(item.characters):'未记录'}</td></tr>`).join('')||'<tr><td colspan="3">未保存文字提取记录。</td></tr>'}</tbody></table></div><h3 class="subheading">跳过文件 · ${skipped.length} 项</h3><div class="table-wrap"><table><thead><tr><th>角色 / 文件</th><th>原因</th></tr></thead><tbody>${skipped.map(item=>`<tr><td>${esc(item.role||'未记录角色')} · ${esc(item.name||item.id||'文件名称未记录')}</td><td class="prose">${esc(nonempty(item.reason))}</td></tr>`).join('')||'<tr><td colspan="2">记录中没有列出跳过文件。</td></tr>'}</tbody></table></div><h3 class="subheading">处理异常 · ${issues.length} 项</h3><div class="table-wrap"><table><thead><tr><th>阶段 / 条目</th><th>来源</th><th>问题及保留结果</th></tr></thead><tbody>${issues.map(item=>`<tr><td>${esc(item.phase||'阶段未记录')} ${esc(item.requirementId||'')}</td><td>${esc(list(item.sources).filter(object).map(source=>[source.file,source.id].filter(Boolean).join(' / ')).join('、')||'未记录')}</td><td class="prose">${esc(nonempty(item.reason))}${Number.isFinite(Number(item.retained))?`<small>保留有效记录 ${Number(item.retained)} 条</small>`:''}</td></tr>`).join('')||'<tr><td colspan="3">记录中没有列出处理异常；这不替代对原文与解析质量的人工复核。</td></tr>'}</tbody></table></div></section>`;
}

function sourceAppendix(registry){
  return `<section id="sources" class="section"><div class="section-head"><span>06</span><h2>依据与证据来源附录</h2></div><p class="section-intro">本文件保存引文、文件名、标识和已记录位置，可离线对照；不包含原始附件，也不依赖本机服务地址。正文已完整呈现引文，以下全文副本可展开查阅；打印时自动显示。需核验附件时，请回到系统按文档标识打开原件。</p>${registry.records.map(record=>{
    const unique=new Map();for(const reference of record.references){const key=reference.role+'|'+reference.location+'|'+reference.quote;if(!unique.has(key))unique.set(key,{...reference,uses:[]});unique.get(key).uses.push(reference);}
    return `<article id="${record.anchor}" class="source-card"><header><div><span class="eyebrow">${[...record.roles].map(esc).join(' / ')}</span><h3>${esc(record.name)}</h3></div><span class="tag">涉及 ${record.rows.size} 项要求</span></header><p class="source-meta">文档 ID：${esc(record.document_id||'未记录')} · 资料库 ID：${esc(record.dataset_id||'未记录')}</p><p>对应条目：${[...record.rows.values()].map(row=>`<a href="#requirement-${row.index+1}">${esc(row.id)}</a>`).join('、')}</p><details class="source-copy"><summary>展开该来源保存的 ${unique.size} 段完整引文副本</summary><div class="source-copy-content">${[...unique.values()].map(reference=>`<div class="source-excerpt"><p class="source-meta">${esc(reference.role)} · ${esc(reference.location)}</p><blockquote>${esc(nonempty(reference.quote,'未保存正文引文，仅保留文件或来源标识。'))}</blockquote><p class="caption">对应引用：${reference.uses.map(use=>`<a href="#${use.refId}">${esc(use.referenceLabel)}</a>${use.role==='结果证据'?'（'+esc(evidenceKindInfo(use.kind,{document:use.isDocument}).label)+'）':''}${use.aliases.filter(alias=>alias!==use.sourceId.trim()).length?'；同组原编号 '+use.aliases.filter(alias=>alias!==use.sourceId.trim()).map(alias=>esc(use.row.id+' · '+alias)).join('、'):''}`).join('；')}</p></div>`).join('')}</div></details></article>`;
  }).join('')||'<p class="notice">当前未保存可列示的依据或证据来源。</p>'}</section>`;
}

function pick(value,keys){
  const result={};if(!object(value))return result;
  for(const key of keys)if(value[key]!==undefined&&['string','number','boolean'].includes(typeof value[key]))result[key]=value[key];
  return result;
}
function cleanSource(value){
  const out=pick(value,['id','document_id','doc_id','dataset_id','kb_id','document_name','doc_name','name','ordinal','content','content_with_weight']);
  for(const key of ['page_num_int','positions','position_int'])if(Array.isArray(value?.[key]))out[key]=value[key].map(item=>Array.isArray(item)?item.filter(number=>typeof number==='number'&&Number.isFinite(number)):typeof item==='number'&&Number.isFinite(item)?item:null).filter(item=>item!==null);
  return out;
}
function cleanEvidence(value){return {...pick(value,['source_id','quote','analysis','kind','basis']),source:cleanSource(value?.source)};}
function cleanResult(value){return {...pick(value,['requirementId','status','project_plan','analysis','gaps','recommendation','checkType','retrievalRounds','retrievedChunks','retryable','humanEdited']),evidence:list(value?.evidence).filter(object).map(cleanEvidence)};}
function cleanRequirement(value){return {...pick(value,['id','type','category','title','subject','criterion','document_name','rationale','quote','question','userAdded','userEdited']),source:cleanSource(value?.source)};}
function rawAppendix(report){
  // A report export never serializes application/session/model-connection objects.
  const data={format:'LIFT-HTML-REPORT-V4',report:pick(report,['title','mode','scope','kb','kbName','modelLabel','createdAt','finishedAt','editedAt','complete','confirmed','awaitingConfirmation','limited','interruption']),
    baselineDocs:list(report.baselineDocs).filter(object).map(value=>pick(value,['id','name','document_id','document_name'])),targetDocs:list(report.targetDocs).filter(object).map(value=>pick(value,['id','name','document_id','document_name'])),
    requirements:list(report.requirements).filter(object).map(cleanRequirement),results:list(report.results).filter(object).map(cleanResult),
    textExtraction:list(report.textExtraction).filter(object).map(value=>pick(value,['name','method','characters'])),
    skippedFiles:list(report.skippedFiles).filter(object).map(value=>pick(value,['id','name','role','reason'])),
    issues:list(report.issues).filter(object).map(value=>({...pick(value,['phase','requirementId','reason','retained']),sources:list(value.sources).filter(object).map(source=>pick(source,['id','file','ordinal']))})),
    polished:list(report.polished).flat().filter(object).map(value=>pick(value,['id','status','headline','summary','action']))};
  if(Array.isArray(report.rawReport))data.rawReport=report.rawReport.filter(object).map(value=>({...pick(value,['id','type','category','question','criterion']),answer:cleanResult(value.answer)}));
  return `<section id="raw-data" class="section"><div class="section-head"><span>07</span><h2>原始结构数据</h2></div><p class="section-intro">保留与审查内容有关的原始要求、结果和处理记录，便于追溯。图表去重不会删除此处已保存的原始条目；应用连接配置、密钥和会话信息不纳入本附录。</p><details class="raw-data"><summary>展开审查结构数据（JSON）</summary><pre>${esc(JSON.stringify(data,null,2))}</pre></details></section>`;
}

const STYLE=`
.source-copy summary{cursor:pointer;color:#4c6e97;font-size:12px;padding:8px 0}.source-copy-content{padding-top:2px}
:root{color-scheme:light;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;color:#24344b;background:#edf1f7;font-size:15px;line-height:1.75;font-synthesis:none}*{box-sizing:border-box}body{margin:0;padding:32px 18px}a{color:#315f95;text-decoration:none;overflow-wrap:anywhere}a:hover{text-decoration:underline}svg{display:block;width:100%;height:auto;font-family:inherit}.report{max-width:1120px;margin:0 auto;background:#fff;border:1px solid #dce4ef;border-radius:15px;box-shadow:0 16px 50px #263d6310;overflow:hidden}.cover{padding:48px 56px 34px;border-bottom:1px solid #d9e3ef;background:linear-gradient(135deg,#f5f8fc,#fff 75%)}.eyebrow{display:block;color:#59728f;font-size:12px;letter-spacing:.09em;margin-bottom:9px}.cover h1{font-size:28px;line-height:1.5;margin:0 0 12px;letter-spacing:.02em;color:#1c304b}.cover h1 small{display:block;font-size:16px;font-weight:500;color:#526985;margin-top:4px}.cover-heading{display:flex;justify-content:space-between;align-items:flex-start;gap:22px}.cover .badge{margin-top:7px}.cover-meta{display:grid;grid-template-columns:1fr 1fr;gap:8px 24px;margin:22px 0 0;font-size:13px}.cover-meta>div{display:grid;grid-template-columns:80px minmax(0,1fr);gap:7px}.cover-meta dt,.facts dt{color:#718098}.cover-meta dd,.facts dd{margin:0;overflow-wrap:anywhere}.toc{padding:19px 56px;background:#f9fbfe;border-bottom:1px solid #e1e7f0;display:flex;gap:9px 24px;flex-wrap:wrap;font-size:13px}.toc b{color:#75869b;font-weight:500}.section{padding:32px 56px;border-top:1px solid #e3e9f1}.section:first-of-type{border-top:0}.section-head{display:flex;align-items:center;gap:12px;margin-bottom:15px}.section-head>span{color:#7390b3;font-size:12px;letter-spacing:.07em;font-variant-numeric:tabular-nums}.section-head h2{font-size:20px;line-height:1.5;margin:0;color:#213a59;font-weight:650}.section-intro{color:#65768d;font-size:13px;margin:0 0 19px}.assessment{border-left:3px solid #5f83ae;border-radius:5px;background:#f5f8fc;padding:18px 22px}.assessment.bad{border-color:#b8535a}.assessment.warn{border-color:#b58a35}.assessment.ok{border-color:#2c8768}.assessment h3{margin:0 0 7px;font-size:20px;line-height:1.5}.assessment p{margin:0;font-size:14px}.kpis{display:grid;grid-template-columns:repeat(4,1fr);border:1px solid #dce4ee;border-radius:8px;margin:20px 0;overflow:hidden}.kpis>div{padding:16px 20px;border-right:1px solid #e2e8f1}.kpis>div:last-child{border:0}.kpis span{display:block;font-size:12px;color:#6d7e96}.kpis strong{font-size:30px;font-weight:650;letter-spacing:-.03em;display:inline-block;line-height:1.3;margin:4px 5px 3px 0;color:#263e5d}.kpis small{font-size:12px;color:#6d7e96}.summary-line{font-size:14px;margin:15px 0;color:#4a607b}.notice{padding:12px 15px;background:#f5f8fc;border:1px solid #dde6f1;border-radius:5px;color:#61748d;font-size:13px;margin:15px 0}.notice.warning{background:#fcf9f2;border-color:#ebdfc3;color:#826c36}.notice h4,.notice p{margin:0}.chart-grid{display:grid;grid-template-columns:1fr;gap:18px}.chart{border:1px solid #dce4ef;border-radius:8px;margin:0;padding:21px 24px;background:#fff;break-inside:avoid}.chart figcaption{font-size:16px;color:#2d4665;font-weight:600;margin-bottom:16px}.chart figcaption>span{font-size:11px;color:#8290a4;font-weight:500;margin-right:10px}.donut-layout{display:grid;grid-template-columns:minmax(190px,34%) minmax(0,1fr);gap:28px;align-items:center}.donut{max-width:240px;justify-self:center;width:100%}.svg-number{font-size:33px;font-weight:650;fill:#243b58}.svg-label{font-size:13px;fill:#506883}.caption{font-size:12px;color:#74839a;line-height:1.8;margin:13px 0 0}.color-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:8px}.table-wrap{overflow-x:auto;margin:10px 0 0}table{border-collapse:collapse;width:100%;font-size:13px;line-height:1.7;table-layout:auto}th,td{padding:10px 12px;text-align:left;border-bottom:1px solid #e4eaf2;vertical-align:top;overflow-wrap:anywhere}thead th{font-size:12px;color:#61738c;background:#f4f7fb;font-weight:550}tbody th{font-weight:550}tbody tr:last-child td,tbody tr:last-child th{border-bottom:0}.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}.legend th,.legend td{padding:7px 9px}.matrix{font-size:11.5px;white-space:nowrap}.matrix th,.matrix td{padding:8px}.total{background:#f7f9fc;font-weight:600}.badge{display:inline-flex;align-items:center;gap:6px;border:1px solid #cdd8e6;border-radius:5px;padding:3px 8px;background:#f5f7fa;color:#63758d;font-size:12px;line-height:1.6;white-space:nowrap;font-weight:500}.badge:before{content:'';width:5px;height:5px;border-radius:50%;background:currentColor}.badge.ok{color:#2c785e;border-color:#bddbcd;background:#f4faf6}.badge.bad{color:#aa464d;border-color:#e4bcc1;background:#fdf7f7}.badge.warn{color:#98742d;border-color:#e7d7ae;background:#fcfaf3}.badge.info{color:#446e9b;border-color:#c0d3e7;background:#f5f8fd}.badge.review{color:#7d67a4;border-color:#d7cbe6;background:#faf7fd}.badge.conflict{color:#9e6b47;border-color:#e0cbb8;background:#fcf9f5}.tag,.tags span{display:inline-block;border:1px solid #dde5ef;border-radius:4px;padding:2px 7px;color:#708198;font-size:11px;background:#f7f9fc;line-height:1.6}.fact-row{display:flex;gap:10px 28px;flex-wrap:wrap;font-size:12px;color:#6d7e95}.fact-row b{color:#354e6e;font-weight:550;margin-left:5px}.actions-table th:nth-child(1){width:25%}.actions-table th:nth-child(2){width:13%}.actions-table small,td>small{display:block;color:#7c8ca2;font-size:11px;margin-top:5px}.prose{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.85}.requirements{margin-top:28px}.requirement{border:1px solid #d9e3ef;border-radius:9px;overflow:hidden;margin:0 0 26px;scroll-margin-top:18px}.requirement-head{display:flex;justify-content:space-between;gap:16px;padding:20px 22px;background:#f5f8fc;border-bottom:1px solid #dfe7f1}.requirement-head h3{display:inline;font-size:18px;font-weight:650;line-height:1.6;margin:0}.requirement-id{font-size:13px;color:#5f7fa7;font-weight:550;margin-right:12px;white-space:nowrap}.tags{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}.requirement>section,.requirement>.question{margin:18px 22px}.requirement h4{font-size:14px;color:#395776;font-weight:600;margin:0 0 7px}.requirement h5{font-size:12px;color:#59718f;margin:10px 0 4px}.requirement p{margin:0;font-size:14px}.criterion{padding:12px 15px;background:#f5f8fc;border-radius:5px}.origin{padding-bottom:17px;border-bottom:1px solid #e6ebf2}.source-name{font-size:12px!important;color:#607894;overflow-wrap:anywhere;margin:8px 0!important}.source-meta{display:block;color:#8190a3;font-size:11px;overflow-wrap:anywhere}.analysis-grid{display:grid;grid-template-columns:1fr 1fr;gap:20px 25px;margin:20px 22px}.analysis-grid section{min-width:0;padding:12px 14px;border:1px solid #dce4ef;border-radius:8px;background:#f8fafd}.analysis-grid section:last-child:nth-child(odd){grid-column:1/-1}.analysis-grid .analysis-analysis{border-left:3px solid #5681b1}.analysis-grid .analysis-gaps{border-left:3px solid #b58a35}.analysis-grid .analysis-action{border-left:3px solid #7e9fc8;background:#eff5fd}.analysis-grid p{font-size:14px;margin:0}.evidence{border-top:1px solid #e4eaf3;padding-top:16px}.quote-card{border:1px solid #e0e7f1;border-radius:5px;padding:13px 15px;margin:13px 0;background:#fcfdff}.quote-header{display:flex;align-items:center;gap:10px;font-size:12px;color:#59718e}blockquote{margin:10px 0 0;padding:11px 14px;border-left:2px solid #b4c6dd;background:#f5f8fc;white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px;color:#415a77;line-height:1.9}.question{border-top:1px solid #e1e8f1;padding-top:12px;font-size:12px;color:#637995}.question p{margin-top:8px;font-size:12px}.question summary,.raw-data summary{cursor:pointer;color:#3b608b;font-size:13px}.backlink{display:block;font-size:12px;margin:16px 22px 20px;text-align:right}.two-columns{display:grid;grid-template-columns:1fr 1fr;gap:18px}.box{border:1px solid #dce5ef;border-radius:7px;padding:18px 20px;font-size:13px}.box h3{font-size:15px;margin:0 0 10px;color:#3c5879}.box p{margin:5px 0}.method{margin-top:18px}.method ul{padding-left:20px;color:#526b87}.method li{margin:7px 0}.file-list{list-style:none;padding:0;margin:0}.file-list li{padding:7px 0;border-bottom:1px solid #e7edf4;overflow-wrap:anywhere}.file-list li:last-child{border:0}.file-list b{font-size:13px;font-weight:550}.file-list small{display:block;font-size:11px;color:#8090a4;margin-top:3px}.facts{display:grid;grid-template-columns:1fr 1fr;font-size:13px;border:1px solid #dce4ef;border-radius:7px;padding:5px 19px;margin:20px 0}.facts>div{padding:10px 8px;border-bottom:1px solid #e4eaf2;display:grid;grid-template-columns:92px minmax(0,1fr);gap:10px}.facts>div:nth-last-child(-n+2){border:0}.subheading{font-size:16px;color:#355273;margin:26px 0 11px}.source-card{border:1px solid #dce4ef;border-radius:7px;padding:20px 22px;margin:20px 0;scroll-margin-top:18px}.source-card header{display:flex;justify-content:space-between;align-items:flex-start;gap:15px}.source-card h3{font-size:17px;color:#345473;margin:0 0 6px;overflow-wrap:anywhere}.source-card p{font-size:13px}.source-card .eyebrow{font-size:11px}.source-excerpt{margin-top:17px;padding-top:12px;border-top:1px solid #e6ecf3}.source-excerpt .caption{font-size:11px;margin-top:7px}.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:11px;word-break:break-all}.raw-data{border:1px solid #dae3ef;border-radius:7px;padding:15px 18px;background:#f9fbfd}.raw-data pre{white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;font-size:11px;line-height:1.75;color:#536b87;max-height:700px;overflow:auto;padding-top:10px}.muted{color:#7b8ba1}.report-footer{padding:22px 56px 28px;font-size:12px;color:#7d8da2;background:#f7f9fc;border-top:1px solid #dce5f0}.report-footer p{margin:4px 0}.report-footer strong{color:#546e8c;font-weight:550}
@media(max-width:760px){body{padding:0}.report{border:0;border-radius:0;box-shadow:none}.cover{padding:30px 22px 24px}.cover h1{font-size:23px}.cover h1 small{font-size:14px}.cover-heading{display:block}.cover .badge{margin-top:12px}.cover-meta{grid-template-columns:1fr}.toc{padding:15px 22px;gap:8px 18px}.section{padding:26px 22px}.section-head h2{font-size:19px}.kpis{grid-template-columns:1fr 1fr}.kpis>div:nth-child(2){border-right:0}.kpis>div:nth-child(-n+2){border-bottom:1px solid #dfe6f0}.kpis>div{padding:13px 15px}.kpis strong{font-size:27px}.donut-layout{grid-template-columns:1fr;gap:10px}.donut{max-width:210px}.chart{padding:17px 15px}.analysis-grid,.two-columns,.facts{grid-template-columns:1fr}.facts>div:nth-last-child(2){border-bottom:1px solid #e4eaf2}.requirement-head{padding:16px;display:block}.requirement-head>.badge{margin-top:10px}.requirement-head h3{font-size:17px}.requirement>section,.requirement>.question{margin:16px}.analysis-grid{margin:16px}.backlink{margin-right:16px}.source-card{padding:17px}.source-card header{display:block}.source-card .tag{margin-top:9px}.report-footer{padding:20px 22px}.matrix{font-size:11px}.table-wrap table{min-width:570px}.legend{min-width:0!important}.section-intro{font-size:13px}}
@page{size:A4;margin:16mm 13mm 18mm}
@media print{.source-copy::details-content{display:block!important;content-visibility:visible!important}.source-copy>.source-copy-content{display:block!important;content-visibility:visible!important}.source-copy summary{font-size:8pt}:root{background:#fff;font-size:10pt;color:#1d2f46}body{padding:0}.report{max-width:none;border:0;border-radius:0;box-shadow:none}.cover{padding:10mm 0 7mm;background:#fff;break-inside:avoid}.cover h1{font-size:22pt}.cover h1 small{font-size:12pt}.cover-meta{font-size:9pt}.toc{padding:5mm 0;background:#fff;gap:2mm 6mm;font-size:9pt}.section{padding:7mm 0;border-top:1px solid #ccd7e6}.section-head{break-after:avoid-page}.section-head h2{font-size:15pt}.section-intro{font-size:9pt}.assessment{padding:4mm 5mm;break-inside:avoid}.assessment h3{font-size:14pt}.assessment p{font-size:10pt}.kpis{break-inside:avoid}.kpis>div{padding:3mm 4mm}.kpis strong{font-size:23pt}.chart{padding:4mm 5mm;break-inside:avoid;box-shadow:none}.chart figcaption{font-size:11pt}.caption,.source-meta{font-size:8pt}.donut-layout{grid-template-columns:34% 1fr}.donut{max-width:42mm}svg{max-height:110mm}table{font-size:8.5pt}thead{display:table-header-group}th,td{padding:2mm}tr{break-inside:avoid}a{color:inherit;text-decoration:none}.table-wrap{overflow:visible}.table-wrap table{min-width:0}.requirement{break-inside:auto;border-radius:2mm;margin-bottom:6mm}.requirement-head{break-inside:avoid;break-after:avoid-page;padding:4mm 5mm}.requirement-head h3{font-size:13pt}.requirement>section,.requirement>.question{margin:4mm 5mm}.requirement h4{font-size:10pt;break-after:avoid-page}.requirement p,.analysis-grid p{font-size:9.5pt}.analysis-grid{grid-template-columns:1fr 1fr;gap:4mm 5mm;margin:4mm 5mm}.criterion{break-inside:avoid}.quote-card{break-inside:auto}.quote-header{break-after:avoid-page}blockquote{font-size:9pt;margin-top:2mm;padding:2.5mm 3mm;box-decoration-break:clone}.backlink{display:none}.two-columns{grid-template-columns:1fr 1fr;gap:4mm}.box{padding:4mm}.box h3{font-size:11pt}.facts{grid-template-columns:1fr 1fr}.facts>div{font-size:9pt}.source-card{break-inside:auto;padding:4mm;margin:4mm 0}.source-card header{break-after:avoid-page}.source-excerpt{break-inside:auto}.raw-data pre{max-height:none;overflow:visible;font-size:8pt}.badge{font-size:8pt;padding:1mm 2mm}.report-footer{padding:5mm 0;background:#fff;font-size:8.5pt}*{-webkit-print-color-adjust:exact;print-color-adjust:exact}}
`;

export function buildReportHtml(value){
  const report=object(value)?value:{},metrics=reportWorkspaceMetrics(report),phase=phaseOf(report,metrics),coverage=coverageData(metrics),registry=sourceRegistry(metrics);
  const exportedAt=dateLabel(new Date().toISOString());
  const positiveShare=percent(metrics.counts['已满足'],metrics.total),doneShare=percent(metrics.done,metrics.total);
  const orphanOrUnknown=list(report.results).filter(object).filter(item=>!metrics.rows.some(row=>row.key===text(item.requirementId).trim())||!VERDICTS.includes(item.status)).length;
  const summary=`纳入 ${metrics.total} 项要求，已核查 ${metrics.done} 项，未核查 ${metrics.pending} 项。其中：${VERDICTS.map(status=>status+' '+metrics.counts[status]+' 项').join('，')}。`;
  return `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'"><meta name="color-scheme" content="light"><title>${esc(nonempty(report.title,'未命名项目'))} · 工程技术审查报告</title><style>${STYLE}</style></head><body><main class="report" data-export-format="html-v4"><header class="cover"><div class="cover-heading"><div><span class="eyebrow">工程技术符合性审查 / 归档报告</span><h1>${esc(nonempty(report.title,'未命名项目'))}<small>合同与技术协议 · 逐项核查报告</small></h1></div>${badge(phase.label)}</div><dl class="cover-meta"><div><dt>资料范围</dt><dd>${esc(nonempty(report.kbName||report.kb,'未记录资料库名称'))}</dd></div><div><dt>模型记录</dt><dd>${esc(nonempty(report.modelLabel))}</dd></div><div><dt>任务建立</dt><dd>${esc(dateLabel(report.createdAt))}</dd></div><div><dt>核查结束</dt><dd>${esc(dateLabel(report.finishedAt))}</dd></div><div><dt>导出时间</dt><dd>${esc(exportedAt)}</dd></div><div><dt>文件形式</dt><dd>独立 HTML · 图表与引文已内嵌</dd></div></dl></header><nav class="toc" aria-label="报告目录"><b>目录</b><a href="#summary">01 执行摘要</a><a href="#statistics">02 统计与图表</a><a href="#actions">03 关注事项</a><a href="#details">04 逐项分析</a><a href="#scope">05 范围与记录</a><a href="#sources">06 来源附录</a><a href="#raw-data">07 原始数据</a></nav><section id="summary" class="section"><div class="section-head"><span>01</span><h2>执行摘要</h2></div><div class="assessment ${phase.tone}"><h3>${esc(phase.heading)}</h3><p>${esc(phase.body)}</p></div><div class="kpis"><div><span>纳入要求</span><strong>${metrics.total}</strong><small>项</small></div><div><span>已核查</span><strong>${metrics.done}</strong><small>项 / ${doneShare}</small></div><div><span>需处理或复核</span><strong>${metrics.attention}</strong><small>项</small></div><div><span>有标识的证据文件</span><strong>${metrics.sourceFiles}</strong><small>份</small></div></div><p class="summary-line">${esc(summary)}</p><p class="summary-line">已满足占全部要求的 ${positiveShare}；保存引用的已核查要求为 ${coverage.withEvidence} / ${metrics.done} 项。以上仅为当前记录的数量和结构统计，不是验收通过率或全文覆盖率。</p>${orphanOrUnknown?`<p class="notice warning">有 ${orphanOrUnknown} 条历史结果未对应当前纳入要求或使用未知判定，未被计为已满足；原始相关记录仍保留在结构数据附录。</p>`:''}${report.interruption?`<p class="notice warning">中断原因：${esc(report.interruption)}</p>`:''}</section><section id="statistics" class="section"><div class="section-head"><span>02</span><h2>统计与数据图表</h2></div><p class="section-intro">统计以去重后的纳入要求为分母。所有图形均由本报告保存的数据绘制，数值表随图提供；文件离线打开仍可查看。</p><div class="chart-grid">${statusChart(metrics)}${categoryChart(metrics)}${coverageChart(metrics,coverage)}${frequencyChart(registry)}</div></section>${attentionSection(metrics)}${detailSection(report,metrics,registry)}${scopeSection(report,metrics)}${sourceAppendix(registry)}${rawAppendix(report)}<footer class="report-footer"><strong>工程技术符合性审查 · V4 HTML 报告</strong><p>文件已内嵌图表、统计、逐项分析和保存的引文，使用浏览器即可离线阅读；原始附件需另行保存。可使用浏览器打印功能打印或另存为 PDF。</p><p>分析与判定来自已有审查记录，导出不重新调用模型，也不改变原始结论。若记录尚未完成或有覆盖限制，应以相应阶段和边界提示为准。</p></footer></main></body></html>`;
}
