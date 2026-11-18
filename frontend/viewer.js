import {api,arrayOf,proxyUrl} from './api.js';
import {$,esc,empty} from './ui.js';

let current=null,sequence=0,blobUrl=null,controller=null;
let pdfTask=null,pdfDocument=null;
const dialog=$('fileViewer');
const office=/\.(docx?|xlsx?|pptx?|odt|ods|odp|rtf)$/i;
const images=/\.(png|jpe?g|gif|webp|bmp|avif|ico)$/i;
const plain=/\.(txt|md|csv|tsv|json|xml|html?|log|ya?ml|py|js|css|sql)$/i;
export function sourcePage(source){
 const positions=source.positions||source.position_int||[];
 const p=positions.find(x=>Array.isArray(x)&&Number(x[0])>0)?.[0];
 const alt=source.page_num_int||source.page_number||source.page;
 const n=Number(p??(Array.isArray(alt)?alt[0]:alt));
 return Number.isInteger(n)&&n>0?n:null;
}
function clearBlob(){if(blobUrl){URL.revokeObjectURL(blobUrl);blobUrl=null;}}
function reset(){sequence++;controller?.abort();controller=null;current=null;pdfTask?.destroy();pdfTask=null;pdfDocument=null;$('viewerPdfControls').hidden=true;$('viewerBody').replaceChildren();clearBlob();}
$('viewerClose').onclick=()=>dialog.close();dialog.addEventListener('close',reset);
dialog.addEventListener('click',e=>{if(e.target===dialog){const r=dialog.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)dialog.close();}});
$('viewerOriginal').onclick=()=>showOriginal();$('viewerParsed').onclick=()=>showParsed();

