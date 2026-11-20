import {drawingBox,drawingLocation,drawingReviewStatuses,esc} from './ui.js';

export const drawingFile=name=>/\.(pdf|png|jpe?g)$/i.test(name);
export const drawingPageSize=20;
const drawingFields=[
 ['drawing_number','图号'],['revision','版次'],['view','视图'],
 ['object','对象'],['annotation','标注'],['meaning','含义'],
];
const drawingBoxes=['annotation_box','object_box'];

export function drawingEvidencePath(datasetId,documentId,evidenceId){
 const path=`/api/v1/datasets/${encodeURIComponent(datasetId)}/documents/${encodeURIComponent(documentId)}/drawing-evidence`;
 return evidenceId?`${path}/${encodeURIComponent(evidenceId)}`:path;
}

export function drawingEvidenceSettings(models,selected='',method){
 const options=models.filter(model=>([model.model_type].flat().includes('vision')||
   ([model.model_type].flat().includes('chat')&&(model.id||model.model_id)===selected))&&model.supports_images!==false)
   .map(model=>({id:model.id||model.model_id,label:[model.name,model.instance_name].filter(Boolean).join(' · ')}))
   .filter(model=>model.id);
 const unavailable=selected&&!options.some(model=>model.id===selected);
 if(unavailable)options.push({id:selected,label:'当前接入不可用'});
 const recognition=method==='ocr_vision'?'OCR 辅助视觉':'直接视觉';
 return `<label for="drawingVisionModel">图纸识别模型 · ${recognition}</label><select id="drawingVisionModel" name="drawing_vision_model" aria-describedby="drawingVisionHelp">
 <option value="">关闭图纸识别</option>${options.map(model=>`<option value="${esc(model.id)}"${model.id===selected?' selected':''}>${esc(model.label)}</option>`).join('')}</select>
 <p id="drawingVisionHelp" class="hint">在<a href="#models">模型设置</a>中以「视觉 / 图纸识别」用途添加 API 接入，再在这里选择。启用后，解析 PNG、JPG、JPEG 和符合识别条件的 PDF 页时，会将图像发送至所选模型 API。${method==='ocr_vision'?' 请求还会附上识别到的文字。':''}</p>`;
}

export function drawingOverlayHtml(row,page){
 if(!row||row.page_number!==page)return '';
 return [['annotation','标注',row.annotation_box],['object','对象',row.object_box]].map(([kind,label,coords])=>{
  const box=drawingBox(coords);
  if(!box)return '';
  const [x0,y0,x1,y1]=box;
  const percent=n=>Number((n*100).toFixed(6))+'%';
  return `<div class="drawing-box drawing-box-${kind}" style="top:${percent(y0)};left:${percent(x0)};width:${percent(x1-x0)};height:${percent(y1-y0)}"><b>${label}</b></div>`;
 }).join('');
}

export function drawingBoxFromDrag(frame,start,end){
 if(frame.width<=0||frame.height<=0)return null;
 const coordinate=(value,offset,size)=>Math.max(0,Math.min(1,(value-offset)/size));
 const xs=[start.x,end.x].map(value=>coordinate(value,frame.left,frame.width));
 const ys=[start.y,end.y].map(value=>coordinate(value,frame.top,frame.height));
 if(Math.abs(xs[1]-xs[0])*frame.width<3||Math.abs(ys[1]-ys[0])*frame.height<3)return null;
 return [Math.min(...xs),Math.min(...ys),Math.max(...xs),Math.max(...ys)].map(value=>Number(value.toFixed(6)));
}

export function sameDrawingVersion(snapshot,current){
 return ['id','version','status','method','page_number',...drawingFields.map(([field])=>field)].every(key=>(snapshot[key]??'')===(current[key]??''))
  &&drawingBoxes.every(key=>JSON.stringify(drawingBox(snapshot[key]))===JSON.stringify(drawingBox(current[key])));
}

export function drawingSnapshotHtml(snapshot,current,message=''){
 return `<section class="drawing-snapshot"><h3>引用中的图纸证据</h3>
 <p class="hint">引用版本 ${esc(snapshot.version||'未记录')} · ${esc(drawingLocation(snapshot))} · ${esc(drawingReviewStatuses[snapshot.status]||'状态未记录')}</p>
 <dl>${drawingFields.map(([key,label])=>`<div><dt>${label}</dt><dd>${esc(snapshot[key]||'未记录')}</dd></div>`).join('')}</dl>
 <p class="hint">${esc(message||'本页展示引用保存的内容与位置。')}</p>
 ${current?`<button type="button" class="btn subtle small" data-current-drawing>打开当前版本 ${esc(current.version)}</button>`:''}</section>`;
}

export function drawingTableHtml(rows){
 const cell=(primary,secondary)=>`<td>${esc(primary||'未记录')}${secondary?`<small>${esc(secondary)}</small>`:''}</td>`;
 return `<div class="table-scroll"><table class="drawing-table" aria-label="图纸证据">
 <thead><tr><th scope="col">图号和版次</th><th scope="col">视图与对象</th><th scope="col">标注与含义</th><th scope="col">原图定位</th><th scope="col">核对状态</th></tr></thead>
 <tbody>${rows.map((row,index)=>`<tr>
 ${cell(row.drawing_number,row.revision&&'版次 '+row.revision)}
 ${cell(row.view,row.object)}${cell(row.annotation,row.meaning)}
 <td><button type="button" class="btn subtle small" data-locate-drawing="${index}">打开并核对</button><small>${esc(drawingLocation(row))}</small></td>
 <td><span class="badge ${row.status==='confirmed'?'done':row.status==='rejected'?'failed':''}">${esc(drawingReviewStatuses[row.status]||'未记录')}</span></td>
 </tr>`).join('')}</tbody></table></div>`;
}

export function drawingReviewFormHtml(row){
 return `<form class="drawing-review-form">
 <h3>核对图纸证据</h3>
 <p class="hint">当前版本 ${esc(row.version)} · ${row.method==='ocr_vision'?'OCR 辅助视觉':'直接视觉'}</p>
 <p class="hint" role="status" data-drawing-feedback></p>
 <div class="drawing-review-grid">${drawingFields.map(([field,label])=>`<label>${label}<input name="${field}" value="${esc(row[field])}"></label>`).join('')}</div>
 <fieldset class="drawing-mark-controls">
 <legend>原图位置</legend>
 <div class="row wrap"><button class="btn subtle small" type="button" data-mark-box="annotation_box" aria-pressed="false">重画标注框</button><button class="btn subtle small" type="button" data-mark-box="object_box" aria-pressed="false">重画对象框</button></div>
 <p class="hint" role="status" data-mark-status></p>
 </fieldset>
 <label>核对状态<select name="status">${Object.entries(drawingReviewStatuses).map(([value,label])=>`<option value="${value}"${value===row.status?' selected':''}>${label}</option>`).join('')}</select></label>
 <button class="btn primary small" type="submit">保存核对</button>
 </form>`;
}

export function drawingReviewPatch(row,fields){
 const status=fields.get('status'),annotation_box=drawingBox(row.annotation_box),object_box=drawingBox(row.object_box);
 if(status==='confirmed'&&(!annotation_box||!object_box))throw Error('请在原图上框选标注框和对象框，再确认。');
 return {version:row.version,status,annotation_box,object_box,...Object.fromEntries(drawingFields.map(([name])=>[name,fields.get(name)]))};
}
