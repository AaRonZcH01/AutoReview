import {useEffect,useState} from 'react';
import {ReferenceEditor} from './ReferenceEditor.js';
type R=Record<string,any>;
export function versionName(detail:R|null,id?:string){const version=[...(detail?.answer_versions||[]),...(detail?.hidden_answer_versions||[]),...(detail?.reference_versions||[])].find(v=>v.id===id);return version?(version.name?new RegExp('v\\s*'+version.version+'$','i').test(version.name.trim())?version.name:version.name+' · v'+version.version:'答案 v'+version.version):'版本信息未保存'}
export function ReferenceVersionPreview({detail,id,api,close}:{detail:R,id:string,api:(p:string,d?:any)=>Promise<any>,close:()=>void}){
 const [opened,Open]=useState(false);
 const version=[...(detail.answer_versions||[]),...(detail.hidden_answer_versions||[]),...(detail.reference_versions||[])].find(v=>v.id===id);
 useEffect(()=>{const key=(e:KeyboardEvent)=>{if(e.key==='Escape')close()};window.addEventListener('keydown',key);return()=>window.removeEventListener('keydown',key)},[close]);
 return <div className='editor-overlay history-viewer' role='dialog' aria-label='只读答案版本预览'><div className='history-banner'><b>{id===detail.task.reference_version_id?'正式版本快照':'历史版本'} · 只读 · {versionName(detail,id)}</b><button onClick={close}>关闭版本预览</button></div>{!opened?<div className='version-action-dialog'><p>将查看此次批改所用答案快照，当前草稿与正式版本保持原状。</p><button className='primary' onClick={()=>Open(true)}>打开只读版本详情</button></div>:version?<ReferenceEditor readOnly draft={{...version,id:detail.task.id,asset_ids:version.asset_ids||[],items:version.items||[]}} api={api} change={()=>{}} review={async()=>{}}/>:<p>该记录未保存答案版本，无法预览。</p>}</div>
}
