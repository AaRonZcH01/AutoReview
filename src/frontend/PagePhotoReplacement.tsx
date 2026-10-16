import {useEffect,useRef,useState} from 'react';
import {CameraCapture} from './ReferenceCamera.js';
import {PreparationPanel} from './PreparationPanel.js';
type R=Record<string,any>;
export function PhotoReplacementButtons({open,disabled=false}:{open:(file?:File)=>void,disabled?:boolean}){return <><label className='button'>选择正确照片替换…<input hidden type='file' accept='image/*' disabled={disabled} onChange={e=>{const file=e.target.files?.[0];e.target.value='';if(file)open(file)}}/></label><button disabled={disabled} onClick={()=>open()}>相机现场重拍…</button></>}
export function PagePhotoReplacement({page,detail,file,api,refresh,close,onSaving}:{page:R,detail:R,file?:File,api:(path:string,data?:any)=>Promise<any>,refresh:()=>Promise<any>,close:()=>void,onSaving:(value:boolean)=>void}){
 const localOnly=page.capture_delivery_mode==='local';
 const [plan,Plan]=useState<R|null>(null),[error,ErrorText]=useState(''),[saving,Saving]=useState(false),[url,Url]=useState('');
 const [stage,Stage]=useState<'photo'|'inspection'|'cloud'>('photo'),[inspection,Inspection]=useState<R|null>(null),[cloud,Cloud]=useState<R|null>(null);
 const submission=useRef<R|null>(null),queued=useRef(false);
 const intent=useRef<R|null>(null),upload=useRef<{file:File,asset?:R}|null>(null),applied=useRef(false),locked=useRef(false),host=useRef<HTMLDivElement>(null),live=useRef(true);
 const sub=detail.submissions.find((s:R)=>s.id===page.parent_id),label=`纸签 ${sub?.paper_tag} · 第 ${page.slot}/${sub?.page_count} 页`;
 const load=()=>api('/pages/'+page.id+'/replace',{preview:true}).then(p=>{if(live.current)Plan(p);return p});
 useEffect(()=>{live.current=true;load().catch(e=>{if(live.current)ErrorText(e.message)});host.current?.focus();return()=>{live.current=false}},[page.id]);
 useEffect(()=>{if(!file)return;const value=URL.createObjectURL(file);Url(value);return()=>URL.revokeObjectURL(value)},[file]);
 const warning=plan?`${plan.dependent_pages?`另有 ${plan.dependent_pages} 个跨页关联页面需要重新识别。`:''}${plan.reviewed_pages||plan.teacher_modified_pages?`受影响页面中有 ${plan.reviewed_pages} 页已复核、${plan.teacher_modified_pages} 页有教师修改；确认替换后这些当前结论将失效，原结论保留在不可变历史中。`:'原图和已发生的调用记录保留，当前结果在替换后重新处理。'}请核对 ${label}，确认后只保存本地替换，不发送云端。`:'正在核查替换影响…';
 const save=async(photo:File)=>{
  if(locked.current)return;locked.current=true;Saving(true);onSaving(true);ErrorText('');
  try{
   if(!applied.current){
    if(!plan)throw new Error('正在核查替换影响，请稍候再确认。');
    if(!intent.current){
     if(upload.current?.file!==photo)upload.current={file:photo};
     if(!upload.current.asset){const body=new FormData();body.append('file',photo);upload.current.asset=await api('/assets',body)}
     const fresh=await load();if(fresh.fingerprint!==plan.fingerprint)throw new Error('照片或教师结论已变化。已读取最新替换影响，请再次核对后确认。');
     intent.current={request_id:crypto.randomUUID(),asset_id:upload.current.asset!.id,revision:fresh.revision,fingerprint:fresh.fingerprint,replacement_ack:true};
    }
    try{await api('/pages/'+page.id+'/replace',intent.current)}catch(e){if((e as R).code!=='REVISION_CONFLICT')throw e;const fresh=await load();if(fresh.fingerprint!==intent.current!.fingerprint)throw e;intent.current={...intent.current,revision:fresh.revision,request_id:crypto.randomUUID()};await api('/pages/'+page.id+'/replace',intent.current)}applied.current=true;
   }
   const fresh=await api('/tasks/'+page.task_id);Inspection(fresh);Stage('inspection');await refresh();
  }catch(e){if((e as R).status!==0&&!applied.current){intent.current=null;await load().catch(()=>{})}throw e}
  finally{locked.current=false;if(live.current)Saving(false);onSaving(false)}
 };
 const acceptLocal=async()=>{if(!file||locked.current||!plan)return;try{await save(file)}catch(e){if(live.current)ErrorText((e as Error).message)}};
 const checked=async()=>{if(localOnly){const plan=await api('/pages/'+page.id+'/capture-remedy',{action:'resume',preview:true});await api('/pages/'+page.id+'/capture-remedy',{action:'resume',fingerprint:plan.fingerprint,request_id:crypto.randomUUID()});await refresh();close();return}const [fresh,state,account,sessions]=await Promise.all([api('/tasks/'+page.task_id),api('/state'),api('/account'),page.capture_session_id?api('/tasks/'+page.task_id+'/capture-sessions'):Promise.resolve([])]);Inspection(fresh);const session=sessions.find((x:R)=>x.id===page.capture_session_id);if(page.capture_session_id&&!session)throw new Error('原采集任务不存在，请返回图库查看。');const config=session?.config||state.settings;Cloud({config,account:session?.account||account.account,stream:!!session});Stage('cloud')};
 const submit=async()=>{if(locked.current||!cloud)return;locked.current=true;Saving(true);onSaving(true);ErrorText('');try{
  if(!queued.current){if(!submission.current){if(page.capture_session_id){const preview=await api('/pages/'+page.id+'/capture-remedy',{action:'resume',preview:true});submission.current={path:'/pages/'+page.id+'/capture-remedy',data:{action:'resume',fingerprint:preview.fingerprint,cloud_consent:true,request_id:crypto.randomUUID()}}}else{submission.current={path:'/tasks/'+page.task_id+'/runs',data:{purpose:'student',group_ids:[page.group_id],page_ids:(plan?.affected_page_ids||[page.id]),cloud_consent:true,request_id:crypto.randomUUID()}}}}await api(submission.current.path,submission.current.data);queued.current=true}
  await refresh();close();
 }catch(e){if((e as R).status!==0&&!queued.current)submission.current=null;ErrorText((e as Error).message)}finally{locked.current=false;if(live.current)Saving(false);onSaving(false)}};
 const commands=useRef({acceptLocal,submit});commands.current={acceptLocal,submit};
 useEffect(()=>{if(stage!=='inspection')host.current?.focus({preventScroll:true})},[stage]);
 useEffect(()=>{const key=(e:KeyboardEvent)=>{
  if(!host.current?.contains(e.target as Node))return;
  if(stage==='inspection')return;
  if(e.key==='Escape'){e.preventDefault();e.stopImmediatePropagation();if(!locked.current)close();return}
  if((stage==='cloud'||file)&&e.key==='Enter'&&!e.repeat&&!e.isComposing&&!e.ctrlKey&&!e.metaKey&&!e.altKey&&!(e.target as HTMLElement).closest('input,textarea,select,button,a')){e.preventDefault();e.stopImmediatePropagation();if(stage==='cloud')commands.current.submit();else commands.current.acceptLocal()}
 };document.addEventListener('keydown',key,true);return()=>document.removeEventListener('keydown',key,true)},[!!file,stage]);
 return <div className='page-photo-replacement' ref={host} tabIndex={-1}>
  {error&&<p className='message error' role='alert'>{error}<button disabled={saving} onClick={()=>load().catch(e=>ErrorText(e.message))}>重新读取替换影响</button></p>}
  {stage==='inspection'&&inspection?<PreparationPanel pages={inspection.pages.filter((p:R)=>(plan?.affected_page_ids||[page.id]).includes(p.id))} detail={inspection} referenceVersionId={page.capture_session_id?page.reference_version_id:undefined} api={api} refresh={refresh} close={close} onComplete={checked}/>:stage==='cloud'&&cloud?<section className='replacement-cloud-confirm' role='alertdialog' aria-label='替换照片提交云端确认'><h3>检查已完成，是否提交云端重新识别？</h3><p><strong>{label}</strong>{(plan?.dependent_pages||0)>0&&`，以及 ${plan!.dependent_pages} 个受影响的关联页面`}。</p><p>将照片及必要跨页图片发送至 <b>{cloud.account?.email||'当前已登录账户'}</b>，使用该账户额度；原图和处理历史保留。</p><div className='replacement-cloud-policy'><b>{cloud.config.conservative_review?'保守复看已开启':'保守复看已关闭'}</b><span>{cloud.stream?'沿用原采集任务冻结设置，当前全局开关不改变本页重做。':'本次使用当前批处理设置。'}</span><small>{cloud.config.model} · {cloud.config.effort==='high'?'高':cloud.config.effort==='low'?'低':'中等'}推理</small></div><div className='actions wrap'><button disabled={saving} onClick={close}>{queued.current?'关闭（已提交云端）':'仅保存本地，暂不提交'}</button><button disabled={saving} onClick={()=>Stage('inspection')}>返回检查</button><button disabled={saving} className='primary' onClick={submit}>{saving?'正在确认提交…':queued.current?'刷新处理状态':'确认提交云端重新识别'} <kbd>Enter</kbd></button></div></section>:file?<><h3>{label}</h3><p className='stream-check-reminder'>{warning}</p><img className='replacement-local-preview' src={url} alt='待替换的本地作业照片'/><div className='actions wrap'><button disabled={saving} onClick={close}>{applied.current?'返回图库（照片已替换）':'取消，保留原照片'}</button><button className='primary' disabled={saving||!plan} onClick={acceptLocal}>{saving?'正在保存替换…':applied.current?'继续检查替换照片':'确认替换并检查题区'} <kbd>Enter</kbd></button></div></>:<CameraCapture purpose='replacement' targetLabel={label} warning={warning} confirmDisabled={!plan} add={save} closeAfterAdd={false} close={close}/>}
 </div>;
}
