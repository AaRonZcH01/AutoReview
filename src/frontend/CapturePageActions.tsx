import {useRef,useState} from 'react';
import {PhotoReplacementButtons} from './PagePhotoReplacement.js';
import {ConfirmationDialog} from './ConfirmationDialog.js';
type R=Record<string,any>;
export function CapturePageActions({page,api,refresh,prepare,tail=false,replacePhoto}:{page:R,api:(p:string,d?:any)=>Promise<any>,refresh:()=>Promise<any>,prepare:()=>void,tail?:boolean,replacePhoto?:(file?:File)=>void}){
 const localOnly=page.capture_delivery_mode==='local';
 const [plan,Plan]=useState<R|null>(null),[busy,Busy]=useState(false),[error,ErrorText]=useState('');
 const intent=useRef<R|null>(null),file=useRef<File|null>(null),opener=useRef<HTMLElement|null>(null);
 const preview=async(action:string)=>{opener.current=document.activeElement as HTMLElement;Busy(true);ErrorText('');intent.current=null;try{const path=action==='tail'?'/capture-sessions/'+page.capture_session_id+'/withdraw-last':'/pages/'+page.id+'/capture-remedy';Plan({...await api(path,{preview:true,frame_id:page.capture_frame_id,action}),action})}catch(e){ErrorText((e as Error).message)}finally{Busy(false)}};
 const execute=async()=>{if(!plan||busy)return;Busy(true);ErrorText('');try{
  let data=intent.current;if(!data){let asset_id;if(plan.action==='replace'){if(!file.current)throw new globalThis.Error('请先选择正确照片。');const body=new FormData();body.append('file',file.current);asset_id=(await api('/assets',body)).id}data={request_id:crypto.randomUUID(),frame_id:page.capture_frame_id,fingerprint:plan.fingerprint,action:plan.action,asset_id,cloud_consent:plan.action==='resume'&&!localOnly};intent.current=data}
  await api(plan.action==='tail'?'/capture-sessions/'+page.capture_session_id+'/withdraw-last':'/pages/'+page.id+'/capture-remedy',data);Plan(null);intent.current=null;await refresh();if(plan.action==='tail')dispatchEvent(new Event('autoreview:capture-position-changed'));
 }catch(e){ErrorText((e as Error).message);if((e as R).status!==0)intent.current=null}finally{Busy(false)}};
 return <div className='capture-page-actions'>
  {page.capture_withheld?<><button disabled={busy} onClick={()=>preview('resume')}>{localOnly?'恢复本地照片（待批量提交）…':'重新提交本页（重做）…'}</button>{!page.preparation?.confirmed&&<button disabled={busy} onClick={prepare}>核对替换后的照片 →</button>}</>:<button disabled={busy} onClick={()=>preview('hold')}>{localOnly?'暂不处理本页…':'撤回本页识别…'}</button>}
  {replacePhoto?<PhotoReplacementButtons disabled={busy} open={replacePhoto}/>:<label className='button'>选择正确照片替换…<input hidden type='file' accept='image/*' disabled={busy} onChange={e=>{file.current=e.target.files?.[0]||null;e.target.value='';if(file.current)preview('replace')}}/></label>}
  {tail&&<button disabled={busy} onClick={()=>preview('tail')}>撤回最近照片并回退编号…</button>}
  {error&&!plan&&<p role='alert' className='message error'>{error}</p>}
  {plan&&<ConfirmationDialog returnFocus={opener.current} title={plan.action==='resume'?'重新提交本页识别？':plan.action==='tail'?'撤回最近照片并回退编号？':plan.action==='replace'?'替换本页照片？':'撤回本页识别？'} busy={busy} error={error} tone={plan.action==='resume'?'primary':'danger'} onCancel={()=>{Plan(null);intent.current=null;ErrorText('')}} onConfirm={execute} confirmLabel={'确认'+(plan.action==='resume'?'重新提交':plan.action==='replace'?'替换照片':plan.action==='tail'?'撤回并回退编号':'撤回识别')}><b className='confirmation-dialog-subject'>纸签 {plan.paper_tag} · 第 {plan.slot} 页</b><p>{plan.action==='resume'?(localOnly?'恢复本地待提交状态；不会发送云端，请稍后从图库批量提交。':'重新发送本页及必要跨页图片到原冻结账户，使用流式识别槽位和账户额度。'):plan.action==='tail'?'撤回最近照片并安全回退编号。':'撤回相关识别，保留纸签和页位，后续照片编号与顺序保持。'}{plan.dependent_pages>0?` ${plan.dependent_pages} 个跨页关联页面也需重新等待。`:''}{plan.action==='replace'?'替换后先核对图片与作答区，再手动重新提交。':''}{plan.discard_current_draft?'当前未确认草稿将退出检查，需按正确页位重拍。':''}</p><p><small>已发生的云端调用、额度和历史不能撤销。</small></p></ConfirmationDialog>}
 </div>;
}
