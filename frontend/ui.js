export const $=id=>document.getElementById(id);
export const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let toastTimer;
export function toast(message,error=false){$('toast').textContent=message;$('toast').className='show'+(error?' error':'');clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').className='',6500);}
export async function busy(button,fn){if(button?.disabled)return; if(button)button.disabled=true;try{return await fn();}catch(e){if(e.name!=='AbortError')toast(e.message||String(e),true);return null;}finally{if(button)button.disabled=false;}}
export function empty(message){return `<div class="empty-state"><p>${esc(message)}</p></div>`;}
export function setOptions(select,list,placeholder,{placeholderDisabled=false}={}){const value=select.value;select.innerHTML=`<option value=""${placeholderDisabled?' disabled':''}>${esc(placeholder)}</option>`+list.map(x=>`<option value="${esc(x.value)}">${esc(x.label)}</option>`).join('');if([...select.options].some(o=>o.value===value))select.value=value;}
export function fileIcon(name){const ext=(name||'').split('.').pop().toLowerCase();return `<span class="file-type ${/^[a-z0-9]+$/.test(ext)?ext:''}">${esc(ext.slice(0,4).toUpperCase())}</span>`;}
export function date(value){if(!value)return '—';const d=new Date(value);return isNaN(d)?String(value):new Intl.DateTimeFormat('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(d);}
export function download(name,content,type='text/markdown;charset=utf-8'){const url=URL.createObjectURL(new Blob([content],{type}));const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
export function loadLocal(key,fallback){try{return JSON.parse(localStorage.getItem(key))??fallback;}catch{return fallback;}}
export function saveLocal(key,value){try{localStorage.setItem(key,JSON.stringify(value));}catch{toast('浏览器存储空间不足，请导出需要保留的报告。',true);}}
function inline(value){
  const text=String(value),pattern=/(`+)(.+?)\1(?!`)/g;
  const emphasis=s=>esc(s).replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');
  let out='',start=0,match;
  while((match=pattern.exec(text))){out+=emphasis(text.slice(start,match.index))+'<code>'+esc(match[2])+'</code>';start=pattern.lastIndex;}
  return out+emphasis(text.slice(start));
}
export function markdown(value){
  const lines=String(value||'').split(/\r?\n/);let out='',list='',code=false;
  const closeList=()=>{if(list){out+='</'+list+'>';list='';}};
  for(let i=0;i<lines.length;i++){const line=lines[i];
    if(line.startsWith('```')){closeList();out+=code?'</code></pre>':'<pre><code>';code=!code;continue;}
    if(code){out+=esc(line)+'\n';continue;}
    const item=/^\s*(?:([-*+])|(\d+)[.)])\s+(.*)$/.exec(line);
    if(item){const tag=item[2]?'ol':'ul';if(list!==tag){closeList();list=tag;out+='<'+tag+(tag==='ol'&&Number(item[2])!==1?' start="'+Number(item[2])+'"':'')+'>';}out+='<li>'+inline(item[3])+'</li>';continue;}
    closeList();
    if(line.includes('|')&&/^\s*\|?\s*:?-+:?/.test(lines[i+1]||'')){
      const cells=s=>s.trim().replace(/^\||\|$/g,'').split('|').map(s=>s.trim());
      out+='<div class="table-scroll"><table><thead><tr>'+cells(line).map(s=>'<th>'+inline(s)+'</th>').join('')+'</tr></thead><tbody>';i++;
      while(lines[i+1]?.includes('|'))out+='<tr>'+cells(lines[++i]).map(s=>'<td>'+inline(s)+'</td>').join('')+'</tr>';
      out+='</tbody></table></div>';continue;
    }
    const head=/^(#{1,6})\s+(.+)/.exec(line);if(head){const n=Math.min(head[1].length,3);out+=`<h${n}>${inline(head[2])}</h${n}>`;}
    else if(line.startsWith('> '))out+='<blockquote>'+inline(line.slice(2))+'</blockquote>';
    else if(line.trim())out+='<p>'+inline(line)+'</p>';
  }
  closeList();if(code)out+='</code></pre>';return out;
}
export function answerHtml(value){const str=String(value||'');const think=/<think>([\s\S]*?)(?:<\/think>|$)/.exec(str);const body=str.replace(/<think>[\s\S]*?(?:<\/think>|$)/g,'');return (think?`<details class="reasoning"><summary>思考过程（模型返回）</summary><p>${esc(think[1])}</p></details>`:'')+`<div class="markdown">${markdown(body||(!think?'正在等待回答…':''))}</div>`;}
// These labels explain saved evidence; they never reclassify it or change a verdict.
export function evidenceKindInfo(kind,{document=false}={}){
 const labels={
  '支持':document?{label:'文件存在记录',description:'仅支持所需文件已存在，不代表文件内容、测试结果或验收合格。',tone:'support'}:{label:'符合要求的依据',description:'支持本条要求全部或部分符合；是否足以判定，仍取决于适用条件和材料性质。',tone:'support'},
  '反证':{label:'不符合要求的依据',description:'材料显示本条要求存在未达到或相矛盾的情况，须核对对象、阶段和适用条件。',tone:'counter'},
  '计划':{label:'计划或承诺',description:'原记录将其标为计划类；应结合具体参数、实现机制及核查目标判断，不能仅凭承诺认定已完成。',tone:'plan'},
  '背景':{label:'背景参考',description:'用于说明要求、条件或上下文，不能单独证明本条要求已满足或不满足。',tone:'context'}
 };
 return labels[String(kind??'')]||{label:'未分类参考',description:'原记录未提供可识别的材料作用分类，请结合原文和引用说明人工复核。',tone:'unknown'};
}
export function evidenceReference(requirementId,item,index=0){
 return (String(requirementId??'').trim()||'当前条目')+' · '+(String(item?.source_id??'').trim()||'引用 '+(index+1));
}
export function evidenceDisplayEntries(evidence){
 if(!Array.isArray(evidence))return [];
 const entries=[],known=new Map();
 for(const [index,item] of evidence.entries()){
  if(!item||typeof item!=='object'||Array.isArray(item))continue;
  const source=item.source&&typeof item.source==='object'?item.source:{};
  const quote=String(item.quote??source.content??source.content_with_weight??'');
  const identity=String(source.document_id||source.doc_id||source.document_name||source.doc_name||source.name||'');
  const alias=String(item.source_id??'').trim();
  // Different positions, classifications or explanations remain separate even
  // when their quotes look alike. An unidentified empty source is not merged.
  const key=quote.trim()&&identity?JSON.stringify([String(source.dataset_id||source.kb_id||''),identity,String(source.id||''),source.ordinal??null,source.page??null,source.page_num_int??null,source.positions??source.position_int??null,source.excerpt_start??null,source.excerpt_end??null,quote,String(item.kind??''),String(item.analysis??'')]):null;
  const existing=key===null?undefined:known.get(key);
  if(existing){existing.duplicateCount++;if(alias&&!existing.aliases.includes(alias))existing.aliases.push(alias);continue;}
  const entry={item,index,aliases:alias?[alias]:[],duplicateCount:0};entries.push(entry);if(key!==null)known.set(key,entry);
 }
 return entries;
}
export function sourcesHtml(chunks,question='',options={}){
 if(!Array.isArray(chunks)||!chunks.length)return '';
 const compact=options.compact===true;
 const terms=[...new Set((question.toLowerCase().match(/[a-z0-9_]{2,}|[\u4e00-\u9fff]{2,}/g)||[]).flatMap(t=>/^[\u4e00-\u9fff]+$/.test(t)?Array.from({length:Math.max(0,t.length-1)},(_,i)=>t.slice(i,i+2)):[t]))];
 const items=chunks.map((c,i)=>{
 const name=c.document_name||c.doc_name||c.name||c.document_id||'文档切片';
 const ext=name.split('.').pop().toLowerCase(),type=ext==='pdf'?'pdf':['doc','docx'].includes(ext)?'docx':['xls','xlsx','csv','tsv','ods'].includes(ext)?'sheet':'other';
 const content=String(c.content||c.content_with_weight||'').replace(/<\/?(?:em|mark)>/gi,'');
 // PDF line breaks can split a condition ("不\n得低于 8 小时"). Rank
 // complete sentences in reports, preserving the saved source verbatim.
 const sentences=compact?(content.match(/[^。！？!?]+[。！？!?]?/g)||[content]).map(sentence=>sentence.trim()).filter(Boolean):(content.match(/[^。！？!?\n]+[。！？!?]?|[^\n]+/g)||[content]);
 const ranked=sentences.map((text,index)=>({text,index,score:terms.reduce((n,t)=>n+(text.toLowerCase().includes(t)?1:0),0)})).sort((a,b)=>b.score-a.score);
 const selected=ranked.filter(x=>x.score>0).slice(0,compact?1:2).sort((a,b)=>a.index-b.index);
 const excerpt=selected.length?selected.map(x=>`<mark><strong>${esc(x.text)}</strong></mark>`).join(' … '):compact?esc(sentences.find(sentence=>sentence.trim())||content):esc(content.slice(0,220))+(content.length>220?'…':'');
 const fileSource={id:c.id,document_id:c.document_id||c.doc_id,dataset_id:c.dataset_id||c.kb_id,document_name:name,positions:c.positions||c.position_int,page_num_int:c.page_num_int,content};
 const number=compact&&chunks.length===1?'':'['+(i+1)+'] ';
 const nameOnly=compact&&content.trim()===String(name).trim();
 return `<div class="source-item source-${type}"><header>${fileIcon(name)}${fileSource.document_id?`<button type="button" class="source-file-link" data-file-source="${esc(JSON.stringify(fileSource))}" title="打开原文件并查看当前引用">${number}${esc(name)} ↗</button>`:`<strong>${number}${esc(name)}</strong>`}</header>${nameOnly?'<p class="hint">本条引用来自知识库文件名清单。</p>':`<div class="source-excerpt">${compact?'<span class="source-excerpt-label">原文摘要 · 词语定位</span>':''}${excerpt}</div><details class="source-full"><summary>${compact?'查看完整引用片段':'展开完整切片'}${selected.length?'':'（未找到明确匹配句）'}</summary><p>${compact?esc(content):sentences.map((t,n)=>selected.some(x=>x.index===n)?`<mark><strong>${esc(t)}</strong></mark>`:esc(t)).join('\n')}</p></details>`}</div>`;
 }).join('');
 return compact?`<div class="sources sources-compact">${items}</div>`:`<details class="sources"><summary>↗ 查看 ${chunks.length} 条来源证据</summary><p class="hint">重点句按当前问题的词语匹配突出，供核对原文。</p>${items}</details>`;
}
let modalSubmit;
export function modal(title,body,onSubmit,button='保存'){$('modal').dispatchEvent(new Event('modal-replace'));$('modal').querySelectorAll('[data-modal-extra]').forEach(el=>el.remove());$('modal').classList.remove('requirement-editor-modal','workflow-modal','workflow-fullscreen');$('modalCancel').textContent='取消';$('modalTitle').textContent=title;$('modalBody').innerHTML=body;$('modalSubmit').textContent=button;$('modalSubmit').hidden=!onSubmit;modalSubmit=onSubmit;$('modal').showModal();}
$('modalClose').onclick=$('modalCancel').onclick=()=>$('modal').close();
$('modalForm').onsubmit=e=>{e.preventDefault();busy($('modalSubmit'),async()=>{await modalSubmit?.(new FormData($('modalForm')));$('modal').close();});};
export function confirmAction(title,message,fn){modal(title,`<p>${esc(message)}</p>`,fn,'确认');}
