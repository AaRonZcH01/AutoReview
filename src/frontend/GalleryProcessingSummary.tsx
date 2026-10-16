import {pageProcessing,processingIndex,ProcessingKind} from './pageProcessing.js';
type R=Record<string,any>;
export function GalleryProcessingSummary({pages,detail,locate}:{pages:R[],detail:R,locate:(page:R)=>void}){
 const index=processingIndex(detail),rows=pages.map(page=>({page,...pageProcessing(page,index)})),pending=rows.filter(r=>r.kind!=='done');
 const groups:[ProcessingKind,string][]=[['cloud','云端处理中'],['queued','已入队等待'],['local','本地准备／判分'],['waiting','等待跨页图片'],['paused','已暂停'],['failed','处理失败'],['withheld','已撤回识别'],['unsubmitted','尚未提交']];
 const counts=new Map(groups.map(([key])=>[key,rows.filter(r=>r.kind===key).length]));
 const submissions=new Map<string,R>(detail.submissions.map((s:R)=>[s.id,s]));
 return <section className='gallery-processing-summary' aria-label='作业处理总览'>
  <div className='gallery-processing-counts' role='status' aria-live='polite'><strong>云端处理中 {counts.get('cloud')} 张</strong><b>尚未判分完成 {pending.length} 张</b>{groups.filter(([key])=>key!=='cloud'&&counts.get(key)).map(([key,label])=><span key={key}>{label} {counts.get(key)} 张</span>)}</div>
  {pending.length>0&&<details><summary>查看尚未判分完成的页面 · {pending.length} 张</summary><ul>{pending.map(({page,label,kind})=><li key={page.id} className={kind}><button onClick={()=>locate(page)}>纸签 {submissions.get(page.parent_id)?.paper_tag} · 第 {page.slot}/{submissions.get(page.parent_id)?.page_count} 页</button><span>{label}</span></li>)}</ul></details>}
 </section>;
}
