import assert from 'node:assert/strict';
import test from 'node:test';
import {uploadSkipReason} from '../frontend/upload-filter.js';

globalThis.document = {getElementById: () => ({})};

const evidence = {
  id:'e1',version:2,status:'pending',method:'vision',
  drawing_number:'J-001',revision:'B',view:'前视图',object:'左侧孔',
  annotation:'⌀12',meaning:'孔径 12 mm',page_number:3,
  annotation_box:[0.15,0.2,0.35,0.4],object_box:[0.4,0.5,0.6,0.65],
};
const source = {
  document_name:'图纸.pdf',document_id:'doc/1',dataset_id:'kb?1',
  content:'尺寸候选',positions:[[1,0,0,1,1]],drawing_evidence:evidence,
};

test('drawing image formats pass both file and folder upload filters', () => {
  for (const name of ['结构图.png','尺寸图.JPG','设备图.jpeg']) {
    assert.equal(uploadSkipReason({name,size:4}),'');
    assert.equal(uploadSkipReason({name,size:4,webkitRelativePath:'图纸/'+name}),'');
  }
  assert.match(uploadSkipReason({name:'图纸.gif',size:4}),/本系统暂不支持/);
});

test('Office drawing citations identify embedded images without inventing Word pages', async () => {
  const {drawingFile} = await import('../frontend/drawing-evidence.js');
  const {drawingLocation} = await import('../frontend/ui.js');
  for (const name of ['report.docx','diagram.pptx','spec.xlsx']) assert.equal(drawingFile(name),true);
  const location=drawingLocation({...evidence,source_part:'word/media/image3.png'});
  assert.match(location,/内嵌图片 3/);
  assert.doesNotMatch(location,/第 3 页/);
  assert.match(drawingLocation({...evidence,source_part:'ppt/media/image3.png',slide_number:2}),/第 2 张幻灯片/);
});

test('citation carries both coordinates and exact evidence ID into the shared original viewer', async () => {
  const {sourcesHtml} = await import('../frontend/ui.js');
  for (const compact of [false,true]) {
    const html = sourcesHtml([source],'尺寸',{compact});
    assert.match(html,/定位图纸证据/);
    const payload = html.match(/data-file-source="([^"]+)"/)[1].replaceAll('&quot;','"');
    assert.deepEqual(JSON.parse(payload).drawing_evidence,evidence);
  }
});

test('drawing overlay uses normalized dimensions on the evidence page only', async () => {
  const {drawingOverlayHtml} = await import('../frontend/drawing-evidence.js');
  const markup = drawingOverlayHtml(evidence,3);
  assert.match(markup,/top:20%;left:15%;width:20%;height:20%/);
  assert.match(markup,/>标注</);
  assert.match(markup,/>对象</);
  assert.equal(drawingOverlayHtml(evidence,2),'');
  assert.equal(drawingOverlayHtml({...evidence,annotation_box:[0,-0.1,1,1],object_box:['0',0,1,1]},3),'');
  assert.equal(drawingOverlayHtml({...evidence,annotation_box:null,object_box:[0.5,0.3,0.5,0.4]},3),'');
});

test('isolated evidence requests encode each scoped path segment', async () => {
  const {drawingEvidencePath} = await import('../frontend/drawing-evidence.js');
  assert.equal(drawingEvidencePath('kb?1','doc/1','e#1'),
    '/api/v1/datasets/kb%3F1/documents/doc%2F1/drawing-evidence/e%231');
});

test('drawing configuration offers dedicated visual models and preserves an existing chat selection', async () => {
  const {drawingEvidenceSettings} = await import('../frontend/drawing-evidence.js');
  const models=[
    {id:'vision-42',name:'视觉模型',instance_name:'本地测试',model_type:'vision'},
    {id:'old-drawing',name:'旧版图纸模型',model_type:'chat'},
    {id:'text-only',name:'文本模型',model_type:'chat'},
    {id:'embedding-42',name:'嵌入模型',model_type:'embedding'},
  ];
  const settings=drawingEvidenceSettings(models,'vision-42','ocr_vision');
  assert.match(settings,/<option value="">关闭图纸识别<\/option>/);
  assert.match(settings,/<option value="vision-42" selected>视觉模型 · 本地测试<\/option>/);
  assert.doesNotMatch(settings,/text-only|embedding-42|old-drawing/);
  assert.match(settings,/图像发送至所选模型 API/);
  assert.match(settings,/OCR 辅助视觉/);
  assert.match(settings,/模型设置/);
  const legacy=drawingEvidenceSettings(models,'old-drawing','vision');
  assert.match(legacy,/<option value="old-drawing" selected>/);
  assert.doesNotMatch(legacy,/text-only|embedding-42/);
});

