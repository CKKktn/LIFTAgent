"""Real collaborative-review UI, isolated SQLite and local synthetic model only."""
from pathlib import Path
import importlib.util
import json
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('collab_browser_fixture', ROOT / 'tests/browser/quick_review_fixture.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
probe.OUT = ROOT / 'runtime-logs/collab-upgrade/browser' / ('narrow-dark' if '--narrow' in sys.argv else 'desktop-light')
probe.HOLD_SECONDS = 40
original_answer = probe.fixture.answer_for


def synthetic_answer(messages):
    combined = '\n'.join(str(row.get('content', '')) for row in messages)
    if 'COLLAB_ANSWER_V1' in combined and 'INPUT_JSON:' in str(messages[-1]['content']):
        value = probe.fixture.json_after(str(messages[-1]['content']), 'INPUT_JSON:')
        if 'BROWSER_SLOW_TEST' in value.get('question', ''):
            time.sleep(1.6)
    return original_answer(messages)


probe.fixture.answer_for = synthetic_answer
probe.JS = r'''
const result=JSON.parse(sessionStorage.getItem('collab-ui-results')||'{"errors":[]}'),$=id=>document.getElementById(id),wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
window.addEventListener('error',event=>result.errors.push(event.message));window.addEventListener('unhandledrejection',event=>result.errors.push(String(event.reason)));
const until=async(fn,label,timeout=25000)=>{const end=performance.now()+timeout;while(!await fn()){if(performance.now()>end)throw Error('Timed out: '+label);await wait(60);}};
const api=async(path,method='GET',body)=>{const response=await fetch(path,{method,headers:body?{'Content-Type':'application/json'}:undefined,body:body?JSON.stringify(body):undefined});const value=await response.json();if(!response.ok)throw Error(path+' '+response.status+' '+JSON.stringify(value));return value;};
const view=()=>JSON.parse(localStorage.getItem('lift-collab-view')||'{}');
const current=()=>api('/api/v1/collab-jobs/'+view().job_id);
const modalOpen=()=>$('modal').open;
const action=name=>$('collabWorkflow').querySelector('[data-collab-action="'+name+'"]');
async function settled(status){let job;await until(async()=>{job=await current();return job.status===status;},status);await until(()=>status==='waiting_confirmation'?!!action('approve'):status==='completed'?!$('chatInput').disabled:status==='stopped'?!!action('resume'):!$('chatInput').disabled,'UI '+status);return job;}
async function ask(question,confirm=false){
 await until(()=>!$('chatInput').disabled&&!$('sendChat').disabled,'composer ready');const old=view().job_id;
 $('collabRequireConfirmation').checked=confirm;$('chatInput').value=question;$('chatForm').requestSubmit();
 await until(()=>view().job_id&&view().job_id!==old,'job created');return current();
}
async function review(decision,{answer,notes,query}={}){
 action(decision).click();await until(modalOpen,'review dialog');
 if(answer!==undefined)$('collabReviewAnswer').value=answer;if(notes!==undefined)$('collabReviewNotes').value=notes;if(query!==undefined)$('collabSupplementQuery').value=query;
 $('modalSubmit').click();await until(()=>!modalOpen(),'review saved');
}
try{
 if(!sessionStorage.getItem('collab-ui-phase'))localStorage.clear();
 await api('/auth/login','POST',{username:'graph-browser',password:'synthetic-only-password'});await import('/app.js');
 await until(()=>$('chatModel').options.length>1&&$('assistantSelect').options.length>1,'initialized');location.hash='collab';document.documentElement.dataset.theme=fixtureData.narrow?'dark':'light';
 if(sessionStorage.getItem('collab-ui-phase')!=='reloaded'){
  const factual=await ask('设备续航时间是多少？');const first=await settled('completed');
  result.factual_completes=first.id===factual.id&&first.answer.includes('10小时')&&first.reference.chunks.length>0;
  result.factual_reply_complete=$('chatMessages').lastElementChild.querySelector('.reply-state').textContent.includes('回复完成');
  const sources=$('chatMessages').lastElementChild.querySelector('details.sources');sources.open=true;sources.querySelector('[data-file-source]').click();await until(()=>$('fileViewer').open,'source viewer');result.source_viewer_preserved=$('viewerBody').textContent.includes('续航');$('viewerClose').click();
  $('chatMessages').querySelector('[data-mark="1"]').click();await until(()=>$('chatMessages').querySelector('[data-mark="1"]').classList.contains('marked'),'annotation saved');
  $('chatMessages').querySelector('[data-note="1"]').click();await until(modalOpen,'annotation note');$('modalForm').querySelector('[name="note"]').value='合成验收：已人工核对来源。';$('modalSubmit').click();await until(()=>!modalOpen(),'note saved');
  const annotations=await api('/conversation-annotations?session='+first.session_id);result.annotations_preserved=annotations.some(row=>row.message_index===1&&row.state==='reviewed'&&row.note.includes('合成验收'));
  await ask('设备方案续航10小时是否符合合同要求？');const waiting=await settled('waiting_confirmation');
  result.review_requires_confirmation=waiting.requires_confirmation&&$('chatInput').disabled&&!$('chatMessages').lastElementChild.querySelector('.reply-state').textContent.includes('回复完成');
  result.waiting_sources_visible=waiting.reference.chunks.length>0&&!!$('chatMessages').lastElementChild.querySelector('details.sources');
  sessionStorage.setItem('collab-ui-session',waiting.session_id);sessionStorage.setItem('collab-ui-job',waiting.id);sessionStorage.setItem('collab-ui-results',JSON.stringify(result));sessionStorage.setItem('collab-ui-phase','reloaded');location.reload();
 }else{
  const expected=sessionStorage.getItem('collab-ui-job');await until(()=>view().job_id===expected&&!!action('approve'),'reload restoration');
  let job=await current(),session=(await api('/api/v1/chats/'+job.chat_id+'/sessions/'+job.session_id)).data;
  result.reload_same_session_and_job=job.id===expected&&job.session_id===sessionStorage.getItem('collab-ui-session');
  result.reload_no_duplicate_turns=session.messages.length===4&&new Set(session.messages.map(message=>message.id)).size===4&&$('chatMessages').querySelectorAll('.message').length===4;
  await review('approve',{notes:'人工核对来源后确认方案文字。'});job=await settled('completed');result.approval_saved=job.review.decision==='approve';
  await ask('请概述设备续航设计参数。',true);job=await settled('waiting_confirmation');result.force_confirmation_honored=job.requires_confirmation;
  const edited=job.answer+'\n人工说明：仅确认方案描述，实际测试另行核查。';await review('revise',{answer:edited,notes:'按实际适用阶段补充复核意见。'});job=await settled('completed');
  result.human_revision_saved=job.review.decision==='revise'&&job.answer===edited&&$('chatMessages').lastElementChild.textContent.includes('人工说明');
  await ask('设备续航是否达标？');job=await settled('waiting_confirmation');const supplementId=job.id,sessionBefore=(await api('/api/v1/chats/'+job.chat_id+'/sessions/'+job.session_id)).data;
  await review('supplement',{query:'补充核对设备续航的测试记录和适用阶段。',notes:'需要补充同一对象的实测依据。'});job=await settled('waiting_confirmation');
  const sessionAfter=(await api('/api/v1/chats/'+job.chat_id+'/sessions/'+job.session_id)).data;
  result.supplement_is_same_turn=job.id===supplementId&&job.manual_rounds===1&&sessionAfter.messages.length===sessionBefore.messages.length;
  await review('reject',{notes:'本轮草稿退回，后续补齐资料。'});job=await settled('completed');
  result.rejected_not_approved=job.review.decision==='reject'&&$('chatMessages').lastElementChild.querySelector('.reply-state').textContent==='已人工退回';
  await ask('BROWSER_SLOW_TEST：设备续航时间是多少？');await until(async()=>(await current()).phase==='generate_answer','slow model stage');const stoppedId=view().job_id;$('stopChat').click();job=await settled('stopped');
  result.stop_saved=job.id===stoppedId&&job.can_resume&&$('chatInput').disabled;
  action('resume').click();job=await settled('completed');const resumed=(await api('/api/v1/chats/'+job.chat_id+'/sessions/'+job.session_id)).data;
  result.resume_without_duplicates=job.id===stoppedId&&new Set(resumed.messages.map(message=>message.id)).size===resumed.messages.length&&resumed.messages.length===10;
  await ask('请概述设备方案参数。',true);await settled('waiting_confirmation');action('cancel').click();await until(modalOpen,'cancel dialog');$('modalSubmit').click();await until(()=>!modalOpen(),'cancel accepted');job=await settled('cancelled');
  result.cancel_unlocks_session=job.status==='cancelled'&&!$('chatInput').disabled;
  await ask('展示最终待人工确认的协审草稿。',true);job=await settled('waiting_confirmation');
  result.real_processing_summary=!!$('collabWorkflow').querySelector('.collab-flow-trace li')&&$('collabWorkflow').textContent.includes('待确认草稿');
  await wait(120);const latest=$('chatMessages').lastElementChild.getBoundingClientRect(),composer=$('chatForm').getBoundingClientRect(),messages=$('chatMessages').getBoundingClientRect();
  result.latest_draft_above_composer=latest.bottom<=composer.top+1&&latest.top>=messages.top-1;
  let fit=true;
  for(const scale of ['.9','1','1.1','1.25']){
   document.documentElement.style.setProperty('--scale',scale);await wait(80);const panel=$('collabWorkflow');fit&&=panel.scrollWidth<=panel.clientWidth+2;
   for(const button of panel.querySelectorAll('button')){const rect=button.getBoundingClientRect(),range=document.createRange();range.selectNodeContents(button);fit&&=[...range.getClientRects()].every(line=>line.left>=rect.left-1&&line.right<=rect.right+1&&line.top>=rect.top-1&&line.bottom<=rect.bottom+1);}
  }
  document.documentElement.style.setProperty('--scale','1');result.review_controls_fit=fit;
  $('collabWorkflow').querySelector('details').open=true;await wait(80);const detail=$('collabWorkflow').querySelector('.collab-flow-detail-body').getBoundingClientRect();result.process_details_fit=detail.right<=innerWidth+1&&detail.left>=-1; $('collabWorkflow').querySelector('details').open=false;
  result.no_raw_chain_of_thought=![...$('chatMessages').querySelectorAll('.reasoning summary')].some(summary=>summary.textContent==='思考过程');
  result.success=Object.entries(result).filter(([key])=>key!=='errors').every(([,value])=>value===true)&&result.errors.length===0;
  $('chatMessages').scrollTop=$('chatMessages').scrollHeight;window.scrollTo({top:0,behavior:'instant'});await wait(180);
 }
}catch(error){result.success=false;result.error=String(error.stack||error);result.debug={view:view(),status:$('collabWorkflow')?.textContent,toast:$('toast')?.textContent};}
if(Object.hasOwn(result,'success')){const output=document.createElement('pre');output.id='graph-probe-result';output.hidden=true;output.textContent=JSON.stringify(result);document.body.append(output);}
'''

if __name__ == '__main__':
    probe.main()