export async function openFile(source){
 const docId=source.document_id||source.doc_id||source.id;
 if(!docId)throw Error('此来源缺少文档 ID，无法定位文件');
 reset();current={...source,docId,kbId:source.dataset_id||source.kb_id,name:source.document_name||source.doc_name||source.docnm_kwd||source.name||'文件',page:sourcePage(source)};
 $('viewerTitle').textContent=current.name;
 $('viewerDownload').href=proxyUrl(`/api/v1/documents/${encodeURIComponent(docId)}`);
 $('viewerDownload').download=current.name.split('/').pop();
 $('viewerLocation').textContent=current.page?`来源记录页码：第 ${current.page} 页`:(source.content?'来源未提供页码，按引用内容定位':'文件预览');
 if(!dialog.open)dialog.showModal();
 if(source.content&&plain.test(current.name))await showParsed();else await showOriginal(true);
}
function begin(mode){controller?.abort();pdfTask?.destroy();pdfTask=null;pdfDocument=null;controller=new AbortController();const token=++sequence;clearBlob();$('viewerPdfControls').hidden=true;$('viewerBody').innerHTML=empty('正在加载文件…');$('viewerOriginal').classList.toggle('primary',mode==='original');$('viewerParsed').classList.toggle('primary',mode==='parsed');return {token,signal:controller.signal,source:current};}
function valid(token){return token===sequence&&dialog.open;}
function failure(e,token){if(!valid(token)||e.name==='AbortError')return;$('viewerPdfControls').hidden=true;$('viewerStatus').textContent='预览失败：'+e.message;$('viewerBody').innerHTML=empty('可切换“解析内容”查看，或下载原文件。');}
async function getBlob(url,signal){const r=await fetch(url,{signal});if(!r.ok){let message=`HTTP ${r.status}`;try{message=(await r.json()).message||message;}catch{}throw Error(message);}const blob=await r.blob();if(r.headers.get('content-type')?.includes('application/json')){let body;try{body=JSON.parse(await blob.text());}catch{}if(typeof body?.code==='number'&&body.code!==0&&body.message)throw Error(body.message);}return blob;}
async function showOriginal(locate=false){
 if(!current)return;const {token,signal,source}=begin('original');
 const isPdf=/\.pdf$/i.test(source.name),isOffice=office.test(source.name);
 if(!isPdf&&!isOffice&&!images.test(source.name)&&!plain.test(source.name)&&!/\.(mp3|wav|ogg|m4a|flac|mp4|webm|mov)$/i.test(source.name))return showParsed();
 try{
 $('viewerStatus').textContent=isOffice?'正在生成本机预览，首次打开可能需要片刻…':'正在读取原文件…';
 const url=isOffice?`/preview-office?document_id=${encodeURIComponent(source.docId)}&name=${encodeURIComponent(source.name)}`:proxyUrl(`/api/v1/documents/${encodeURIComponent(source.docId)}/preview`);
 const blob=await getBlob(url,signal);if(!valid(token))return;
 $('viewerBody').replaceChildren();
 if(plain.test(source.name)){
 const text=await blob.text();if(!valid(token))return;const pre=document.createElement('pre');pre.className='viewer-text';pre.textContent=text;$('viewerBody').append(pre);$('viewerStatus').textContent='文本原文件';return;
 }
 blobUrl=URL.createObjectURL(new Blob([blob],{type:isPdf||isOffice?'application/pdf':blob.type}));
 if(isPdf||isOffice){
 await renderPdf(blob,source,isOffice,token,locate);if(!valid(token))return;
 }else if(images.test(source.name)){const img=document.createElement('img');img.src=blobUrl;img.alt=source.name;img.className='viewer-image';$('viewerBody').append(img);$('viewerStatus').textContent='图片原文件';}
 else{const media=document.createElement(/\.(mp4|webm|mov)$/i.test(source.name)?'video':'audio');media.controls=true;media.src=blobUrl;media.className='viewer-media';$('viewerBody').append(media);$('viewerStatus').textContent='音视频原文件（播放能力取决于浏览器编码支持）';}
 if(isOffice&&source.content){const jump=document.createElement('button');jump.className='btn primary small';jump.textContent='定位引用内容';jump.onclick=showParsed;$('viewerStatus').append(' ',jump);}
 }catch(e){failure(e,token);}
}
async function renderPdf(blob,source,converted,token,locate){
 const pdfjs=await import('/vendor/pdfjs/pdf.mjs');pdfjs.GlobalWorkerOptions.workerSrc='/vendor/pdfjs/pdf.worker.mjs';
 if(!valid(token))return;
 pdfTask=pdfjs.getDocument({data:new Uint8Array(await blob.arrayBuffer()),isEvalSupported:false,cMapUrl:'/vendor/pdfjs/cmaps/',cMapPacked:true,standardFontDataUrl:'/vendor/pdfjs/standard_fonts/'});
 const pdf=await pdfTask.promise;if(!valid(token)){await pdf.destroy();return;}pdfDocument=pdf;
 let pageNumber=converted?1:Math.min(source.page||1,pdf.numPages),scale=1,matched=false;
 if(converted&&source.content&&locate){
 $('viewerStatus').textContent='正在定位引用内容对应的预览页…';
 const normalize=t=>t.replace(/<[^>]*>/g,'').replace(/[^\p{L}\p{N}]/gu,'').toLowerCase();const quote=normalize(source.content);
 const parts=[];for(let i=0;i<Math.min(quote.length,1200)-9;i+=10)parts.push(quote.slice(i,i+10));
 let best=0;
 for(let n=1;n<=pdf.numPages;n++){if(!valid(token))return;const page=await pdf.getPage(n);const text=normalize((await page.getTextContent()).items.map(x=>x.str||'').join(''));const score=parts.filter(x=>text.includes(x)).length;if(score>best){best=score;pageNumber=n;}}
 matched=best>=2;
 if(!matched){await showParsed();if(current===source)$('viewerLocation').textContent='Office 转换后未匹配到可靠页码，已定位对应引用内容';return;}
 }
 if(!valid(token))return;
 $('viewerBody').innerHTML='<div class="pdf-sheet"><canvas id="pdfCanvas"></canvas><div id="pdfHighlights"></div></div>';
 $('pdfTotal').textContent='/ '+pdf.numPages;$('pdfPage').max=pdf.numPages;
 let drawing=false,queued=false;
 async function draw(){if(drawing){queued=true;return;}drawing=true;do{queued=false;if(!valid(token))break;const page=await pdf.getPage(pageNumber);if(!valid(token))break;const initial=page.getViewport({scale:1});const width=Math.max(200,$('viewerBody').clientWidth-50);const view=page.getViewport({scale:Math.min(1.8,width/initial.width)*scale});const canvas=$('pdfCanvas');const ratio=Math.min(devicePixelRatio||1,2);canvas.width=Math.floor(view.width*ratio);canvas.height=Math.floor(view.height*ratio);canvas.style.width=view.width+'px';canvas.style.height=view.height+'px';await page.render({canvasContext:canvas.getContext('2d'),viewport:view,transform:[ratio,0,0,ratio,0,0]}).promise;if(!valid(token))break;$('pdfPage').value=pageNumber;$('pdfPrev').disabled=pageNumber<=1;$('pdfNext').disabled=pageNumber>=pdf.numPages;$('pdfHighlights').innerHTML='';if(!converted){for(const pos of source.positions||source.position_int||[]){if(Number(pos[0])!==pageNumber||pos.length<5)continue;const box=document.createElement('span');box.style.cssText=`left:${pos[1]*view.scale}px;top:${pos[3]*view.scale}px;width:${Math.max(1,pos[2]-pos[1])*view.scale}px;height:${Math.max(1,pos[4]-pos[3])*view.scale}px`;$('pdfHighlights').append(box);}}
 }while(queued);drawing=false;}
 const render=()=>draw().catch(e=>failure(e,token));
 $('pdfPrev').onclick=()=>{pageNumber=Math.max(1,pageNumber-1);render();};$('pdfNext').onclick=()=>{pageNumber=Math.min(pdf.numPages,pageNumber+1);render();};$('pdfPage').onchange=e=>{pageNumber=Math.max(1,Math.min(pdf.numPages,Math.floor(Number(e.target.value)||1)));render();};$('pdfMinus').onclick=()=>{scale=Math.max(.5,scale-.2);render();};$('pdfPlus').onclick=()=>{scale=Math.min(3,scale+.2);render();};
 $('viewerStatus').textContent=converted?(matched?`已按引用文字定位预览第 ${pageNumber} 页（Office 转换版式）`:'Office 文字预览（重新排版）'+(source.content?'；未匹配到可靠页码，请用“解析内容”查看对应证据。':'')):(source.page?`已定位来源第 ${pageNumber} 页，黄色区域为引用位置`:'PDF 原文件');
 await draw();
 if(valid(token))$('viewerPdfControls').hidden=false;
}
async function showParsed(){
 if(!current)return;const {token,signal,source}=begin('parsed');
 $('viewerStatus').textContent='正在加载解析内容…';
 try{
 let kb=source.kbId;if(Array.isArray(kb))kb=kb[0];
 const evidence=source.content||source.content_with_weight||'';
 $('viewerBody').innerHTML=evidence?`<article class="viewer-chunk evidence-target"><h3>当前来源证据${source.page?' · 第 '+source.page+' 页':''}</h3><pre>${esc(evidence)}</pre></article>`:'';
 if(!kb){$('viewerStatus').textContent='来源未提供知识库 ID，仅显示已返回的证据。';if(!evidence)$('viewerBody').innerHTML=empty('该格式暂无可显示的解析内容，可下载原文件查看');return;}
 let p=1,total=0,loaded=0;
 const loadMore=async()=>{
 const r=await api('GET',`/api/v1/datasets/${encodeURIComponent(kb)}/documents/${encodeURIComponent(source.docId)}/chunks?page=${p}&page_size=50`,undefined,signal);
 if(!valid(token))return;const chunks=arrayOf(r.data);total=r.data?.total||0;p++;loaded+=chunks.length;
 $('viewerBody').querySelector('.viewer-more')?.remove();
 for(const chunk of chunks){const item=document.createElement('article');item.className='viewer-chunk';const page=sourcePage(chunk);item.innerHTML=`<h3>${page?'第 '+page+' 页':'文档切片'}${chunk.id===source.id&&evidence?' · 对应引用':''}</h3><pre>${esc(chunk.content||chunk.content_with_weight||'')}</pre>`;$('viewerBody').append(item);}
 $('viewerStatus').textContent=`解析内容：已加载 ${loaded} / ${total} 个切片${evidence?' · 当前引用已置顶':''}。无原始页码的格式按切片定位。`;
 if(!loaded&&!evidence)$('viewerBody').innerHTML=empty('尚无解析内容，请先在知识库解析文件');
 if(loaded<total&&chunks.length){const more=document.createElement('button');more.className='btn subtle viewer-more';more.textContent='加载更多内容';more.onclick=async()=>{more.disabled=true;try{await loadMore();}catch(e){failure(e,token);}};$('viewerBody').append(more);}
 };await loadMore();
 }catch(e){failure(e,token);}
}

// Event delegation survives streaming answer re-renders.
document.addEventListener('click',e=>{const b=e.target.closest('[data-file-source]');if(!b)return;e.preventDefault();try{openFile(JSON.parse(b.dataset.fileSource)).catch(err=>{ $('viewerStatus').textContent=err.message; });}catch(err){console.error('文件来源无效',err);}});
