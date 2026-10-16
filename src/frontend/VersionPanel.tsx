import {useEffect,useState} from 'react';
import {ReferenceEditor} from './ReferenceEditor.js';

type R=Record<string,any>;
export function VersionPanel({detail,api,refresh,save}:{detail:R,api:(p:string,d?:any)=>Promise<any>,refresh:()=>Promise<any>,save:()=>Promise<any>}){
 const [selected,S]=useState<R|null>(null),[preview,P]=useState<R|null>(null),[notice,N]=useState(''),[error,E]=useState(''),[busy,B]=useState(false),[pending,Pending]=useState<R|null>(null);
 const versions=detail.answer_versions||detail.reference_versions||[],hidden=detail.hidden_answer_versions||[];
 const official=(v:R)=>v.id===detail.task.reference_version_id;
 const run=async(fn:()=>Promise<any>)=>{B(true);E('');N('');try{await fn()}catch(e){E((e as Error).message)}finally{B(false)}};
 const send=(v:R,data:R)=>api('/tasks/'+detail.task.id+'/versions/'+v.id,data);
 const close=()=>{if(!busy){Pending(null);P(null);E('')}};
 useEffect(()=>{if(!pending&&!preview)return;const escape=(e:KeyboardEvent)=>{if(e.key==='Escape'&&!busy){Pending(null);P(null);E('')}};window.addEventListener('keydown',escape);return()=>window.removeEventListener('keydown',escape)},[pending,preview,busy]);
 useEffect(()=>{S(null);Pending(null);P(null);N('');E('')},[detail.task.id]);
 const activate=(v:R)=>run(async()=>{await save();P({...await send(v,{action:'preview'}),version:v})});
 const confirm=()=>run(async()=>{
  if(!pending)return;
  if(pending.action==='view'){S(pending.version);Pending(null);return}
  await send(pending.version,{action:pending.action,name:pending.name,confirmed:true,fingerprint:pending.fingerprint});
  const message=pending.action==='rename'?'版本名称已保存。':pending.action==='delete'?'版本已移入版本回收站。':'版本已彻底删除。';
  Pending(null);await refresh();N(message);
 });
 const dialog=pending||preview;
 return <details className='version-panel'><summary>答案版本管理 · {versions.length} 个版本</summary>
  {versions.length===0&&<p className='muted'>确认答案后将生成首个正式版本。</p>}
  {versions.map((v:R)=><div className='version-row' key={v.id}>
   <div className='version-identity'><b>{v.name||'答案 v'+v.version}</b><small className='version-status'>{official(v)?'当前正式版本':'历史版本 · 只读'} · {(v.items||[]).length}题 / {v.page_count}页</small>{official(v)&&<small className='version-protection'>正式版本正在使用；切换到其他版本后才能移除。</small>}</div>
   <div className='version-actions'><button disabled={busy} onClick={()=>{E('');Pending({action:'view',version:v})}}>查看详情</button><button disabled={busy} onClick={()=>{E('');Pending({action:'rename',version:v,name:v.name||'答案 v'+v.version})}}>重命名</button><button disabled={busy} onClick={()=>run(async()=>Pending({action:'delete',version:v,...await send(v,{action:'delete_preview'})}))}>移入版本回收站</button>{!official(v)&&<button className='primary' disabled={busy} onClick={()=>activate(v)}>设为正式版本…</button>}</div>
  </div>)}
  <details className='version-recycle'><summary>版本回收站 · {hidden.length}</summary>
   {hidden.length===0&&<p className='muted'>没有已移除的答案版本。</p>}
   {hidden.map((v:R)=><div className='version-row' key={v.id}><div className='version-identity'><b>{v.name||'答案 v'+v.version}</b><small className='version-status'>{v.hidden_at?'移除于 '+new Date(v.hidden_at).toLocaleString():''}</small><small>{v.delete_after?'保留至 '+new Date(v.delete_after).toLocaleDateString():'已有历史引用，保留原始依据'}</small></div><div className='version-actions'><button disabled={busy} onClick={()=>run(async()=>{await send(v,{action:'restore'});await refresh();N('版本已恢复到答案版本列表。')})}>恢复</button><button disabled={busy} onClick={()=>{E('');Pending({action:'view',version:v})}}>只读查看</button><button className='danger' disabled={busy} onClick={()=>run(async()=>Pending({action:'purge',version:v,...await send(v,{action:'purge_preview'})}))}>彻底删除…</button></div></div>)}
  </details>
  {dialog&&<div className='editor-overlay version-confirm-overlay' onMouseDown={e=>{if(e.target===e.currentTarget)close()}}><section className='version-action-dialog' role='dialog' aria-modal='true' aria-labelledby='version-dialog-title'>
   <h3 id='version-dialog-title'>{preview?'设为当前正式版本':pending?.action==='rename'?'重命名答案版本':pending?.action==='delete'?'移入版本回收站':pending?.action==='purge'?'彻底删除答案版本':'查看答案版本详情'}</h3>
   {preview?<><p>将“{preview.version.name||'答案 v'+preview.version.version}”设为正式版本，并载入当前编辑界面。原正式版本成为只读历史，切换前的草稿自动保留；已有批改结果仍使用原答案版本。</p>{preview.scoring_requires_confirmation&&<p>自动计分已开启；切换后需要重新核对并发布对应的分值规则。</p>}{(preview.active_runs.length>0||preview.incompatible_pages)&&<p className='version-blocked'>{preview.active_runs.length>0?'当前有待处理或正在运行的任务，请先停止或等待完成。':'历史页结构与已有作业不一致，请导出答案包并导入新任务。'}</p>}</>:pending?.action==='rename'?<label>版本名称<input autoFocus maxLength={100} value={pending.name} onChange={e=>Pending({...pending,name:e.target.value})} onKeyDown={e=>{if(e.key==='Enter'&&pending.name.trim()&&!busy){e.preventDefault();confirm()}}}/></label>:pending?.action==='view'?<p>将打开“{pending.version.name||'答案 v'+pending.version.version}”的只读快照。当前草稿与正式版本不会因查看而切换。</p>:pending?.action==='delete'?<><p>{pending.behavior}。恢复入口位于“版本回收站”。</p>{pending.blocked_reason&&<p className='version-blocked'>{pending.blocked_reason}</p>}</>:<><p>彻底删除“{pending?.version.name||'答案版本'}”后将无法恢复。原件和其他版本共用的图片保留。</p>{pending?.blocked_reason&&<p className='version-blocked'>{pending.blocked_reason}</p>}{pending?.reference_count>0&&<p>共有 {pending?.reference_count} 条记录引用该版本；请保留这些历史依据。</p>}</>}
   {error&&<p className='version-error' role='alert'>{error}</p>}
   <div className='version-dialog-actions'><button disabled={busy} onClick={close}>取消</button>{preview?<button className='primary' disabled={busy||preview.active_runs.length>0||preview.incompatible_pages} onClick={()=>run(async()=>{await send(preview.version,{action:'activate',fingerprint:preview.fingerprint,confirmed:true});P(null);S(null);await refresh();N('已设为当前正式版本，可继续编辑。')})}>{busy?'正在切换…':'确认切换并编辑'}</button>:<button className={pending?.action==='purge'?'danger':'primary'} disabled={busy||Boolean(pending?.blocked_reason)||(pending?.action==='rename'&&!pending.name.trim())} onClick={confirm}>{busy?'正在处理…':pending?.action==='view'?'打开只读详情':pending?.action==='rename'?'保存名称':pending?.action==='delete'?'确认移入回收站':'确认彻底删除'}</button>}</div>
  </section></div>}
  {selected&&<div className='history-viewer editor-overlay'><div className='history-banner'><b>{official(selected)?'正式版本快照':'历史版本'} · 只读 · {selected.name||'v'+selected.version}</b><button onClick={()=>S(null)}>关闭详情</button>{!official(selected)&&<button className='primary' disabled={busy} onClick={()=>run(async()=>{await save();P({...await send(selected,{action:'preview'}),version:selected});S(null)})}>设为当前正式版本并编辑…</button>}</div>{error&&<p className='version-error' role='alert'>{error}</p>}<div className='history-score'>{(detail.scoring_versions||[]).filter((v:R)=>v.reference_version_id===selected.id).map((v:R)=><span key={v.id}>计分规则 v{v.version} · 总分 {v.declared_total_minor/100} 分</span>)}</div><ReferenceEditor readOnly draft={{...selected,id:detail.task.id,asset_ids:selected.asset_ids||[],items:selected.items,issues:selected.issues||[]}} api={api} change={()=>{}} review={async()=>{}}/></div>}
  {notice&&<p className='version-notice' role='status'>{notice}</p>}{error&&!dialog&&!selected&&<p className='version-error' role='alert'>{error}</p>}
 </details>
}


