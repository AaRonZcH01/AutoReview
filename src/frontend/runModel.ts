export function runPageCounts(items:Record<string,any>[]){
 const counts=new Map<string,{completed:number,total:number}>();
 for(const item of items){const value=counts.get(item.parent_id)||{completed:0,total:0};value.total++;if(item.state==='completed')value.completed++;counts.set(item.parent_id,value)}
 return counts;
}
export function activeRunPages(items:Record<string,any>[],pages:Record<string,any>[],submissions:Record<string,any>[]){
 const byPage=new Map(pages.map(page=>[page.id,page])),bySubmission=new Map(submissions.map(sub=>[sub.id,sub]));
 const active=new Map<string,Record<string,any>[]>();
 for(const work of items){if(work.state!=='running')continue;const page=byPage.get(work.page_id),paper=page&&bySubmission.get(page.parent_id);const rows=active.get(work.parent_id)||[];rows.push({id:work.id,tag:paper?.paper_tag,slot:page?.slot,purpose:work.progress?.purpose||work.purpose,phase:work.progress?.phase});active.set(work.parent_id,rows)}
 return active;
}
