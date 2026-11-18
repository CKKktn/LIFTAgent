import {api} from './api.js';
import {ask,fatalReviewError,noteIssue} from './review.js';

export async function runRetrievalReview({report,signal,progress,onUpdate,sequential=false,deferComplete=false}){
  const whole=report.scope==='dataset';
  if(!report.kb)throw Error('未选择知识库');
  const baselineIds=new Set(report.baselineDocs.map(d=>d.id));
  const allowed=new Map((report.targetDocs||[]).filter(d=>!report.skippedFiles?.some(x=>x.id===d.id)).map(d=>[d.id,d]));
  for(const d of report.baselineDocs)allowed.delete(d.id);
  if(!whole&&!allowed.size)throw Error('没有有效待审文件，不能进行限定范围检索');
  const failedIds=new Set((report.issues||[]).filter(x=>x.phase==='指标检索判定').map(x=>x.requirementId));
  const pending=report.requirements.filter(r=>!report.results.some(x=>x.requirementId===r.id&&!x.retryable&&!(x.status==='待人工复核'&&failedIds.has(r.id))));
  if(pending.length){report.polished=[];report.rawReport=null;report.rawComplete=false;}
  const save=result=>{const i=report.results.findIndex(x=>x.requirementId===result.requirementId);if(i<0)report.results.push(result);else report.results[i]=result;};
  const clearIssue=id=>{report.issues=(report.issues||[]).filter(x=>!(x.phase==='指标检索判定'&&x.requirementId===id));};
  // Fetch names once for all existence checks, including unparsed documents.
  let catalogPromise;
  async function catalog(){
    if(!catalogPromise)catalogPromise=(async()=>{const docs=[];for(let page=1;;page++){
      const res=await api('GET',`/api/v1/datasets/${encodeURIComponent(report.kb)}/documents?page=${page}&page_size=100`,undefined,signal);
      const value=res.data,rows=Array.isArray(value)?value:value?.docs;
      if(!Array.isArray(rows))throw Error('文件清单响应无效');
      docs.push(...rows);if(rows.length<100||docs.length>=(value.total??Infinity))break;
    }return docs.filter(d=>!baselineIds.has(d.id)&&! /^(?:~\$|\.~lock\.)|\.(?:tmp|temp|lock)$/i.test(String(d.name).split(/[\\/]/).pop()));})();
    return catalogPromise;
  }
  async function documentResult(r){
    const docs=await catalog(),norm=s=>String(s).normalize('NFKC').toLowerCase().replace(/\.(pdf|docx?|xlsx?|pptx?|txt|md)$/i,'').replace(/[\s_\-（）()《》]/g,'');
    const wanted=norm(r.document_name||r.title);if(wanted.length<2)throw Error('文件名称不明确，请修改指标清单');
    const matches=docs.filter(d=>norm(String(d.name).split(/[\\/]/).pop()).includes(wanted));
    // For renamed deliverables, compare names only; uncertain matches remain unconfirmed.
    if(!matches.length)for(let start=0;start<docs.length;start+=100){
      const group=docs.slice(start,start+100),names=group.map((d,i)=>({id:'F'+i,name:d.name}));
      const selected=await ask(`仅检查交付文档是否存在。要求文件：${r.document_name||r.title}。以下为知识库真实文件名清单（不是正文）。识别文件名称明确表示同一种交付物的文件，包括常见简称和同义名称；不能因同属一个项目、主题相关就算匹配，例如测试计划不等于测试报告。不确定则不选。只输出 JSON {"matches":[{"id":"F0","reason":"名称等价依据"}]}。禁止读取或推断文件内容。文件名：${JSON.stringify(names)}`,{model:report.model,signal,attempts:1,maxTokens:1024},v=>{if(!Array.isArray(v.matches)||v.matches.some(x=>!names.some(n=>n.id===x.id)||typeof x.reason!=='string'))throw Error('文件名称匹配结果无效');});
      for(const x of selected.matches){const d=group[Number(x.id.slice(1))];if(!matches.some(m=>m.id===d.id))matches.push(d);}
    }
    return {status:matches.length?'已满足':'证据不足',analysis:matches.length?`文件清单中找到 ${matches.length} 个名称匹配文档，仅确认存在，未检查内容。`:'完整文件清单中未找到名称匹配文档；可能使用不同名称，需人工核对。',gaps:matches.length?'无（仅文件存在性）':'未找到对应文件名称',recommendation:matches.length?'无需进行文档内容核查':'核对文件别名或补充上传',evidence:matches.map((d,i)=>({source_id:'F'+(i+1),quote:d.name,analysis:'知识库文件清单中的实际文件名',kind:'支持',source:{document_id:d.id,document_name:d.name,dataset_id:report.kb,content:d.name}})),checkType:'document',retrievalRounds:0};
  }
  let cursor=0,fatal;
  async function worker(){while(cursor<pending.length&&!fatal){const requirement=pending[cursor++];let evidence=[],rounds=0,result;
    try{
      if(requirement.type==='document'){progress(2,`${requirement.id} · 核对知识库文件清单`);const found=await documentResult(requirement);clearIssue(requirement.id);save({...found,requirementId:requirement.id});onUpdate(report);continue;}
      for(let round=0;round<2;round++){
        signal.throwIfAborted();rounds++;
        progress(2,`${requirement.id} · ${round?'补充检索':'检索与判定'} · 本轮 ${pending.indexOf(requirement)+1}/${pending.length}`);
        const question=round?`${requirement.title} ${requirement.criterion} 项目方案 技术设计 功能实现 参数 配置 ${result?.gaps||''}`:`${requirement.title} ${requirement.criterion}`;
        const response=await api('POST','/api/v1/retrieval',{question:question.slice(0,2000),dataset_ids:[report.kb],exclude_document_ids:[...baselineIds],...(whole?{}:{document_ids:[...allowed.keys()]}),page:1,page_size:whole?32:8,similarity_threshold:round?0.1:0.2,vector_similarity_weight:0.3,top_k:64,highlight:false},signal);
        const before=evidence.length;let added=0;const chunks=response.data?.chunks;if(!Array.isArray(chunks))throw Error('检索接口未返回有效切片列表');
        for(const c of chunks){const id=c.document_id||c.doc_id;if(!id||baselineIds.has(id)||(!whole&&!allowed.has(id))||(c.dataset_id&&c.dataset_id!==report.kb)||(c.kb_id&&c.kb_id!==report.kb))continue;const filename=whole?(c.document_name||c.docnm_kwd||c.name||id):allowed.get(id).name;if(/^(?:~\$|\.~lock\.)|\.(?:tmp|temp|lock)$/i.test(String(filename).split(/[\\/]/).pop()))continue;const content=String(c.content||c.content_with_weight||'').replace(/<\/?(?:em|mark)>/gi,'');if(!content.trim())continue;if(whole&&added>=8)continue;if(evidence.some(e=>e.document_id===id&&(c.id?e.id===c.id:e.content===content)))continue;evidence.push({...c,document_id:id,dataset_id:report.kb,document_name:filename,content,reviewId:'E'+(evidence.length+1)});added++;}
        if(round&&result&&evidence.length===before)break; // No new evidence: keep the previous answer.
        if(!evidence.length){result={status:'证据不足',analysis:'在所选知识库的检索范围内未找到足够证据，不等同于未完成。',gaps:'缺少对应测试、实施或验收记录',recommendation:'补充记录或调整指标关键词后复核',evidence:[]};continue;}
        let used=0;const supplied=evidence.map(c=>{const remaining=Math.max(0,24000-used),content=c.content.slice(0,Math.min(Math.floor(24000/evidence.length),3000,remaining));used+=content.length;return {...c,content};}).filter(c=>c.content);
        // Number original excerpts in code; the model selects IDs instead of copying quotes.
        const excerpts=[];
        for(const c of supplied)for(let offset=0;offset<c.content.length;offset+=800)excerpts.push({...c,reviewId:'E'+(excerpts.length+1),content:c.content.slice(offset,offset+800)});
        const map=new Map(excerpts.map(c=>[c.reviewId,c]));
        let rejected=0;
        result=await ask(`${requirement.type==='numeric'?'这是数值指标核查：比较指标要求和项目方案的参数、单位、比较方向、适用条件。项目方案明确数值符合即可判已满足（方案符合），无需强求实测或验收报告；不得将方案符合宣称为实测达标。':'这是功能条件核查：检查材料是否明确描述该功能如何实现、具有何种属性。有明确实现/设计机制可作为支持；仅重复需求或提出未来愿望不算实现。无需强求量化数据或测试报告。'}未召回不能判为未完成。不满足必须有明确反证。材料作用分类是相对于本条指标及核查目标，不是相对于最终结论：“支持”表示材料直接支持全部或部分符合本条要求，具体方案参数或实现机制也可属于支持，必须注明属于设计符合还是实测结果；“反证”表示同一对象、同一核查阶段及可比条件下存在明确不符事实，其他阶段的实测不足不能直接推断设计参数不符；“计划”表示仅有未来安排、愿望或承诺，不能把已有明确参数或实现机制的方案一律标为计划；“背景”表示要求基准、定义、条件或上下文，不能独立证明已满足或不满足。每个引用只归入最贴合的一类，不重复选择同一原文来凑证据。project_plan只写材料中的参数或实现，analysis只写比较推理，gaps只写尚存缺口，recommendation只写可执行后续行动；避免四栏重复同一句。输出 JSON：{"status":"已满足/部分满足/不满足/证据不足/证据冲突","project_plan":"项目方案参数或功能实现情况，注明来源编号", "analysis":"对比分析，注明证据编号","gaps":"缺口与冲突","recommendation":"建议","evidence":[{"source_id":"E1","analysis":"此段原文证明什么","kind":"支持/反证/计划/背景"}]}。只选择下方真实 source_id，不输出 quote，不抄写或改写原文作为引文；程序会按编号填入对应原文。没有依据则 evidence 为空，判为证据不足。
表达要求：对比分析用完整、自然的条件句写明要求阈值、方案数值和比较结果，保留真实数值、单位与适用条件。避免“比较方向为不低于”等不自然措辞；改用“要求至少达到……，方案为……，因此……”或“要求不超过……，方案为……，因此……”说明。不得补造未提供的数值。
指标：${JSON.stringify({title:requirement.title,criterion:requirement.criterion})}
本条问句提示词：${requirement.question||requirement.title}
关注点：${report.focus||'全部指标'}
证据：${JSON.stringify(excerpts.map(c=>({source_id:c.reviewId,file:c.document_name,content:c.content})))}`,{model:report.model,signal,attempts:2},v=>{
          if(!['已满足','部分满足','不满足','证据不足','证据冲突'].includes(v.status))throw Error('判定状态无效');
          if(typeof v.analysis!=='string'||!v.analysis.trim())throw Error('缺少判定分析');
          for(const [k,fallback] of [['gaps','模型未提供缺口说明'],['recommendation','模型未提供补证建议']])if(typeof v[k]!=='string'||!v[k].trim())v[k]=fallback;
          if(typeof v.project_plan!=='string'||!v.project_plan.trim())v.project_plan='模型未提供项目方案或实现情况说明';
          if(!Array.isArray(v.evidence))throw Error('缺少证据数组');
          const good=[];rejected=0;const seen=new Set();
          for(const e of v.evidence){
            if(!e||!map.has(e.source_id)||typeof e.analysis!=='string'||!['支持','反证','计划','背景'].includes(e.kind)){rejected++;continue;}
            if(seen.has(e.source_id))continue;seen.add(e.source_id);
            const source=map.get(e.source_id);good.push({...e,quote:source.content,source:{...source,content_with_weight:undefined}});
          }
          v.evidence=good;
          const unsupported=(['已满足','部分满足'].includes(v.status)&&!good.some(e=>e.kind==='支持'))||(v.status==='不满足'&&!good.some(e=>e.kind==='反证'))||(v.status==='证据冲突'&&good.length<2);
          if(rejected||unsupported){v.status='待人工复核';v.retryable=true;v.gaps+='；部分引用编号无效或不足以支持原判定，已保留有效原文，请复核。';}
        });
        clearIssue(requirement.id);
        if(result.retryable)noteIssue(report,{phase:'指标检索判定',requirementId:requirement.id,retained:result.evidence.length,reason:rejected?`${rejected} 条证据编号或说明无效，已隔离；其余证据已保留`:'有效证据不足以支持模型判定，已降级为待人工复核'});
        if(['已满足','不满足'].includes(result.status))break;
      }
      if(!result.retryable)clearIssue(requirement.id);
      save({...result,checkType:requirement.type,requirementId:requirement.id,retrievalRounds:rounds,retrievedChunks:evidence.length});
    }catch(e){if(fatalReviewError(e,signal)){fatal=e;return;}clearIssue(requirement.id);noteIssue(report,{phase:'指标检索判定',requirementId:requirement.id,retained:result?.evidence?.length||0,reason:e.message});save(result?{...result,requirementId:requirement.id,retryable:true,gaps:result.gaps+'；补充检索或判定失败：'+e.message}:{requirementId:requirement.id,status:'待人工复核',retryable:true,analysis:'本项检索或判定失败，继续其余指标。',gaps:e.message,recommendation:'继续审查时将重试本项，已成功条目不重跑。',evidence:[]});}
    onUpdate(report);
  }}
  await Promise.all(sequential?[worker()]:[worker(),worker()]);if(fatal)throw fatal;signal.throwIfAborted();
  report.results.sort((a,b)=>report.requirements.findIndex(r=>r.id===a.requirementId)-report.requirements.findIndex(r=>r.id===b.requirementId));
  report.limited=Boolean(report.issues?.length||report.skippedFiles?.length);report.complete=!deferComplete;report.finishedAt=new Date().toISOString();onUpdate(report);
}
