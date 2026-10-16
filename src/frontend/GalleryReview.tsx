import {useState,useEffect,useRef} from 'react';
import {taskCache} from './sessionCache.js';
import {ConfirmationDialog} from './ConfirmationDialog.js';
import './galleryReview.css';
type R=Record<string,any>;
const pendingJobs=new Map<string,R>();
const stageLabel=(stage:string)=>({queued:'已排队',checking:'正在核查页面',saving:'正在保存复核结果',completed:'已完成',failed:'已停止',interrupted:'已中断'} as Record<string,string>)[stage]||'正在处理';
export function GalleryReview({task,pages,api,refresh,open,active,children}:{task:string,pages:R[],api:(path:string,data?:any)=>Promise<R>,refresh:()=>Promise<any>,open:(id:string)=>void,active:boolean,children:(selected:Set<string>,toggle:(id:string,shift:boolean)=>void)=>React.ReactNode}){
 const [ids,Ids]=useState(()=>new Set<string>(taskCache.selections.get(task)||[])),[last,Last]=useState(''),[plan,Plan]=useState<R|null>(null),[result,Result]=useState<R|null>(null),[busy,Busy]=useState(false),[error,Error]=useState(''),[progress,Progress]=useState<R|null>(null),[history,History]=useState<R>({}),[uncertain,Uncertain]=useState<R|null>(null);
 const opener=useRef<HTMLElement|null>(null);
 const uncertainRef=useRef<R|null>(null);uncertainRef.current=uncertain;
 const mounted=useRef(true),busyRef=useRef(false),latest=useRef({active,api,refresh});latest.current={active,api,refresh};
 const selected=new Set([...ids].filter(id=>pages.some(p=>p.id===id)));
 const readHistory=()=>api('/tasks/'+task+'/batch-review-history').then(v=>{if(mounted.current)History(v)});
 useEffect(()=>{taskCache.selections.set(task,[...ids])},[task,ids]);
 useEffect(()=>{mounted.current=true;readHistory().catch(e=>Error(e.message));const pending=pendingJobs.get(task);if(pending)execute(pending);return()=>{mounted.current=false}},[task]);
 useEffect(()=>{if(active&&!busyRef.current)readHistory().catch(()=>{})},[active]);
 const lock=(v:boolean)=>{busyRef.current=v;if(mounted.current)Busy(v)};
 const toggle=(id:string,shift:boolean)=>{if(busyRef.current)return;const next=new Set(selected),from=pages.findIndex(p=>p.id===last),to=pages.findIndex(p=>p.id===id);const targets=shift&&from>=0?pages.slice(Math.min(from,to),Math.max(from,to)+1).map(p=>p.id):[id];const removing=next.has(id);for(const x of targets)removing?next.delete(x):next.add(x);Ids(next);Last(id);Plan(null);Result(null)};
 const execute=async(intent:R)=>{
  if(busyRef.current||uncertainRef.current&&uncertainRef.current.request.request_id!==intent.request.request_id)return;lock(true);Error('');Uncertain(null);pendingJobs.set(task,intent);
  try{let job=intent.job?await api('/tasks/'+task+'/batch-review-jobs/'+intent.job):await api(intent.path,intent.request);intent={...intent,job:job.id};pendingJobs.set(task,intent);
   while(true){if(mounted.current)Progress(job);if(['completed','failed','interrupted'].includes(job.state))break;if(!mounted.current)return;await new Promise(r=>setTimeout(r,250));job=await api('/tasks/'+task+'/batch-review-jobs/'+job.id)}
   pendingJobs.delete(task);if(job.state!=='completed')throw new globalThis.Error(job.error_message||job.error||job.message||'批量操作未完成，请核对最新状态。');
   if(mounted.current){Result(job.result);Plan(null);Progress(job);await refresh();await readHistory()}
  }catch(e){if(mounted.current){Error((e as Error).message);if(pendingJobs.has(task))Uncertain(pendingJobs.get(task)!);await readHistory().catch(()=>{})}}finally{lock(false)}
 };
 const run=async(action:string,preview=true)=>{if(busyRef.current)return;if(!preview){await execute({path:'/tasks/'+task+'/batch-review-jobs',request:{...plan!.request,preview:false,confirm:true}});return}opener.current=document.activeElement as HTMLElement;lock(true);Error('');Result(null);Progress(null);try{const request={action,request_id:crypto.randomUUID(),pages:pages.filter(p=>selected.has(p.id)).map(p=>({id:p.id,revision:p.revision}))};const r=await api('/tasks/'+task+'/batch-review',{...request,preview:true});Plan({...r,request})}catch(e){Error((e as Error).message)}finally{lock(false)}};
 const undoRedo=(action:string)=>execute({path:'/tasks/'+task+'/batch-review-history',request:{action,request_id:crypto.randomUUID()}});
 const keys=useRef({undoRedo,history});keys.current={undoRedo,history};
 useEffect(()=>{const key=(e:KeyboardEvent)=>{if(!latest.current.active||busyRef.current||uncertainRef.current||!(e.ctrlKey||e.metaKey)||e.altKey)return;const el=e.target as HTMLElement;if(el.closest('input,textarea,select,[contenteditable=true]')||document.querySelector('[role=dialog]'))return;const name=e.key.toLowerCase(),action=name==='y'||name==='z'&&e.shiftKey?'redo':name==='z'?'undo':'';if(!action)return;e.preventDefault();if(keys.current.history['can_'+action])keys.current.undoRedo(action)};addEventListener('keydown',key);return()=>removeEventListener('keydown',key)},[]);
 const feedback=result;
 return <><div className='gallery-review-toolbar'><b>批量教师复核</b><button disabled={busy} onClick={()=>{Ids(new Set(pages.map(p=>p.id)));Plan(null)}}>全选当前筛选</button><button disabled={busy||!selected.size} onClick={()=>{Ids(new Set());Plan(null)}}>清空选择</button><span>已选 {selected.size} 张 · 点击卡片选择，Shift 连选</span><button className='primary' disabled={busy||!!uncertain||!selected.size} onClick={()=>run('confirm')}>确认所选复核…</button><button disabled={busy||!!uncertain||!selected.size} onClick={()=>run('unconfirm')}>取消所选复核…</button><div className='gallery-history'><button title={history.undo_label||'撤销最近一次批量复核 Ctrl+Z'} disabled={busy||!!uncertain||!history.can_undo} onClick={()=>undoRedo('undo')}>↶ 撤销 <kbd>Ctrl+Z</kbd></button><button title={history.redo_label||'重做 Ctrl+Y / Ctrl+Shift+Z'} disabled={busy||!!uncertain||!history.can_redo} onClick={()=>undoRedo('redo')}>↷ 重做 <kbd>Ctrl+Y</kbd></button></div></div>
 {error&&!plan&&<div role='alert' className='message error'>{error}{uncertain&&<button disabled={busy} onClick={()=>execute(uncertain)}>查询并继续本次操作</button>}</div>}
 {(busy||progress)&&<div role='status' aria-live='polite' className='gallery-batch-progress'><div><b>{busy?progress?.current?.paper_tag?`正在处理纸签 ${progress.current.paper_tag} · 第 ${progress.current.slot} 页`:progress?stageLabel(progress.stage):'正在检查所选页面…':`${progress?.state==='completed'?'本次操作已完成':progress?.state==='failed'?'本次操作失败':progress?.state==='interrupted'?'本次操作已中断':'本次操作状态待核对'} ${progress?.completed||0} / ${progress?.total||0} 张`}</b><span>{busy&&progress?`${progress.completed} / ${progress.total} 张 · ${stageLabel(progress.stage)}`:''}</span></div><progress max={progress?.total||1} value={progress?.completed||0}/></div>}
 {active&&plan&&<ConfirmationDialog returnFocus={opener.current} title={plan.action==='confirm'?'确认所选页面复核？':'取消所选页面复核？'} busy={busy} confirmDisabled={!!uncertain||!plan.accepted.length} confirmLabel={`确认执行 ${plan.accepted.length} 张${plan.rejected.length?'（其余跳过）':''}`} onConfirm={()=>run(plan.action,false)} onCancel={()=>Plan(null)} error={error}>
  <div className='confirmation-dialog-summary'><b>可执行 {plan.accepted.length} 张，拒绝 {plan.rejected.length} 张</b></div><p>{plan.action==='confirm'?'仅确认没有待核实和照片问题的已处理页面，不会将未知答案改成正确。':'取消复核保留教师判定与历史，统计随之更新。'}</p>
  <div className='confirmation-dialog-issues'>{plan.rejected.map((r:R)=><div className='confirmation-dialog-issue' key={r.id}><b>纸签 {r.paper_tag||'—'} · 第 {r.slot||'—'} 页</b><div>{r.reasons.join('；')}</div><button disabled={busy} onClick={()=>{Plan(null);open(r.id)}}>定位处理</button></div>)}</div>
  {busy&&progress&&<div role='status' aria-live='polite'><b>{progress.current?.paper_tag?`纸签 ${progress.current.paper_tag} · 第 ${progress.current.slot} 页 · `:''}{stageLabel(progress.stage)} · {progress.completed} / {progress.total} 张</b><progress aria-label='批量复核进度' max={progress.total||1} value={progress.completed||0}/></div>}
  {uncertain&&<button disabled={busy} onClick={()=>execute(uncertain)}>查询并继续本次操作</button>}
 </ConfirmationDialog>}
 {feedback&&<div className='batch-review-feedback' role='status'><b>操作结果：已完成 {feedback.accepted?.length||0} 张，拒绝 {feedback.rejected?.length||0} 张</b><p>{feedback.action==='confirm'?'仅确认没有待核实和照片问题的已处理页面，不会将未知答案改成正确。':'取消或恢复复核状态会保留教师判定与历史，统计随之更新。'}</p>{(feedback.rejected||[]).map((r:R)=><p key={r.id}>纸签 {r.paper_tag||'—'}／第{r.slot||'—'}页：{r.reasons.join('；')} <button onClick={()=>open(r.id)}>定位处理</button></p>)}</div>}{children(selected,toggle)}</>
}