test('the drawing table and review form display uncertainty and save all editable fields and version', async () => {
  const {drawingReviewFormHtml,drawingReviewPatch,drawingTableHtml} = await import('../frontend/drawing-evidence.js');
  const row={...evidence,object:'<孔>',annotation_box:null,object_box:null};
  const table=drawingTableHtml([row]);
  assert.match(table,/图号和版次.*视图与对象.*标注与含义.*原图定位.*核对状态/);
  assert.match(table,/未记录框选位置/);
  assert.match(table,/&lt;孔&gt;/);
  const form=drawingReviewFormHtml(row);
  assert.match(form,/<option value="pending" selected>待核对<\/option>/);
  assert.match(form,/<option value="confirmed">已确认<\/option>/);
  assert.match(form,/<option value="rejected">已驳回<\/option>/);
  assert.match(form,/data-mark-box="annotation_box"[^>]*>重画标注框/);
  assert.match(form,/data-mark-box="object_box"[^>]*>重画对象框/);
  const corrections={status:'confirmed',drawing_number:'J-002',revision:'C',view:'截面',object:'右侧孔',annotation:'⌀15',meaning:'孔径 15 mm'};
  const fields=new Map(Object.entries(corrections));
  assert.throws(()=>drawingReviewPatch(row,fields),/标注框和对象框/);
  assert.deepEqual(drawingReviewPatch({...row,status:'rejected'},new Map(fields).set('status','rejected')),
    {version:2,...corrections,status:'rejected',annotation_box:null,object_box:null});
  assert.deepEqual(drawingReviewPatch(evidence,fields),
    {version:2,...corrections,annotation_box:evidence.annotation_box,object_box:evidence.object_box});
});

test('a box dragged in reverse is normalized to the whole page with edges clamped', async () => {
  const {drawingBoxFromDrag}=await import('../frontend/drawing-evidence.js');
  const frame={left:100,top:200,width:500,height:250};
  assert.deepEqual(drawingBoxFromDrag(frame,{x:450,y:370},{x:90,y:275}),[0,.3,.7,.68]);
  assert.deepEqual(drawingBoxFromDrag(frame,{x:105,y:200},{x:610,y:500}),[.01,0,1,1]);
  assert.equal(drawingBoxFromDrag(frame,{x:100,y:200},{x:101,y:290}),null);
});

test('only a matching current version can be edited directly from a citation', async () => {
  const {sameDrawingVersion,drawingSnapshotHtml}=await import('../frontend/drawing-evidence.js');
  const current={...evidence,version:3,object:'右侧孔',annotation_box:[.5,.2,.8,.4]};
  assert.equal(sameDrawingVersion(evidence,structuredClone(evidence)),true);
  assert.equal(sameDrawingVersion(evidence,current),false);
  assert.equal(sameDrawingVersion(evidence,{...current,version:2}),false);
  const historical=drawingSnapshotHtml(evidence,current);
  assert.match(historical,/引用版本 2/);
  assert.match(historical,/左侧孔/);
  assert.match(historical,/打开当前版本/);
  assert.doesNotMatch(historical,/右侧孔/);
  assert.doesNotMatch(drawingSnapshotHtml(evidence,null,'当前记录已过期'),/打开当前版本/);
});

test('offline report records drawing location and review status', async () => {
  const {buildReportHtml} = await import('../frontend/report-export.js');
  const report = {
    title:'合成报告',
    requirements:[{id:'R001',type:'numeric',title:'孔径',criterion:'孔径应 12 mm'}],
    results:[{requirementId:'R001',status:'待人工复核',evidence:[{source_id:'E1',quote:'尺寸候选',source}]}],
  };
  const html = buildReportHtml(report);
  assert.match(html,/图纸标注 · 第 3 页 · 标注、对象已定位/);
  assert.match(html,/待核对/);
  assert.match(html,/&quot;drawing_evidence&quot;: \{/);
  assert.match(html,/&quot;annotation_box&quot;: \[\s+0\.15,/);
});
