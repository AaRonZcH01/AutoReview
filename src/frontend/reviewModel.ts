type R=Record<string,any>;
export function needsReview(a:R,p:R){
 return ['pending','retake_required'].includes(p.photo_status)||(typeof a.review_pending==='boolean'?a.review_pending:a.status==='unknown'||a.writing_status==='pending');
}
export function reviewCategory(a:R,p:R){
 if(['pending','retake_required'].includes(p.photo_status)||a.status==='unknown')return 'unknown';
 if(['incorrect','missing'].includes(a.status)||a.writing_status==='confirmed_error')return 'incorrect';
 return a.writing_status==='pending'?'unknown':a.status==='correct'?'correct':'incorrect';
}
export function questionLabel(a:R,p:R){return `第${p.slot}页 · ${a.section||'未分组'} · 第${a.number}题${a.part_id?' · 第'+a.part_id+'空':''}`}
export function questionKey(a:R,p:R){return p.id+':'+a.id}
export function reviewMatchesFilter(a:R,filter:string){return filter==='all'||(filter==='modified'?!!a.teacher_override:filter==='unknown'?needsReview(a,a._page):reviewCategory(a,a._page)===filter)}
// Gallery order is authoritative, including across submission boundaries.
export function reviewAdjacentPage(pages:R[],pageId:string,delta:number){const index=pages.findIndex(p=>p.id===pageId);return index<0?undefined:pages[index+delta]}
export function reviewAdjacentSubmission(pages:R[],submissionId:string,delta:number){const ids=[...new Set(pages.map(p=>p.parent_id))],index=ids.indexOf(submissionId);return index<0?undefined:pages.find(p=>p.parent_id===ids[index+delta])}
export function questionZone(a:R){
 const structure=a.observed_structure||{};
 const boxes:R[]=[structure.question_zone,structure.box,structure.answer_zone,a.box,
  ...(structure.options||[]).map((n:R)=>n.box),...(a.annotation_marks||[]).map((n:R)=>n.box)];
 if(a.kind==='matching')boxes.push(...(structure.nodes||[]).map((n:R)=>n.box));
 const valid=boxes.filter(b=>b&&['x1','x2','y1','y2'].every(k=>Number.isFinite(b[k]))&&b.x2>b.x1&&b.y2>b.y1);
 if(!valid.length)return undefined;
 return {x1:Math.max(0,Math.min(...valid.map(b=>b.x1))-.008),y1:Math.max(0,Math.min(...valid.map(b=>b.y1))-.008),x2:Math.min(1,Math.max(...valid.map(b=>b.x2))+.008),y2:Math.min(1,Math.max(...valid.map(b=>b.y2))+.008)};
}
export function questionFocus(a:R){
 const b=questionZone(a);if(!b)return undefined;
 // Keep the complete answer area and nearby printed context, never just ink.
 const horizontal=a.kind==='matching'?.018:Math.max(.045,(b.x2-b.x1)*.16),vertical=a.kind==='matching'?.03:Math.max(.035,(b.y2-b.y1)*.18);
 return {x1:Math.max(0,b.x1-horizontal),y1:Math.max(0,b.y1-vertical),x2:Math.min(1,b.x2+horizontal),y2:Math.min(1,b.y2+vertical)};
}
export function mergeReviewDetail(previous:R,incoming:R){
 if(!previous||previous.task?.id!==incoming.task?.id)return incoming;
 const keepNewest=(key:string)=>incoming[key]?.map((row:R)=>{const old=previous[key]?.find((v:R)=>v.id===row.id);return old&&old.revision>row.revision?old:row})||previous[key];
 const snapshots=new Map([...(incoming.score_snapshots||[]),...(previous.score_snapshots||[])].map((v:R)=>[v.id,v]));
 return {...incoming,pages:keepNewest('pages'),submissions:keepNewest('submissions'),score_snapshots:[...snapshots.values()]};
}
export function patchReviewDetail(previous:R,value:R){
 if(!previous||previous.task?.id!==value.submission?.task_id)return previous;
 const updated=new Map((value.pages||[value.page]).filter(Boolean).map((p:R)=>[p.id,p]));
 const pages=previous.pages.map((p:R)=>{const next:any=updated.get(p.id);return next&&next.revision>=p.revision?{...p,...next}:p});
 const submissions=previous.submissions.map((s:R)=>s.id===value.submission.id&&value.submission.revision>=s.revision?value.submission:s);
 const score_snapshots=value.score_snapshot?[...previous.score_snapshots.filter((s:R)=>s.id!==value.score_snapshot.id),value.score_snapshot]:previous.score_snapshots;
 return {...previous,pages,submissions,score_snapshots};
}
