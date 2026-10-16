import {needsReview} from './reviewModel.js';
type R=Record<string,any>;
export type ProcessingKind='done'|'cloud'|'queued'|'local'|'waiting'|'paused'|'failed'|'unsubmitted'|'withheld';
export function pendingQuestionCount(page:R):number|null{
 if(!page.reviewed&&page.state!=='processed'||!Array.isArray(page.results))return null;
 return new Set(page.results.filter((a:R)=>needsReview(a,page)).map((a:R,i:number)=>a.id??i)).size;
}
export function processingIndex(detail:R){
 const runs=new Map<string,R>((detail.runs||[]).map((r:R)=>[r.id,r])),work=new Map<string,R>(),attempts=new Map<string,R>();
 for(const w of detail.work_items||[])if(w.state!=='superseded')work.set(w.page_id+':'+w.generation,w);
 for(const a of detail.attempts||[])if(a.state==='running')attempts.set(a.parent_id,a);
 return {runs,work,attempts};
}
export function pageProcessing(page:R,index:ReturnType<typeof processingIndex>):{kind:ProcessingKind,label:string}{
 if(page.capture_withheld)return {kind:'withheld',label:'已撤回识别'};
 if(page.reviewed||page.state==='processed')return {kind:'done',label:page.reviewed?'已复核':'已判分'};
 const work=index.work.get(page.id+':'+page.generation),run=index.runs.get(work?.parent_id),attempt=index.attempts.get(work?.id),phase=work?.progress?.phase;
 if(work?.state==='running'&&attempt&&(attempt.run_generation==null||attempt.run_generation===run?.generation)&&!['validating','completed'].includes(phase)&&
    (attempt.turn_id||['waiting','cloud_active','receiving'].includes(phase)))return {kind:'cloud',label:attempt.purpose==='student_verify'?'云端保守复看中':'云端识别中'};
 if(work?.state==='failed'||page.state==='failed')return {kind:'failed',label:run?.state==='paused'?'本页处理失败 · 本轮已暂停':'本页处理失败 · 等待重试'};
 if(['paused','interrupted','stopping'].includes(run?.state))return {kind:'paused',label:'处理已暂停'};
 if(work?.state==='running')return {kind:'local',label:['validating','completed'].includes(phase)?'正在校验结果与判分':'正在准备识别'};
 if(work?.state==='pending')return {kind:'queued',label:'已入队，等待识别空位'};
 if(page.capture_delivery_mode==='local')return {kind:'unsubmitted',label:'已保存本地 · 待批量提交'};
 if(page.capture_dependency_wait||page.capture_session_id&&page.preparation?.confirmed)return {kind:'waiting',label:'等待必要跨页图片'};
 return {kind:'unsubmitted',label:'尚未提交处理'};
}
