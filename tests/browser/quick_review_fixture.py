"""Exercise the actual quick-review UI against an isolated synthetic LangGraph app.

No daily workspace, real API key or external model is used. The local fake model
verifies orchestration and source plumbing, not review accuracy.
"""
from pathlib import Path
import asyncio
import html
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "runtime-logs/browser-acceptance/quick-review"
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from fastapi import Request
from fastapi.responses import HTMLResponse, Response
from backend.config import Settings
from backend.main import create_app
from backend.db import uid, now
from backend.security import hash_password
import fake_model_provider as fixture
import uvicorn


JS = r'''
const originalFetch=window.fetch.bind(window),calls=[];
window.fetch=(input,options)=>{calls.push({url:typeof input==='string'?input:input.url,method:options?.method||'GET'});return originalFetch(input,options);};
const result=JSON.parse(sessionStorage.getItem('graph-probe-result')||'{"errors":[]}');
window.addEventListener('error',event=>result.errors.push(event.message));
window.addEventListener('unhandledrejection',event=>result.errors.push(String(event.reason)));
const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const until=async(test,label,timeout=25000)=>{const deadline=performance.now()+timeout;while(!await test()){if(performance.now()>deadline)throw Error('Timed out: '+label);await wait(70);}};
const jobId=()=>JSON.parse(localStorage.getItem('lift-review-job')||'{}').id;
const getJob=async(id=jobId())=>{const response=await fetch('/api/v1/review-jobs/'+id);if(!response.ok)throw Error('Task GET '+response.status);return response.json();};
const getModal=()=>document.getElementById('modal');
const confirmationOpen=()=>getModal().open&&!!document.getElementById('requirementEditor');
async function toolbarLayout(){
 const root=document.documentElement,old=root.style.getPropertyValue('--scale');let okay=true;
 for(const scale of ['.9','1','1.1','1.25']){root.style.setProperty('--scale',scale);await wait(50);const row=document.querySelector('.report-panel>.panel-heading>.row');okay&&=row.scrollWidth<=row.clientWidth+2;for(const button of row.querySelectorAll('button')){const range=document.createRange();range.selectNodeContents(button);const outer=button.getBoundingClientRect();okay&&=[...range.getClientRects()].every(rect=>rect.left>=outer.left+1&&rect.right<=outer.right-1);}}
 root.style.setProperty('--scale',old);return okay;
}
async function confirmAndFinish(id,excludeFirst=false){
 await until(confirmationOpen,'confirmation modal');
 const title=document.querySelector('[name="title_'+(excludeFirst?1:0)+'"]');title.value=excludeFirst?'离线缓存功能（人工核对）':'设备续航时间（人工核对）';title.dispatchEvent(new Event('input',{bubbles:true}));
 if(excludeFirst){const include=document.querySelector('[name="include"][value="0"]');include.checked=false;include.dispatchEvent(new Event('change',{bubbles:true}));document.getElementById('addRequirement').click();await wait(60);const category=document.querySelector('[name="category_3"]');category.value='数值指标';category.dispatchEvent(new Event('change',{bubbles:true}));for(const [name,value] of [['title_3','人工补充续航条件'],['criterion_3','设备续航时间至少8小时。']]){const field=document.querySelector('[name="'+name+'"]');field.value=value;field.dispatchEvent(new Event('input',{bubbles:true}));}}
 document.getElementById('modalSubmit').click();await until(()=>!getModal().open,'confirmation accepted');
 let job;await until(async()=>{job=await getJob(id);return ['completed','failed','stale'].includes(job.status);},'graph completion',35000);
 if(job.status!=='completed')throw Error('Graph did not finish: '+job.status+' '+job.message);
 await until(()=>document.getElementById('stopReview').hidden&&!document.getElementById('startReview').disabled,'completed UI');
 return job;
}
try{
 await fetch('/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:'graph-browser',password:'synthetic-only-password'})});
 await import('/app.js');
 await until(()=>document.getElementById('quickModel').options.length>1&&document.querySelector('#quickKbList [data-kb]'),'app initialization');
 document.documentElement.dataset.theme='light';
 if(fixtureData.narrow){
  result.report_toolbar_unclipped=await toolbarLayout();document.documentElement.style.setProperty('--scale','1.25');
  document.getElementById('viewReviewWorkflow').click();await until(()=>getModal().open&&document.querySelector('.workflow-inspector .wf-stage-flow'),'narrow graph viewer');
  result.narrow_graph_container_fits=getModal().scrollWidth<=getModal().clientWidth+2;
  result.success=result.report_toolbar_unclipped&&result.narrow_graph_container_fits&&result.errors.length===0;
 }else if(sessionStorage.getItem('graph-probe-phase')!=='reloaded'){
  document.getElementById('chooseBaseline').click();await until(()=>document.querySelector('[name="baseline"]'),'baseline picker');
  document.querySelector('[name="baseline"][value="'+fixtureData.document.id+'"]').checked=true;
  document.getElementById('modalSubmit').click();await until(()=>!getModal().open,'baseline selected');
  document.getElementById('startReview').click();await until(confirmationOpen,'initial confirmation');
  const first=await getJob();result.first_task_created=Boolean(first.id)&&first.report.workflowJobId===first.id;
  result.no_review_before_confirmation=first.status==='waiting_confirmation'&&first.report.results.length===0;
  result.three_requirement_types=['numeric','functional','document'].every(type=>first.report.requirements.some(row=>row.type===type));
  document.getElementById('modalCancel').click();await wait(180);
  result.cancel_keeps_waiting=(await getJob()).status==='waiting_confirmation'&&!getModal().open;
  sessionStorage.setItem('graph-first-id',first.id);sessionStorage.setItem('graph-probe-phase','reloaded');sessionStorage.setItem('graph-probe-result',JSON.stringify(result));
  location.reload();
 }else{
  const id=sessionStorage.getItem('graph-first-id');await until(confirmationOpen,'reload reconnects confirmation');
  result.reload_reconnects_same_task=jobId()===id;
  const expected=(await getJob(id)).report.requirements[1].quote;
  const first=await confirmAndFinish(id,true);
  result.first_graph_completed=first.report.results.length===3&&first.report.complete===true;
  result.human_edit_saved=first.report.requirements[0].title==='离线缓存功能（人工核对）'&&first.report.requirements[0].userEdited===true;
  result.excluded_first_preserves_other_sources=first.report.requirements[0].quote===expected&&first.report.requirements[0].quote.includes('离线缓存')&&first.report.requirements.at(-1).userAdded===true;
  result.results_unique=new Set(first.report.results.map(row=>row.requirementId)).size===first.report.results.length;
  const firstGraph=await(await fetch('/api/v1/review-jobs/'+id+'/graph')).json();
  result.actual_three_branches_executed=['document_check','judge_numeric','judge_functional'].every(name=>firstGraph.trace.some(event=>String(event.node).includes(name)));
  // Slow only the next synthetic extraction so an actual user stop is observable.
  await fetch('/__probe/slow',{method:'POST'});document.getElementById('startReview').click();
  await until(()=>jobId()&&jobId()!==id&&!document.getElementById('stopReview').hidden,'second running task');
  const secondId=jobId();document.getElementById('stopReview').click();
  let stopped;await until(async()=>{stopped=await getJob(secondId);return stopped.status==='stopped';},'explicit stop',18000);
  result.explicit_stop_saved=stopped.can_resume===true&&calls.some(call=>call.url.endsWith('/'+secondId+'/stop')&&call.method==='POST');
  await until(()=>!document.getElementById('resumeReview').hidden&&!document.getElementById('resumeReview').disabled,'resume button');
  document.getElementById('resumeReview').click();
  const second=await confirmAndFinish(secondId);
  result.explicit_resume_completed=second.report.results.length===3&&calls.some(call=>call.url.endsWith('/'+secondId+'/resume')&&call.method==='POST');
  const blobs=new Map(),makeUrl=URL.createObjectURL.bind(URL),anchorClick=HTMLAnchorElement.prototype.click;const downloads=[];
  URL.createObjectURL=blob=>{const url=makeUrl(blob);blobs.set(url,blob);return url;};
  HTMLAnchorElement.prototype.click=function(){if(/\.(?:html|svg)$/.test(this.download)){downloads.push({name:this.download,blob:blobs.get(this.href)});return;}return anchorClick.call(this);};
  document.getElementById('viewReviewWorkflow').click();await until(()=>getModal().open&&document.querySelector('.workflow-inspector .wf-stage-flow'),'actual workflow viewer');
  result.actual_graph_visible=!!document.querySelector('.wf-timeline li');
  [...document.querySelectorAll('.modal-actions [data-modal-extra]')].find(button=>button.textContent==='导出完整结构').click();await until(()=>downloads.some(item=>item.name.endsWith('.svg')),'SVG download');
  document.getElementById('modalCancel').click();document.getElementById('exportReport').click();await until(()=>downloads.some(item=>item.name.endsWith('.html')),'HTML report download');
  const svgDownload=downloads.find(item=>item.name.endsWith('.svg')),htmlDownload=downloads.find(item=>item.name.endsWith('.html'));
  const svg=await svgDownload.blob.text(),exported=await htmlDownload.blob.text(),documentExport=new DOMParser().parseFromString(exported,'text/html');
  result.svg_comes_from_actual_graph=svg.includes('<svg')&&svg.includes('data-source=');
  result.html_export_complete=documentExport.querySelectorAll('svg').length===4&&['R001','R002','R003'].every(value=>exported.includes(value))&&!documentExport.querySelector('script,iframe');
  await fetch('/__probe/export',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({html:exported,svg})});
  result.no_browser_model_calls=!calls.some(call=>/chat\/completions|\/extract-text|\/retrieval(?:\?|$)/.test(call.url));
  result.report_toolbar_unclipped=await toolbarLayout();
  result.success=Object.entries(result).filter(([key])=>key!=='errors').every(([,value])=>value===true)&&result.errors.length===0;
  document.getElementById('viewReviewWorkflow').click();await until(()=>getModal().open&&document.querySelector('.workflow-inspector .wf-stage-flow'),'final flow preview');
 }
}catch(error){result.success=false;result.error=String(error.stack||error);result.debug={job:jobId(),modalOpen:getModal()?.open,status:document.getElementById('reviewStatus')?.textContent,toast:document.getElementById('toast')?.textContent};}
if(Object.hasOwn(result,'success')){const pre=document.createElement('pre');pre.id='graph-probe-result';pre.textContent=JSON.stringify(result);document.body.append(pre);}
'''


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for key in ('ADMIN_PASSWORD','ADMIN_USERNAME','LLM_MODEL','LLM_BASE_URL','EMBEDDING_MODEL','EMBEDDING_BASE_URL'):
        os.environ.pop(key, None)
    os.environ['NO_PROXY'] = '127.0.0.1,localhost'
    os.environ['LANGSMITH_TRACING'] = 'false'
    os.environ['LANGCHAIN_TRACING_V2'] = 'false'
    original = fixture.answer_for
    control = {'slow': False}

    def synthetic_answer(messages):
        text = str(messages[-1]['content'])
        if '原文段落：' in text:
            if control['slow']:
                control['slow'] = False
                time.sleep(2)
            answer = json.loads(original(messages))
            rows = fixture.json_after(text,'原文段落：')
            row = next(item for item in rows if '离线缓存' in item['text'])
            answer['technical_indicators'].append({'type':'functional','subject':'离线缓存','title':'离线缓存功能','criterion':'系统须支持离线缓存功能。','rationale':'原文明确要求技术功能','source_ids':[row['id']]})
            return json.dumps(answer, ensure_ascii=False)
        return original(messages)

    fixture.answer_for = synthetic_answer
    with fixture.FakeProvider() as provider, tempfile.TemporaryDirectory(prefix='lift-graph-ui-') as temporary:
        app = create_app(Settings(data_dir=Path(temporary)))
        db, knowledge = app.state.db, app.state.knowledge
        db.execute('INSERT INTO users(id,username,password_hash,created_at) VALUES (?,?,?,?)',(uid(),'graph-browser',hash_password('synthetic-only-password'),now()))
        app.state.models.save({'name':'synthetic-graph-model','model_type':'chat','base_url':provider.base_url,'api_key':'synthetic-test-only','set_default':True})
        dataset = db.json_list('datasets')[0]
        documents = []
        for name, content in [('技术协议.txt','设备续航时间不得低于8小时。\n系统须支持离线缓存功能。\n应提交设备测试报告。'),('项目方案.txt','设备采用双电池，设备续航时间为10小时。系统通过本地缓存队列实现离线缓存，恢复联网后上传。'),('设备测试报告.txt','合成测试报告，仅用于软件验证。')]:
            path = Path(temporary) / 'uploads' / (uid()+'.txt')
            path.write_text(content, encoding='utf-8')
            documents.append(knowledge.add_document(dataset['id'],name,path,path.stat().st_size))
        knowledge.parse([row['id'] for row in documents],dataset['id'])
        deadline = time.monotonic()+12
        while any(row['run']=='RUNNING' for row in db.rows('SELECT run FROM documents')):
            if time.monotonic()>deadline:
                raise RuntimeError('Synthetic parsing timed out')
            time.sleep(.05)
        page = (ROOT / 'frontend/index.html').read_text(encoding='utf-8').replace('<script type="module" src="/app.js"></script>','')
        data = json.dumps({'document':{'id':documents[0]['id'],'name':documents[0]['name']},'narrow':'--narrow' in sys.argv},ensure_ascii=False)
        page = page.replace('</body>','<img src="/__probe/hold.png" hidden alt=""><script>if(!sessionStorage.getItem("graph-probe-started")){localStorage.clear();sessionStorage.setItem("graph-probe-started","yes");}</script><script type="module">const fixtureData='+data+';\n'+JS+'</script></body>')

        @app.get('/__probe/index')
        def index():
            return HTMLResponse(page)

        @app.get('/__probe/hold.png')
        async def hold():
            await asyncio.sleep(globals().get('HOLD_SECONDS', 8 if '--narrow' in sys.argv else 52))
            return Response(b'',media_type='image/png')

        @app.post('/__probe/slow')
        def slow():
            control['slow'] = True
            return {'ok':True}

        @app.post('/__probe/export')
        async def capture(request: Request):
            payload = await request.json()
            assert len(payload['html'])<10*1024*1024 and len(payload['svg'])<1024*1024
            (OUT / 'graph-ui-report.html').write_text(payload['html'],encoding='utf-8')
            (OUT / 'graph-ui-workflow.svg').write_text(payload['svg'],encoding='utf-8')
            return {'ok':True}

        listener = socket.socket()
        listener.bind(('127.0.0.1',0))
        server = uvicorn.Server(uvicorn.Config(app,log_level='error'))
        worker = threading.Thread(target=lambda:server.run(sockets=[listener]),daemon=True)
        worker.start()
        deadline = time.monotonic()+8
        while not server.started:
            if time.monotonic()>deadline:
                raise RuntimeError('Temporary graph UI server did not start')
            time.sleep(.05)
        profile = OUT / ('graph-ui-profile-'+uid()[:8])
        try:
            case='graph-ui-narrow' if '--narrow' in sys.argv else 'graph-ui'
            completed = subprocess.run([r'C:\Program Files\Google\Chrome\Application\chrome.exe','--headless','--no-first-run','--no-default-browser-check','--disable-background-networking','--disable-component-update','--user-data-dir='+str(profile),'--window-size='+('520,1000' if '--narrow' in sys.argv else '1720,1150'),'--timeout=68000','--screenshot='+str(OUT / (case+'.png')),'--dump-dom',f'http://127.0.0.1:{listener.getsockname()[1]}/__probe/index'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=80)
        finally:
            server.should_exit = True
            worker.join(timeout=6)
            listener.close()
            if worker.is_alive():
                server.force_exit = True
            # Isolated fixtures must release a checkpoint connection before the
            # temporary directory is removed, including early browser failures.
            if hasattr(app.state, 'collab_jobs'):
                app.state.collab_jobs.close()
                app.state.collab_jobs._executor.shutdown(wait=True, cancel_futures=True)
        (OUT / (case+'-rendered.html')).write_bytes(completed.stdout)
        (OUT / (case+'-browser.log')).write_bytes(completed.stderr)
        match = re.search(rb'<pre id="graph-probe-result"[^>]*>(.*?)</pre>',completed.stdout,re.S)
        if not match:
            raise RuntimeError('Browser did not return graph workflow checks; inspect graph-ui-rendered.html')
        result = json.loads(html.unescape(match[1].decode('utf-8')))
        (OUT / (case+'-verification.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(result,ensure_ascii=True),flush=True)
        if not result.get('success'):
            raise RuntimeError('A graph UI check failed')


if __name__ == '__main__':
    main()
