import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';
import {loadReviewGraph} from './helpers/review_graph.mjs';

const source=await readFile(new URL('../frontend/review-jobs.js',import.meta.url),'utf8');
const {workflowAcademicSvg,workflowViewModel}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const fixture=loadReviewGraph();
const context={structuredClone:globalThis.structuredClone};
vm.runInNewContext(await readFile(new URL('../frontend/vendor/dagre/dagre.min.js',import.meta.url),'utf8'),context,{timeout:3000});
const layoutEngine=context.dagre.default||context.dagre;
const decodeXml=value=>String(value).replace(/&(?:lt|gt|quot|apos|amp);/g,entity=>({'&lt;':'<','&gt;':'>','&quot;':'"','&apos;':"'",'&amp;':'&'}[entity]));
const attributes=tag=>Object.fromEntries([...tag.matchAll(/([\w:-]+)="([^"]*)"/g)].map(([,key,value])=>[key,decodeXml(value)]));
const elements=(svg,attribute)=>[...svg.matchAll(/<g\b[^>]*>/g)].map(match=>attributes(match[0])).filter(value=>attribute in value);

function metadata(svg){
  const match=svg.match(/<metadata\b[^>]*id="workflow-academic-map"[^>]*>([\s\S]*?)<\/metadata>/);
  assert.ok(match,'The academic figure must retain a machine-readable correspondence map.');
  return JSON.parse(decodeXml(match[1]));
}
let cached;
function actual(){
  cached??=(async()=>{const input=structuredClone(fixture),before=structuredClone(input);const svg=await workflowAcademicSvg(input,{layoutEngine});return {svg,map:metadata(svg),input,before};})();
  return cached;
}
const edgeKey=edge=>JSON.stringify([edge.source,edge.target,Boolean(edge.conditional)]);

test('academic workflow corresponds to every actual node and directed compiled connection',async()=>{
  const {svg,map}=await actual(),model=workflowViewModel(fixture);
  assert.equal(fixture.nodes.length,31);assert.equal(fixture.edges.length,59);
  assert.equal(map.nodes.length,model.nodes.length);
  assert.equal(map.edges.length,model.edges.length);
  assert.deepEqual(map.nodes.map(({id,code,label})=>({id,code,label})),model.nodes.map(({id,code,label})=>({id,code,label})));
  assert.deepEqual(map.edges.map(edgeKey).sort(),model.edges.map(edgeKey).sort());
  assert.deepEqual(elements(svg,'data-node-id').map(node=>[node['data-node-id'],node['data-node-code']]).sort(),model.nodes.map(node=>[node.id,node.code]).sort());
  assert.equal(elements(svg,'data-edge-id').length,model.edges.length);
  assert.equal(new Set(elements(svg,'data-edge-id').map(edge=>edge['data-edge-id'])).size,model.edges.length);
  assert.deepEqual(elements(svg,'data-edge-id').map(edge=>JSON.stringify([edge['data-source'],edge['data-target']])).sort(),model.edges.map(edge=>JSON.stringify([edge.source,edge.target])).sort());
});

test('academic layout does not mutate task data or its original correspondence numbers',async()=>{
  const {input,before}=await actual();
  assert.deepEqual(input,before);
  const model=workflowViewModel(input);
  assert.equal(model.nodes.find(node=>node.id==='await_confirmation').code,'N03');
  assert.equal(model.nodes.find(node=>node.id==='report_workflow:finalize_report').code,'N31');
});

test('all academic node rectangles fit the canvas and do not overlap one another',async()=>{
  const {svg,map}=await actual(),viewBox=attributes(svg.match(/<svg\b[^>]*>/)[0]).viewBox.split(/\s+/).map(Number);
  assert.equal(viewBox.length,4);
  assert.ok(viewBox.every(Number.isFinite));
  const [left,top,width,height]=viewBox;
  assert.ok(width>0&&height>0);
  for(const node of map.nodes){
    assert.ok(['x','y','width','height'].every(key=>Number.isFinite(node[key])),node.id+' has finite geometry');
    assert.ok(node.width>0&&node.height>0,node.id+' has positive dimensions');
    assert.ok(node.x-node.width/2>=left-.5,node.id+' is not cropped at left');
    assert.ok(node.y-node.height/2>=top-.5,node.id+' is not cropped at top');
    assert.ok(node.x+node.width/2<=left+width+.5,node.id+' is not cropped at right');
    assert.ok(node.y+node.height/2<=top+height+.5,node.id+' is not cropped at bottom');
  }
  for(let first=0;first<map.nodes.length;first++)for(let second=first+1;second<map.nodes.length;second++){
    const a=map.nodes[first],b=map.nodes[second];
    const overlapX=(a.width+b.width)/2-Math.abs(a.x-b.x),overlapY=(a.height+b.height)/2-Math.abs(a.y-b.y);
    assert.ok(overlapX<=.5||overlapY<=.5,`${a.id} and ${b.id} overlap`);
  }
});

test('the workflow figure is compact, readable and continuous across neighbouring stages',async()=>{
  const {svg,map}=await actual(),model=workflowViewModel(fixture);
  assert.ok(map.width<=1600&&map.height<=2500,'The complete figure should fit a compact portrait composition.');
  assert.ok(svg.includes('presentation')&&svg.includes('compact-research-v2'));
  const bounds=model.stages.map(stage=>{const nodes=map.nodes.filter(node=>node.stage===stage.id);return {top:Math.min(...nodes.map(node=>node.y-node.height/2)),bottom:Math.max(...nodes.map(node=>node.y+node.height/2))};});
  for(let index=1;index<bounds.length;index++)assert.ok(bounds[index].top-bounds[index-1].bottom<=40,'Neighbouring stages should not be separated by large empty bands.');
  const visiblePaths=[...svg.matchAll(/<g\b[^>]*data-edge-id="[^"]+"[^>]*>([\s\S]*?)<\/g>/g)].map(match=>[...match[1].matchAll(/<path\b[^>]*>/g)].at(-1)[0]);
  assert.equal(visiblePaths.length,59);
  for(const path of visiblePaths)assert.ok(Number(attributes(path)['stroke-width'])>=2.2,'Every actual connection has a clearly visible line weight.');
  assert.ok(svg.includes('font-size="18"'),'Main labels use a readable scientific diagram font size.');
});

test('the exported button and download use the requested plain workflow name',async()=>{
  const app=await readFile(new URL('../frontend/app.js',import.meta.url),'utf8');
  assert.ok(app.includes("action('导出流程图'"));
  assert.ok(app.includes("download('快速审查流程图.svg'"));
  assert.ok(!app.includes("action('导出学术流程图'"));
});

test('academic diagram preserves conditional declarations and all output-retry self loops',async()=>{
  const {svg,map}=await actual();
  assert.equal(map.edges.filter(edge=>edge.conditional).length,46);
  assert.deepEqual(map.edges.filter(edge=>edge.source===edge.target).map(edge=>edge.source).sort(),[
    'check_requirements:judge_functional','check_requirements:judge_numeric','report_workflow:polish_one'
  ]);
  assert.ok(svg.includes('条件'),'Conditional paths have an explicit legend.');
  assert.ok(svg.includes('状态'),'The legend explains state-selected paths.');
  for(const node of ['await_confirmation','await_retry'])assert.ok(map.nodes.some(item=>item.id===node));
});

test('academic structure export is neutral and independent of a task execution trace',async()=>{
  const neutral=structuredClone(fixture),executed=structuredClone(fixture);
  executed.trace=executed.nodes.map(node=>({node:node.id.replaceAll(':','/'),status:'completed',message:'PRIVATE_TASK_MESSAGE_SENTINEL'}));
  executed.task_id='PRIVATE_TASK_ID_SENTINEL';executed.job={message:'PRIVATE_JOB_MESSAGE_SENTINEL',status:'completed'};
  const first=await workflowAcademicSvg(neutral,{layoutEngine}),second=await workflowAcademicSvg(executed,{layoutEngine});
  assert.equal(first,second,'An academic structure diagram must not imply execution based on node colour.');
  assert.ok(!second.includes('PRIVATE_'));
});

test('labels and identifiers cannot inject SVG markup or external dependencies',async()=>{
  const id='user-" onload="bad<node>',label='</metadata><script>alert(1)</script><image href="https://invalid.example/secret">';
  const graph={nodes:[{id,label},{id:'other',label:'人工确认 & <核对>'}],edges:[{source:id,target:'other',conditional:true}]};
  const svg=await workflowAcademicSvg(graph,{layoutEngine}),map=metadata(svg);
  assert.equal(map.nodes.find(node=>node.id===id).label,label);
  assert.ok(!/<(?:script|foreignObject|image)\b/i.test(svg));
  assert.ok(![...svg.matchAll(/<(?:svg|g|path|rect|ellipse|text|title|metadata)\b[^>]*>/g)].some(match=>Object.hasOwn(attributes(match[0]),'onload')));
  assert.ok(!/\bhref="https?:/i.test(svg));
  assert.ok(/xmlns="http:\/\/www\.w3\.org\/2000\/svg"/.test(svg));
  assert.ok(svg.includes('&lt;script&gt;'));
});

test('unfamiliar nodes and parallel edges retain their identity and multiplicity',async()=>{
  const graph={nodes:[{id:'extension-a',label:'扩展入口'},{id:'extension-b',label:'扩展处理'}],edges:[
    {source:'extension-a',target:'extension-b',conditional:false},
    {source:'extension-a',target:'extension-b',conditional:true,label:'按状态'},
    {source:'extension-b',target:'extension-b',conditional:true},
    {source:'extension-a',target:'missing',conditional:true}
  ]};
  const svg=await workflowAcademicSvg(graph,{layoutEngine}),map=metadata(svg);
  assert.deepEqual(map.nodes.map(node=>[node.id,node.code,node.stage]),[['extension-a','N01','other'],['extension-b','N02','other']]);
  assert.equal(map.edges.length,3);
  assert.equal(map.edges.filter(edge=>edge.source==='extension-a'&&edge.target==='extension-b').length,2);
  assert.equal(new Set(map.edges.map(edge=>edge.id)).size,3);
  assert.ok(!elements(svg,'data-node-id').some(node=>node['data-node-id']==='missing'));
});
