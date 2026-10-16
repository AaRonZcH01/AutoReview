import {capturePageStatus} from './capturePageStatus.js';
import {useEffect,useRef,useState} from 'react';
import {GeometryEditor,ImageEditor} from './GeometryEditor.js';
import {captureShortcut,captureReceiptText,captureErrorText,captureCountdownSeconds,CapturePhase} from './streamCaptureModel.js';
import {operationDetail} from './displayText.js';
import './streamCapture.css';
import {stopCamera,closeCameraChannel,connectCamera,waitCameraFrame,photograph,cameraInfo,CameraInfo,photographInfo,cameraChanging,cameraRestoration} from './cameraTransport.js';
import {CameraPreview} from './CameraPreview.js';
import {ConfirmationDialog} from './ConfirmationDialog.js';

type R=Record<string,any>;
const regionCacheKey='autoreview.capture.region-drafts';
function cachedRegions(frame:R){try{const entry=JSON.parse(localStorage.getItem(regionCacheKey)||'{}')[frame.id];return entry?.asset===frame.asset_id&&entry?.stamp===frame.preparation?.stamp?entry.zones:null}catch{return null}}
function keepRegions(frame:R,zones:R|null){try{const entries=JSON.parse(localStorage.getItem(regionCacheKey)||'{}');delete entries[frame.id];if(zones)entries[frame.id]={asset:frame.asset_id,stamp:frame.preparation?.stamp,zones};const bounded=Object.fromEntries(Object.entries(entries).slice(-16));localStorage.setItem(regionCacheKey,JSON.stringify(bounded))}catch{/* The server still retains the original photo if browser storage is unavailable. */}}

type Props={task:string,detail:R,initialGroup:string,api:(path:string,data?:any,method?:string)=>Promise<any>,refresh:()=>Promise<any>,close:()=>void,visible?:boolean,hide?:()=>void,onStatus?:(value:R)=>void};
export function StreamCapture({task,detail,initialGroup,api,refresh,close,visible=true,hide,onStatus}:Props){
 const visibleRef=useRef(visible);visibleRef.current=visible;const focusHost=()=>{if(visibleRef.current)host.current?.focus({preventScroll:true})};
 const [phase,Phase]=useState<CapturePhase>('idle'),[group,Group]=useState(initialGroup||detail.groups[0]?.id||''),[source,Source]=useState<'camera'|'files'>('camera');
 const [session,Session]=useState<R|null>(null),[activeSessions,ActiveSessions]=useState<R[]>([]),[frame,Frame]=useState<R|null>(null),[zones,Zones]=useState<R>({});
 const [files,Files]=useState<File[]>([]),[fileIndex,FileIndex]=useState(0),[devices,Devices]=useState<MediaDeviceInfo[]>([]),[device,Device]=useState('');
 const deviceRef=useRef(device);deviceRef.current=device;
 const [consent,Consent]=useState(false),[editor,Editor]=useState(false),[region,Region]=useState('');
 const [error,SetError]=useState(''),[note,Note]=useState(''),[receipt,Receipt]=useState<R|null>(null),[countdown,Countdown]=useState(2),[account,Account]=useState<R|null>(null);
 const [operation,Operation]=useState(''),[elapsed,Elapsed]=useState(0),[startTag,StartTag]=useState(''),[continuation,Continuation]=useState(''),[discarding,Discarding]=useState('');
 const [delivery,Delivery]=useState('immediate');
 const sessionLocal=session?.delivery_mode==='local';
 const localOnly=(session&& !['idle','stopped'].includes(phase)?session.delivery_mode:delivery)==='local';
 const [mode,Mode]=useState('whole_submission'),[slot,Slot]=useState(1),[cohortId,CohortId]=useState(''),[reverseCohort,ReverseCohort]=useState(false);
 const host=useRef<HTMLElement>(null),video=useRef<HTMLVideoElement>(null),stream=useRef<MediaStream|null>(null),epoch=useRef(0),lock=useRef(false),mounted=useRef(true);
 const phaseRef=useRef(phase),sessionRef=useRef(session),frameRef=useRef(frame),fileIndexRef=useRef(fileIndex),commitIntent=useRef<R|null>(null),captureIntent=useRef<R|null>(null),startIntent=useRef<R|null>(null),appliedFrames=useRef(new Set<string>());
 const recoveryIntent=useRef<R|null>(null);
 const [cameraState,CameraState]=useState('未开启相机'),[cameraReady,CameraReady]=useState(false),[cameraBusy,CameraBusy]=useState(false);
 const cameraEpoch=useRef(0);
 const cameraOwner=useRef(crypto.randomUUID()).current,loan=useRef<{id:string,resume:boolean,returned:boolean}|null>(null);
 const [loanVersion,LoanVersion]=useState(0);
 const [withdrawal,Withdrawal]=useState<R|null>(null),[undoBusy,UndoBusy]=useState(false);const undoIntent=useRef<R|null>(null);
 const [cameraDetails,CameraDetails]=useState<CameraInfo|null>(null),[photoDetails,PhotoDetails]=useState('');
 phaseRef.current=phase;sessionRef.current=session;frameRef.current=frame;fileIndexRef.current=fileIndex;
 const countdownSeconds=captureCountdownSeconds(session);
 const busy=undoBusy||['starting','capturing','preparing','submitting'].includes(phase);
 const selectedGroup=detail.groups.find((g:R)=>g.id===group);
 const unfinishedSessions=activeSessions.filter(s=>s.state==='active'),savedSessions=activeSessions.filter(s=>s.state==='stopped'&&s.saved_frame);
 const reference=detail.reference_versions.find((r:R)=>r.id===(session?.reference_version_id||selectedGroup?.reference_version_id));
 const setupReference=detail.reference_versions.find((r:R)=>r.id===selectedGroup?.reference_version_id),pageCount=setupReference?.page_count||1;
 const selectedCohort=(detail.cohorts||[]).find((c:R)=>c.id===cohortId);
 const cohortIds=selectedCohort?[...selectedCohort.submission_ids]:[];if(reverseCohort)cohortIds.reverse();
 const remainingCohort=cohortIds.filter((sid:string)=>!detail.pages.some((p:R)=>p.parent_id===sid&&p.slot===slot));
 const cohortComplete=mode==='single_page'&&!!cohortId&&remainingCohort.length===0;
 const rosterComplete=session?.state==='active'&&session?.mode==='single_page'&&session?.cohort_submission_ids!=null&&!session.next;
 const phaseLabel:Record<CapturePhase,string>={idle:'准备开始',starting:'正在开始采集',ready:'等待拍摄',capturing:'正在拍摄',preparing:'正在本地准备图片',review:'检查本张照片',submitting:localOnly?'正在确认并保存本地':'正在确认并入队',uncertain:'正在核对提交结果',success:'已确认',stopped:'本轮采集已停止'};
 const currentLabel=rosterComplete?'本名单此页已拍完':phaseLabel[phase];
 const statusCallback=useRef(onStatus);statusCallback.current=onStatus;
 useEffect(()=>{statusCallback.current?.({label:currentLabel,active:session?.state==='active',cameraReady,busy:busy||cameraBusy,
   confirmed:session?.committed_count||0,next:session?.next||null})},[currentLabel,session?.state,session?.committed_count,session?.next?.paper_tag,session?.next?.slot,cameraReady,busy,cameraBusy]);
 const setPhase=(value:CapturePhase)=>{phaseRef.current=value;if(mounted.current)Phase(value)};
 const showFrame=(value:R)=>{frameRef.current=value;Frame(value);Zones(cachedRegions(value)||value.preparation?.zones||{});Region('');commitIntent.current=null;setPhase('review');focusHost()};
 const releaseCamera=()=>{cameraEpoch.current++;closeCameraChannel(cameraOwner);stopCamera(stream.current);stream.current=null;if(video.current)video.current.srcObject=null;if(mounted.current){CameraReady(false);CameraBusy(false);CameraDetails(null);CameraState('相机已关闭')}};
 const enumerateCameras=async()=>{if(!navigator.mediaDevices?.enumerateDevices)return;const rows=(await navigator.mediaDevices.enumerateDevices()).filter(d=>d.kind==='videoinput');if(mounted.current){Devices(rows);if(!stream.current&&!rows.some(d=>d.deviceId===deviceRef.current)){const preferred=rows.find(d=>d.label&&d.label===localStorage.getItem('autoreview.camera.label'));Device(preferred?.deviceId||'')}}};
 const loadSessions=()=>api('/tasks/'+task+'/capture-sessions').then((rows:R[])=>{if(mounted.current)ActiveSessions(rows)});
 useEffect(()=>{mounted.current=true;loadSessions().catch(e=>SetError(captureErrorText(e)));api('/account').then(Account).catch(()=>{});return()=>{mounted.current=false;epoch.current++;releaseCamera();const sid=sessionRef.current?.id;if(sid&&sessionRef.current?.state==='active')api('/capture-sessions/'+sid+'/stop',{}).catch(()=>{})}},[task]);
 useEffect(()=>{if(video.current&&stream.current&&video.current.srcObject!==stream.current)video.current.srcObject=stream.current},[phase]);
 useEffect(()=>{const devices=navigator.mediaDevices;if(!devices)return;const update=()=>enumerateCameras().catch(()=>{});update();devices.addEventListener('devicechange',update);return()=>devices.removeEventListener('devicechange',update)},[]);
 useEffect(()=>{if(!busy){Elapsed(0);return}const start=Date.now();const timer=setInterval(()=>Elapsed(Math.floor((Date.now()-start)/1000)),500);return()=>clearInterval(timer)},[busy,phase]);
 useEffect(()=>{
  if(phase!=='success')return;
  const generation=epoch.current;
  const prepareNext=()=>{if(!mounted.current||generation!==epoch.current||phaseRef.current!=='success'||sessionRef.current?.state!=='active')return;Receipt(null);Frame(null);frameRef.current=null;setPhase('ready');focusHost()};
  Countdown(countdownSeconds);
  if(countdownSeconds===0){prepareNext();return}
  const end=Date.now()+countdownSeconds*1000;
  const timer=setInterval(()=>{const left=Math.max(0,Math.ceil((end-Date.now())/1000));Countdown(left);if(!left){clearInterval(timer);prepareNext()}},100);
  return()=>clearInterval(timer);
 },[phase,receipt?.frame_id,countdownSeconds]);

 const openCamera=async(generation:number,target=device)=>{
  if(!navigator.mediaDevices?.getUserMedia)throw new Error('当前窗口无法访问摄像头，请使用本地图片代替拍摄。');
  releaseCamera();const cameraGeneration=cameraEpoch.current;const current=()=>epoch.current===generation&&cameraEpoch.current===cameraGeneration&&mounted.current;
  CameraBusy(true);CameraState('正在连接相机，请处理相机权限提示');
  let media:MediaStream|null=null;
  try{if(!target){const rows=await navigator.mediaDevices.enumerateDevices();target=rows.find(d=>d.kind==='videoinput'&&d.label&&d.label===localStorage.getItem('autoreview.camera.label'))?.deviceId||'';}if(!current())return false;
   media=await connectCamera(target,current,text=>{if(current())CameraState(text)},cameraOwner);if(!current()){stopCamera(media);return false}
   stream.current=media;const element=video.current;if(!element)throw new Error('相机预览尚未挂载，请重新连接。');element.srcObject=media;
   await waitCameraFrame(element,media,current);if(!current()){stopCamera(media);return false}
   const track=media.getVideoTracks()[0];const settings=track.getSettings();CameraDetails(cameraInfo(element,media));CameraReady(true);CameraState(`${track.label||'摄像头'} · ${element.videoWidth} × ${element.videoHeight}`);Device(settings.deviceId||target);localStorage.setItem('autoreview.camera.label',track.label);localStorage.removeItem('autoreview.camera.device');
   const unavailable=()=>{if(current()&&!cameraChanging(track)){CameraReady(false);CameraState('相机已断开或没有可用画面，请重新连接；已拍照片仍保留。')}};
   track.addEventListener('ended',unavailable);track.addEventListener('mute',unavailable);track.addEventListener('unmute',()=>{if(current()&&media){CameraReady(true);CameraDetails(cameraInfo(element,media));CameraState(`${track.label||'摄像头'} · ${element.videoWidth} × ${element.videoHeight}`)}});
   await enumerateCameras().catch(()=>{});return true;
  }catch(error){stopCamera(media);if(current()){closeCameraChannel(cameraOwner);stream.current=null;CameraReady(false);CameraState(captureErrorText(error))}throw error}
  finally{if(current())CameraBusy(false)}
 };
 const reconnectCamera=async(target=device)=>{if(cameraBusy||busy||loan.current&&!loan.current.returned)return;SetError('');Device(target);try{await openCamera(epoch.current,target)}catch(error){if(mounted.current)SetError(captureErrorText(error))}};
 const leaseCommands=useRef({releaseCamera});leaseCommands.current={releaseCamera};
 useEffect(()=>{
  const borrow=(event:Event)=>{const id=(event as CustomEvent).detail.id;loan.current={id,resume:!!stream.current,returned:false};leaseCommands.current.releaseCamera();CameraState('相机暂用于现场拍摄，结束后恢复原预览')};
  const returned=(event:Event)=>{if(loan.current&&loan.current.id===(event as CustomEvent).detail.id){loan.current.returned=true;LoanVersion(v=>v+1)}};
  addEventListener('autoreview:borrow-camera',borrow);addEventListener('autoreview:return-camera',returned);return()=>{removeEventListener('autoreview:borrow-camera',borrow);removeEventListener('autoreview:return-camera',returned);loan.current=null};
 },[]);
 useEffect(()=>{if(busy||!loan.current?.returned)return;const resume=loan.current.resume;loan.current=null;if(resume)reconnectCamera(deviceRef.current)},[loanVersion,busy]);
 const begin=async(existing?:R)=>{
  if(lock.current)return;if(!existing&&((delivery!=='local'&&!consent)||!group))return;
  const chosenSource=existing?.source||source;
  if(!existing&&chosenSource==='files'&&!files.length){SetError('请先选择代替拍摄的本地图片。');return}
  Source(chosenSource);
  const generation=++epoch.current;lock.current=true;SetError('');Note('');Receipt(null);setPhase('starting');Operation('正在准备采集设备');
  try{
   if(chosenSource==='camera'&&!await openCamera(generation))return;
   if(generation!==epoch.current)return;
   Operation((existing?.delivery_mode||delivery)==='local'?'正在核验答案与本地采集配置':'正在核验答案、账户与采集配置');
   const request=startIntent.current||{request_id:crypto.randomUUID(),group_id:group,source,mode,slot,delivery_mode:delivery,
    start_tag:!cohortId&&!continuation?startTag||undefined:undefined,
    submission_id:mode==='whole_submission'?continuation||undefined:undefined,
    cohort_id:mode==='single_page'?cohortId||undefined:undefined,reverse_cohort:mode==='single_page'&&!!cohortId&&reverseCohort,cloud_consent:delivery!=='local'&&consent};
   startIntent.current=request;
   const value=existing?await api('/capture-sessions/'+existing.id):await api('/tasks/'+task+'/capture-sessions',request);
   if(generation!==epoch.current||!mounted.current){await api('/capture-sessions/'+value.id+'/stop',{});return}
   sessionRef.current=value;
   if(value.state!=='active'){releaseCamera();Session(value);setPhase('stopped');Note('这轮采集已结束；请开始新一轮，缺页作业可选择接续纸签。');loadSessions().catch(()=>{});return}
   const priorFile=existing&&(value.active_frame?.source_filename||value.active_frame?.asset?.filename);
   const priorIndex=priorFile?files.findIndex(f=>f.name===priorFile):-1;
   const committedNames=new Set<string>(value.committed_files||[]);
   const firstUnused=files.findIndex(file=>!committedNames.has(file.name));
   const nextIndex=priorIndex>=0?priorIndex:existing?(firstUnused<0?files.length:firstUnused):0;
   Session(value);Delivery(value.delivery_mode||'immediate');Group(value.group_id);Mode(value.mode||'whole_submission');Slot(value.slot||1);CohortId(value.cohort_submission_ids!=null?value.cohort_id:'');ReverseCohort(!!value.reverse_cohort);StartTag('');Consent(true);FileIndex(nextIndex);fileIndexRef.current=nextIndex;startIntent.current=null;captureIntent.current=null;commitIntent.current=null;Frame(null);frameRef.current=null;loadSessions().catch(()=>{});
   if(value.active_frame)showFrame(value.active_frame);else setPhase('ready');focusHost();
  }catch(e){if(generation===epoch.current){releaseCamera();SetError(captureErrorText(e));setPhase('idle')}}finally{if(generation===epoch.current)lock.current=false}
 };
 const stop=()=>{
  loan.current=null;
  const stoppingEpoch=++epoch.current;lock.current=false;Withdrawal(null);releaseCamera();Editor(false);setPhase('stopped');SetError('');
  Note(commitIntent.current?'采集已停止，正在核对最后一次确认的结果；已确认照片不会撤销。':sessionRef.current?.delivery_mode==='local'?'已停止拍摄。照片保留在本机，可在图库检查后统一提交批处理。':'已停止拍摄。未确认草稿保留在本机；已入队处理继续运行，可在批处理模块查看。');
  const current=sessionRef.current;
  if(current){Session({...current,state:'stopped'});sessionRef.current={...current,state:'stopped'};api('/capture-sessions/'+current.id+'/stop',{}).then(()=>loadSessions()).catch(e=>{if(stoppingEpoch===epoch.current&&mounted.current)SetError('摄像头已停止；后台会话状态尚未确认：'+captureErrorText(e))})}
  if(commitIntent.current&&frameRef.current)api('/capture-frames/'+frameRef.current.id).then(value=>{if(mounted.current&&value.commit_result){if(stoppingEpoch===epoch.current){Receipt(value.commit_result);Note('最后一张照片已确认。停止采集不撤销已入队照片。')}refresh().catch(()=>{})}}).catch(()=>{});
 };
 const recoverSaved=async(saved:R)=>{
  if(lock.current||(delivery!=='local'&&!consent))return;
  const generation=++epoch.current;lock.current=true;SetError('');Note('');Discarding('');setPhase('starting');Operation('正在重新准备保留照片');
  const target=saved.saved_frame;
  let recovered:R|null=null;
  try{
   if(source==='camera'&&!await openCamera(generation))return;
   if(generation!==epoch.current)return;
   const intent=recoveryIntent.current?.frame_id===target.id?recoveryIntent.current:{frame_id:target.id,request_id:crypto.randomUUID(),revision:target.revision,source,delivery_mode:delivery,cloud_consent:delivery!=='local'&&consent};
   recoveryIntent.current=intent;
   try{recovered=await api('/capture-frames/'+target.id+'/recover',intent)}catch(error){
    const original=await api('/capture-frames/'+target.id).catch(()=>null);
    if(!original?.recovered_to_frame_id)throw error;
    const current=await api('/capture-frames/'+original.recovered_to_frame_id);
    recovered={frame:current,session:await api('/capture-sessions/'+current.parent_id)};
   }
   const current=recovered!.session;
   if(generation!==epoch.current||!mounted.current){await api('/capture-sessions/'+current.id+'/stop',{});return}
   sessionRef.current=current;
   recoveryIntent.current=null;startIntent.current=null;captureIntent.current=null;commitIntent.current=null;
   Session(current);Delivery(current.delivery_mode||'immediate');Group(current.group_id);Source(current.source);Mode(current.mode||'whole_submission');Slot(current.slot||1);CohortId(current.cohort_submission_ids!=null?current.cohort_id:'');ReverseCohort(!!current.reverse_cohort);StartTag('');Consent(true);Receipt(null);
   if(current.state!=='active'||recovered!.frame.state!=='draft'){releaseCamera();setPhase('stopped');Note('这张照片已在另一轮继续处理，请查看最新采集状态。');return}
   const index=files.findIndex(file=>file.name===recovered!.frame.source_filename);FileIndex(index>=0?index:files.length);fileIndexRef.current=index>=0?index:files.length;
   showFrame(recovered!.frame);Note(recovered!.session.delivery_mode==='local'?'保留照片已带入新一轮。请重新检查图片与题区，确认后只保存到本地图库。':'保留照片已带入新一轮。请重新检查图片与题区，确认后才会发送；原处理队列保持原状态。');
  }catch(error){if(generation===epoch.current){releaseCamera();SetError(captureErrorText(error));setPhase('idle')}}
  finally{if(generation===epoch.current)lock.current=false;loadSessions().catch(()=>{})}
 };
 const discardSaved=async(saved:R)=>{
  if(lock.current)return;const generation=epoch.current;lock.current=true;UndoBusy(true);SetError('');Operation('正在放弃未确认照片');
  try{await api('/capture-frames/'+saved.saved_frame.id+'/discard',{request_id:crypto.randomUUID(),revision:saved.saved_frame.revision});if(generation===epoch.current){Discarding('');Note('已放弃这张未确认照片。原图和处理历史仍保留。')}await loadSessions()}
  catch(error){if(generation===epoch.current)SetError(captureErrorText(error))}finally{if(generation===epoch.current){lock.current=false;UndoBusy(false)}}
 };
 const acceptFile=async(file:File,generation:number)=>{
  let intent=captureIntent.current;
  if(!intent){intent={capture_id:crypto.randomUUID(),captured_at:new Date().toISOString(),source_filename:frameRef.current?.source_filename||files[fileIndexRef.current]?.name||file.name,file};captureIntent.current=intent}
  if(!intent.asset){Operation('正在保存原图并检查图片方向');const body=new FormData();body.append('file',file);intent.asset=await api('/assets',body)}
  if(generation!==epoch.current)return;
  Operation('正在定位完整答题区域');setPhase('preparing');
  const value=await api('/capture-sessions/'+sessionRef.current!.id+'/frames',{capture_id:intent.capture_id,asset_id:intent.asset.id,captured_at:intent.captured_at,source_filename:intent.source_filename});
  if(generation!==epoch.current)return;
  captureIntent.current=null;showFrame(value);refresh().catch(()=>{});
 };
 const capture=async(replacement?:File)=>{
  if(lock.current||!sessionRef.current||sessionRef.current.state!=='active')return;
  if(stream.current&&cameraChanging(stream.current.getVideoTracks()[0])){Note('清晰照片已拍摄，正在恢复流畅预览，请稍候。');return}
  if(loan.current&&!loan.current.returned){SetError('相机正在现场拍摄窗口使用，请先完成或关闭该窗口。');return}
  if(!sessionRef.current.next){SetError('本名单此页已全部拍完，请停止采集后选择其他页码或名单。');return}
  const generation=epoch.current;lock.current=true;SetError('');Note('');setPhase('capturing');Operation('正在拍摄');
  try{
   let file=replacement||captureIntent.current?.file;
   if(!file&&source==='files')file=files[fileIndexRef.current];
   if(!file&&source==='files')throw new Error('所选本地图片已全部采集，请点击“选择接下来要拍摄的图片”继续，或结束本轮。');
   if(!file){
    if(cameraBusy||!video.current||!stream.current)throw new Error('摄像头尚未连接，请点击重新连接。');
    Operation('正在等待相机画面');
    await waitCameraFrame(video.current,stream.current,()=>generation===epoch.current&&mounted.current);
    file=await photograph(video.current,stream.current,()=>generation===epoch.current&&mounted.current);
    const info=photographInfo(file);if(info)PhotoDetails(`${info.width} × ${info.height} · ${info.method}`);if(info?.previewRestored===false){CameraReady(false);CameraState('照片已保留；相机预览恢复失败，请重新连接')}
    const target=stream.current,restoration=cameraRestoration(target);if(restoration){CameraReady(false);CameraState('照片已拍摄，正在恢复流畅预览');restoration.then(ok=>{if(generation===epoch.current&&stream.current===target&&mounted.current){CameraReady(ok);CameraState(ok?'相机已恢复流畅预览':'照片已保留；相机预览恢复失败，请重新连接');if(ok&&video.current)CameraDetails(cameraInfo(video.current,target))}})}
   }
   await acceptFile(file,generation);
  }catch(e){if(generation===epoch.current){SetError(captureErrorText(e));setPhase(frameRef.current?'review':'ready')}}finally{if(generation===epoch.current)lock.current=false}
 };
 const updateAsset=async(asset:R)=>{
  if(!frameRef.current||asset.id===frameRef.current.asset_id)return;
  const generation=epoch.current;lock.current=true;setPhase('preparing');Operation('正在保存校正图并重新定位');SetError('');
  const old=frameRef.current;
  try{const value=await api('/capture-frames/'+old.id,{asset_id:asset.id,revision:old.revision,request_id:crypto.randomUUID()},'PUT');if(generation===epoch.current)showFrame(value)}
  catch(e){if(generation===epoch.current){SetError(captureErrorText(e));setPhase('review')}}finally{if(generation===epoch.current)lock.current=false}
 };
 const applied=async(value:R,generation:number)=>{
  if(!mounted.current)return;
  if(generation!==epoch.current){if(phaseRef.current==='stopped'){Receipt(value);Note('采集已停止；最后一张已确认照片保留在队列中。')}refresh().catch(()=>{});return}
  if(frameRef.current)keepRegions(frameRef.current,null);Receipt(value);commitIntent.current=null;captureIntent.current=null;
  const firstApplication=!appliedFrames.current.has(value.frame_id);
  if(firstApplication){FileIndex(index=>index+1);fileIndexRef.current++;appliedFrames.current.add(value.frame_id)}
  const current={...sessionRef.current,next:value.next,active_frame_id:null,committed_count:(sessionRef.current?.committed_count||0)+(firstApplication?1:0),last_committed_frame_id:value.frame_id};Session(current);sessionRef.current=current;
  Countdown(captureCountdownSeconds(current));setPhase('success');Note(captureReceiptText(value));refresh().catch(e=>SetError(captureErrorText(e)));focusHost();
 };
 const confirm=async()=>{
  if(lock.current||!frameRef.current||phaseRef.current!=='review'||editor)return;
  const generation=epoch.current;lock.current=true;SetError('');setPhase('submitting');Operation(localOnly?'正在保存本张照片到本地图库':'正在保存本张照片并加入处理队列');
  const value=frameRef.current;
  const intent=commitIntent.current||{request_id:crypto.randomUUID(),revision:value.revision,preparation_stamp:value.preparation.stamp,zones,images_prepared:true,boundary_ack:true,cloud_consent:sessionRef.current?.delivery_mode!=='local'};
  commitIntent.current=intent;
  try{await applied(await api('/capture-frames/'+value.id+'/commit',intent),generation)}
  catch(e){if(generation===epoch.current){
   try{const current=await api('/capture-frames/'+value.id);if(current.commit_result){await applied(current.commit_result,generation);return}}
   catch{}
   SetError(captureErrorText(e));if((e as R).status===0||['RECOVERY_REQUIRED','RECOVERY_RETRY'].includes((e as R).code))setPhase('uncertain');else{commitIntent.current=null;setPhase('review')}
  }}finally{if(generation===epoch.current)lock.current=false}
 };
 const queryCommit=async()=>{
  if(lock.current||!frameRef.current)return;lock.current=true;const generation=epoch.current;SetError('');
  try{const current=await api('/capture-frames/'+frameRef.current.id);if(generation!==epoch.current||!mounted.current)return;if(current.commit_result)await applied(current.commit_result,generation);else if(current.state==='draft'&&sessionRef.current?.state==='active'){setPhase('review');Note('已读取：本张尚未确认。可再次确认，沿用同一操作标识，不会重复入库。')}else{releaseCamera();setPhase('stopped');Note('本轮采集已结束，本张未确认。')}}
  catch(e){if(generation===epoch.current&&mounted.current)SetError(captureErrorText(e))}finally{if(generation===epoch.current)lock.current=false}
 };
 const queryState=async()=>{
  if(lock.current||!sessionRef.current)return;const generation=epoch.current;lock.current=true;SetError('');
  try{const current=await api('/capture-sessions/'+sessionRef.current.id);if(generation!==epoch.current||!mounted.current)return;Session(current);sessionRef.current=current;
   if(current.state==='active'&&current.active_frame)showFrame(current.active_frame);
   else if(current.state!=='active'){releaseCamera();setPhase('stopped')}
   else{Frame(null);frameRef.current=null;setPhase('ready')}
   Note(current.delivery_mode==='local'?'已读取本轮最新状态；检查当前照片后可确认保存到本地图库。':'已读取本轮最新状态；仍需检查当前照片后才能发送入队。');
  }catch(e){if(generation===epoch.current&&mounted.current)SetError(captureErrorText(e))}finally{if(generation===epoch.current)lock.current=false}
 };
 const previewWithdrawal=async()=>{
  if(lock.current||!sessionRef.current?.last_committed_frame_id)return;lock.current=true;UndoBusy(true);Operation('正在核验撤回影响');SetError('');
  try{const value=await api('/capture-sessions/'+sessionRef.current.id+'/withdraw-last',{preview:true,frame_id:sessionRef.current.last_committed_frame_id});Withdrawal(value);undoIntent.current=null;focusHost()}
  catch(e){SetError(captureErrorText(e))}finally{lock.current=false;UndoBusy(false)}
 };
 const withdraw=async()=>{
  if(lock.current||!withdrawal||!sessionRef.current)return;const generation=epoch.current;lock.current=true;UndoBusy(true);Operation('正在撤回照片并恢复纸签与页位');SetError('');
  const intent=undoIntent.current||{frame_id:withdrawal.frame_id,fingerprint:withdrawal.fingerprint,request_id:crypto.randomUUID()};undoIntent.current=intent;
  try{const value=await api('/capture-sessions/'+sessionRef.current.id+'/withdraw-last',intent);if(mounted.current&&generation===epoch.current){if(frameRef.current)keepRegions(frameRef.current,null);Session(value.session);sessionRef.current=value.session;Frame(null);frameRef.current=null;Receipt(null);captureIntent.current=null;commitIntent.current=null;undoIntent.current=null;Withdrawal(null);FileIndex(index=>Math.max(0,index-1));fileIndexRef.current=Math.max(0,fileIndexRef.current-1);setPhase(value.session.state==='active'?'ready':'stopped');Note(`已撤回纸签 ${value.paper_tag} 第 ${value.slot} 页，可按原编号重拍。已发生的云端调用与用量保留。`);focusHost()}await refresh();await loadSessions()}
  catch(e){if(mounted.current){SetError(captureErrorText(e));if((e as R).status!==0){undoIntent.current=null;Withdrawal(null)}}}finally{lock.current=false;if(mounted.current)UndoBusy(false)}
 };
 const commands=useRef({stop,capture,confirm,previewWithdrawal,withdraw});commands.current={stop,capture,confirm,previewWithdrawal,withdraw};
 useEffect(()=>{const halt=(event:Event)=>{if((event as CustomEvent).detail?.task===task)commands.current.stop()};addEventListener('autoreview:stop-capture',halt);return()=>removeEventListener('autoreview:stop-capture',halt)},[task]);
 useEffect(()=>{const key=(event:KeyboardEvent)=>{
  if(!visibleRef.current||!host.current?.contains(event.target as Node)||document.querySelector('.veil,dialog[open]'))return;
  const el=event.target as HTMLElement;const editable=!!el.closest('input,textarea,select,button,a,[contenteditable=true]')||editor;
  if(!editable&&(event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='z'&&!event.shiftKey){event.preventDefault();event.stopImmediatePropagation();if(!withdrawal&&['ready','success','stopped'].includes(phaseRef.current))commands.current.previewWithdrawal();return}
  if(withdrawal&&event.key!=='Escape'){if(event.key==='Enter'&&!event.repeat&&!event.isComposing&&!editable){event.preventDefault();event.stopImmediatePropagation();commands.current.withdraw()}return}
  const action=captureShortcut(phaseRef.current,event,editable);if(!action)return;event.preventDefault();event.stopImmediatePropagation();
  if(action==='stop')commands.current.stop();else if(action==='capture')commands.current.capture();else commands.current.confirm();
 };document.addEventListener('keydown',key,true);return()=>document.removeEventListener('keydown',key,true)},[editor,withdrawal]);

 useEffect(()=>{if(visible)focusHost()},[visible]);
 useEffect(()=>{
  if(!visible||editor||!['ready','review','success'].includes(phase))return;
  let settled=0;const tick=requestAnimationFrame(()=>{settled=requestAnimationFrame(()=>{
   const target=host.current?.querySelector<HTMLElement>(phase==='review'?'.stream-review-body .image-viewport':phase==='ready'?'.stream-camera-stage':phase==='success'?'.stream-success':'.stream-status');
   const panel=host.current?.closest<HTMLElement>('.stream-workspace-panel');if(!target||!panel)return;
   const header=host.current?.querySelector('.stream-header')?.getBoundingClientRect().height||0;
   const bounds=target.getBoundingClientRect(),available=panel.clientHeight-header-24;
   panel.scrollTo({top:Math.max(0,panel.scrollTop+bounds.top-panel.getBoundingClientRect().top-header-12-(phase==='review'?Math.max(0,(available-bounds.height)/2):0)),behavior:'instant'});
  })});return()=>{cancelAnimationFrame(tick);cancelAnimationFrame(settled)};
 },[phase,frame?.id,frame?.asset_id,visible,editor]);
 useEffect(()=>{const tick=()=>{refresh().catch(()=>{});loadSessions().catch(()=>{})};const timer=setInterval(tick,2000);return()=>clearInterval(timer)},[task,refresh]);
 useEffect(()=>{const updated=()=>{if(!lock.current&&sessionRef.current)queryState().catch(()=>{})};addEventListener('autoreview:capture-position-changed',updated);return()=>removeEventListener('autoreview:capture-position-changed',updated)},[]);
 useEffect(()=>{const updated=activeSessions.find(s=>s.id===sessionRef.current?.id);if(updated?.stop_reason&&updated.state==='stopped'&&sessionRef.current?.state==='active'){epoch.current++;lock.current=false;releaseCamera();Session(updated);sessionRef.current=updated;setPhase('stopped');SetError(updated.stop_reason)}},[activeSessions]);
 const boxes=Object.entries(zones).filter(([,box])=>box).map(([id,box])=>({id,box,label:frame?.questions?.find((q:R)=>q.id===id)?.number||'题目'}));
 const cameraIdentity=<div className='stream-photo-identity' role='status'>{session?.state==='active'&&session.next?<><span>本张待拍</span><strong>纸签 {session.next.paper_tag}</strong><b>第 {session.next.slot} / {session.page_count} 页</b><small>{phase==='capturing'?'正在拍摄清晰照片，请保持试卷不动':phase==='ready'?'放好此纸签对应页面后，按 Enter 拍摄':currentLabel}</small></>:<b>{rosterComplete?'本名单此页已拍完':'相机预览 · 开始采集后显示本张纸签和页码'}</b>}</div>;
 const cameraActions=phase==='ready'&&<div className='stream-ready-actions'>{source==='files'&&!rosterComplete&&<label className='button stream-add-files'>选择接下来要拍摄的图片<input type='file' accept='image/*' multiple hidden onChange={e=>{Files(Array.from(e.target.files||[]).sort((a,b)=>a.name.localeCompare(b.name,undefined,{numeric:true})));FileIndex(0);fileIndexRef.current=0;captureIntent.current=null;e.target.value='';focusHost()}}/></label>}<button className='primary' disabled={busy||rosterComplete||(source==='files'?!files[fileIndex]:!cameraReady||cameraBusy)} onClick={()=>capture()}>拍摄本张 <kbd>Enter</kbd></button><p>{rosterComplete?'本名单此页已拍完，请停止采集后另开一轮。':'请放好当前纸签与对应页面，画面稳定后拍摄。'}</p></div>;
 const canConfirm=phase==='review'&&!editor&&!undoBusy&&!withdrawal;
 const savedToDiscard=savedSessions.find(value=>value.saved_frame.id===discarding);
 return <section className='stream-capture' ref={host} tabIndex={-1} aria-label='流式拍摄工作区'>
  <header className='stream-header'><div><h2>流式拍摄</h2><p>拍摄 → 检查 → {localOnly?'保存本地，稍后批量提交':'确认入队'}。下一张由你按 Enter 拍摄。</p></div><div className='actions'>{hide&&<button onClick={hide}>隐藏拍摄面板</button>}<button className='stream-stop' onClick={stop}>停止采集 <kbd>Esc</kbd></button><button onClick={()=>{stop();close()}}>结束并关闭面板</button></div></header>
  <div className='stream-status' role='status' aria-live='polite'><b>{currentLabel}</b>{session?.next&&<span>下一张：纸签 {session.next.paper_tag} · 第 {session.next.slot}/{session.page_count} 页</span>}{session&&<><span>{session.mode==='single_page'?`单面 · 固定第 ${session.slot} 页${session.cohort_submission_ids!=null?` · 名单${session.reverse_cohort?'倒序':'正序'}补页`:' · 新名单'}`:'整份 · 逐页拍摄'} · 已确认 {session.committed_count} 张</span><small>{sessionLocal?'本轮仅保存本地，不发送云端':`本轮账户：${session.account?.email||session.config?.profile_id||'未提供'} · ${session.config?.model}`} · 确认后等待 {countdownSeconds} 秒</small></>}{busy&&<span>{operation} · 已用 {elapsed} 秒</span>}{phase==='success'&&!rosterComplete&&<span>{countdownSeconds===0?'正在准备下一张':`${countdown} 秒后准备下一张`}</span>}</div>
  {rosterComplete&&<div className='message success' role='status'>本名单第 {session.slot} 页已全部收齐。后台识别继续运行；请停止采集，选择其他页码或名单开始下一轮。</div>}
  {error&&<div className='message error' role='alert'>{error}{phase==='uncertain'&&<button onClick={queryCommit}>查询本次提交结果</button>}{session&&phase!=='uncertain'&&!busy&&<button onClick={queryState}>读取采集最新状态</button>}{phase==='ready'&&captureIntent.current&&<button onClick={()=>capture()}>重试本张本地准备</button>}</div>}
  {note&&<div className='message success' role='status'>{note}</div>}
  {session?.last_committed_frame_id&&<button disabled={busy||editor||phase==='uncertain'} onClick={previewWithdrawal}>撤回最近确认照片… <kbd>Ctrl+Z</kbd></button>}
  {visible&&withdrawal&&<ConfirmationDialog title='撤回最近确认照片？' busy={undoBusy} tone='danger' error={error} confirmLabel='确认撤回并回退编号（Enter）' cancelLabel='保留照片，取消撤回' onConfirm={withdraw} onCancel={()=>{Withdrawal(null);undoIntent.current=null;SetError('')}}><b className='confirmation-dialog-subject'>纸签 {withdrawal.paper_tag} · 第 {withdrawal.slot}/{withdrawal.page_count} 页</b><p>移出有效队列，终止相关识别，回到此纸签和页位重拍。{withdrawal.dependent_pages>0?`同时使 ${withdrawal.dependent_pages} 个依赖本张的页面重新等待。`:''}{withdrawal.discard_current_draft?'当前未确认照片将退出检查并保留历史，请按正确页位重新拍摄。':''}</p><p><small>已发送的云端内容与已经发生的额度无法撤销；原图、调用用量及审计历史仍保留。</small></p></ConfirmationDialog>}
  {visible&&savedToDiscard&&<ConfirmationDialog title='放弃这张未确认照片？' busy={busy} tone='danger' error={error} confirmLabel='确认放弃' onConfirm={()=>discardSaved(savedToDiscard)} onCancel={()=>{Discarding('');SetError('')}}><b className='confirmation-dialog-subject'>纸签 {savedToDiscard.saved_frame.paper_tag} · 第 {savedToDiscard.saved_frame.slot}/{savedToDiscard.page_count} 页</b><p>放弃后不再作为待确认照片继续检查。原图与处理历史仍保留。</p></ConfirmationDialog>}

  {['idle','stopped','starting'].includes(phase)&&<div className='stream-setup'>
   <div><h3>本轮采集设置</h3><div className='form-grid'>
    <label>班级<select disabled={busy} value={group} onChange={e=>{Group(e.target.value);Continuation('');CohortId('');Slot(1);StartTag('');startIntent.current=null}}>{detail.groups.map((g:R)=><option key={g.id} value={g.id}>{g.name}</option>)}</select></label>
    <label>处理方式<select aria-label='处理方式' disabled={busy} value={delivery} onChange={e=>{Delivery(e.target.value);Consent(false);startIntent.current=null;recoveryIntent.current=null}}><option value='immediate'>确认后逐张提交云端识别</option><option value='local'>仅保存本地，稍后批量提交</option></select></label>
    <label>拍摄来源<select aria-label='拍摄来源' disabled={busy} value={source} onChange={e=>{Source(e.target.value as any);startIntent.current=null;releaseCamera()}}><option value='camera'>摄像头</option><option value='files'>本地图片代替拍摄</option></select></label>
    <label>编排方式<select aria-label='编排方式' disabled={busy} value={mode} onChange={e=>{Mode(e.target.value);Continuation('');CohortId('');ReverseCohort(false);startIntent.current=null}}><option value='whole_submission'>整份：{Array.from({length:Math.min(pageCount,2)},(_,i)=>'001/P'+(i+1)).join('、')}{pageCount>2?'…':''}；{Array.from({length:Math.min(pageCount,2)},(_,i)=>'002/P'+(i+1)).join('、')}{pageCount>2?'…':''}</option><option value='single_page'>单面：001/P{slot}、002/P{slot}、003/P{slot}…</option></select></label>
    {mode==='whole_submission'?<label>多页接续<select disabled={busy} value={continuation} onChange={e=>{Continuation(e.target.value);startIntent.current=null}}><option value=''>开始新纸签</option>{detail.submissions.filter((s:R)=>s.group_id===group&&s.loaded_pages<s.page_count).map((s:R)=><option key={s.id} value={s.id}>接续纸签 {s.paper_tag}（已收 {s.loaded_pages}/{s.page_count} 页）</option>)}</select></label>:<>
     <label>本次页位<select aria-label='本次页位' disabled={busy} value={slot} onChange={e=>{Slot(Number(e.target.value));startIntent.current=null}}>{Array.from({length:pageCount},(_,i)=><option key={i+1} value={i+1}>第{i+1}页</option>)}</select></label>
     <label>补页名单<select aria-label='补页名单' disabled={busy} value={cohortId} onChange={e=>{CohortId(e.target.value);startIntent.current=null}}><option value=''>创建新名单</option>{(detail.cohorts||[]).filter((c:R)=>c.group_id===group&&c.submission_ids.length).map((c:R)=><option key={c.id} value={c.id}>{c.name} · {c.submission_ids.length}份</option>)}</select></label>
     {cohortId&&<label className='check'><input type='checkbox' disabled={busy} checked={reverseCohort} onChange={e=>{ReverseCohort(e.target.checked);startIntent.current=null}}/>按名单倒序补页</label>}
    </>}
    {!cohortId&&!continuation&&<label>起始纸签<input disabled={busy} type='number' min={selectedGroup?.next_tag||1} value={startTag} placeholder={String(selectedGroup?.next_tag||1).padStart(3,'0')} onChange={e=>{StartTag(e.target.value);startIntent.current=null}}/></label>}
   </div>
   <div className='stream-arrangement-preview' role='status'>{mode==='whole_submission'?'按整份逐页拍摄；收齐后递增纸签。':cohortId?<><b>{selectedCohort?.name} · 第 {slot} 页 · {reverseCohort?'倒序':'正序'}</b><span>待拍 {remainingCohort.length}/{cohortIds.length} 份；已有本页的 {cohortIds.length-remainingCohort.length} 份会跳过，不覆盖照片。</span>{remainingCohort.length>0?<span>首先拍摄纸签 {detail.submissions.find((s:R)=>s.id===remainingCohort[0])?.paper_tag} · 第 {slot} 页</span>:<span>本名单此页已全部收齐，请选择其他页码或名单。</span>}</>:<>固定拍摄第 {slot} 页，每张递增纸签。确认后自动保存名单，下一轮选择此名单补拍其他页。</>}</div>
   {source==='files'&&<div className='stream-file-source'><label className='button'>选择代替拍摄的图片<input type='file' accept='image/*' multiple hidden disabled={busy} onChange={e=>{Files(Array.from(e.target.files||[]).sort((a,b)=>a.name.localeCompare(b.name,undefined,{numeric:true})));FileIndex(0);e.target.value=''}}/></label><span>{files.length?`已选 ${files.length} 张，按文件名顺序；每次 Enter 读取一张。`:'未选择图片。此入口复用检查、入队和复核流程。'}</span></div>}
   {delivery==='local'?<p className='stream-consent-detail'>每张照片检查后只保存到本机图库，不调用云端，也不需要登录账户。结束拍摄后，在图库点击“开始处理”，按当时的批处理设置和并发度统一提交。</p>:<><p className='stream-consent-detail'>确认每张照片后，将向 {account?.account?.email||'当前 ChatGPT 账户'} 发送本张及必要跨页图片，使用账户额度。只拍摄和检查不会发往云端。后台识别与下一次拍摄可以同时进行，使用独立的流式并发设置。</p>
   <label className='check'><input type='checkbox' checked={consent} disabled={busy} onChange={e=>Consent(e.target.checked)}/>我已了解，核对确认后的照片可以发送到该账户</label></>}<button className='primary' disabled={busy||cameraBusy||!group||(delivery!=='local'&&!consent)||!setupReference||cohortComplete||source==='files'&&!files.length} onClick={()=>begin()}>开始流式采集</button>{!setupReference&&<p className='hint'>请先确认所选班级的参考答案。</p>}</div>
   {(unfinishedSessions.length>0||savedSessions.length>0)&&<aside className='stream-saved-sessions'>{unfinishedSessions.length>0&&<><h3>未结束的采集</h3>{unfinishedSessions.map(value=><article key={value.id}><b>{detail.groups.find((g:R)=>g.id===value.group_id)?.name}</b><span>已确认 {value.committed_count} 张</span><button disabled={busy} onClick={()=>begin(value)}>继续上次采集</button><button disabled={busy} onClick={async()=>{await api('/capture-sessions/'+value.id+'/stop',{});await loadSessions()}}>结束上次采集</button></article>)}</>}{savedSessions.length>0&&<><h3>保留的未确认照片</h3><p className='hint'>仅带入图片，按当前账户与配置开始新一轮，重新检查答案依据和题区；不会重开旧队列。下一张使用左侧所选拍摄来源。</p>{savedSessions.map(value=><article key={value.id}><b>{detail.groups.find((g:R)=>g.id===value.group_id)?.name} · 纸签 {value.saved_frame.paper_tag} · 第 {value.saved_frame.slot}/{value.page_count} 页</b><span>{value.source==='camera'?'摄像头':'本地图片'} · {value.saved_frame.source_filename}</span><div className='actions wrap'><button className='primary' disabled={busy||(delivery!=='local'&&!consent)} onClick={()=>recoverSaved(value)}>在新一轮继续检查</button><button disabled={busy} onClick={()=>{SetError('');Discarding(value.saved_frame.id)}}>放弃此照片</button></div></article>)}</>}</aside>}
  </div>}
  {source==='camera'&&<div className='stream-camera-tools'>
   <label>摄像头<select aria-label='选择摄像头' value={device} disabled={busy||cameraBusy} onChange={e=>{const value=e.target.value;Device(value);localStorage.setItem('autoreview.camera.label',devices.find(d=>d.deviceId===value)?.label||'');if(stream.current)reconnectCamera(value)}}><option value=''>系统默认摄像头</option>{devices.map((d,index)=><option key={d.deviceId||index} value={d.deviceId}>{d.label||`摄像头 ${index+1}`}</option>)}</select></label>
   <button disabled={busy||cameraBusy} onClick={()=>enumerateCameras().catch(error=>SetError(captureErrorText(error)))}>刷新设备</button>
   <button disabled={busy||cameraBusy} onClick={()=>reconnectCamera()}>{cameraReady?'重新连接':'连接 / 预览相机'}</button>
   {cameraBusy?<button className='danger' onClick={stop}>取消相机连接</button>:<button disabled={busy} onClick={()=>reconnectCamera()}>重新初始化相机</button>}
   {cameraReady&&<button disabled={busy} onClick={releaseCamera}>关闭镜头</button>}
   <span role='status' className={cameraReady?'camera-live':'hint'}>{cameraState}</span>
   <small>仅开启镜头不会上传。自动选择设备实际支持的清晰模式；连接卡住可重新初始化，保留当前照片与任务。</small>
  </div>}
  <div className={'stream-camera-stage '+(cameraBusy||phase==='ready'||phase==='capturing'||phase==='starting'||cameraReady&&['idle','stopped'].includes(phase)?'':'stream-camera-hidden')}>
   {source!=='camera'&&cameraIdentity}
   <div className='stream-camera'>
   {source==='camera'?<CameraPreview video={video} info={cameraDetails} ready={cameraReady} compact sideContent={<>{cameraIdentity}{cameraActions}</>}/>:<div className='stream-file-preview'>{files[fileIndex]?.name?<><strong>下一张本地图片</strong><span>{files[fileIndex].name}</span><small>{fileIndex+1} / {files.length}</small></>:<strong>请为下一次拍摄选择本地图片，或结束本轮。</strong>}</div>}
   {source!=='camera'&&cameraActions}
  </div></div>
  {frame&&['review','preparing','submitting','uncertain'].includes(phase)&&<div className='stream-review'>
   {photoDetails&&source==='camera'&&<p className='camera-photo-details' role='status'>拍摄原图：{photoDetails}；当前图片：{frame.asset?.width} × {frame.asset?.height}</p>}
   <div className='stream-review-heading'><div><h3>纸签 {frame.paper_tag} · 第 {frame.slot}/{session?.page_count} 页</h3><small>{frame.asset?.filename} · {source==='files'?'本地图片代替拍摄':'摄像头拍摄'}</small></div><div className='actions wrap'><button disabled={busy||phase==='uncertain'} onClick={()=>Editor(true)}>旋转 / 校正 / 裁剪</button><button disabled={busy||phase==='uncertain'} onClick={()=>{captureIntent.current=null;Frame(null);frameRef.current=null;setPhase('ready');focusHost()}}>重拍本张</button><label className='button'>替换本张图片<input type='file' accept='image/*' hidden disabled={busy||phase==='uncertain'} onChange={e=>{const file=e.target.files?.[0];e.target.value='';if(file){captureIntent.current=null;capture(file)}}}/></label></div></div>
   <div className='stream-review-body'><GeometryEditor asset={frame.asset_id} boxes={boxes} selected={region} select={Region} change={phase==='review'?(id,box)=>{Zones(old=>{const next={...old,[id]:box};keepRegions(frame,next);return next});commitIntent.current=null}:undefined}/><aside className='stream-inspection'><h3>纸签 {frame.paper_tag} · 第 {frame.slot}/{session?.page_count} 页</h3><p>检查纸签、页位、文字清晰度与完整答题区；可以拖动区域边框修正位置。</p><p className='hint'>{frame.preparation?.mapping?.reason}</p>{frame.preparation?.boundary_warnings?.length>0&&<div className='message warning'><b>照片边缘有定位警告</b><p>请查看是否缺题、裁切或答题区越界；缺少内容应重拍。</p></div>}<div className='stream-check-reminder'><strong>确认前请检查本张照片</strong>核对纸签、页位、文字清晰度和完整答题区。按 Enter 即确认检查并{localOnly?'保存本地':'发送'}；有缺题或裁切请先重拍。</div><button className='primary stream-confirm' disabled={!canConfirm} onClick={confirm}>{localOnly?'确认并保存本地':'确认并发送入队'} <kbd>Enter</kbd></button><small>{localOnly?'仅保存本地，稍后按批处理设置提交。':`发送到 ${session?.account?.email||'本轮所选 ChatGPT 账户'} · ${session?.config?.model}。`}确认后 {session?.mode==='single_page'?'按本轮名单和页码准备下一张，纸签与页位不随返回顺序改变。':session?.page_count>1?'继续同一纸签的下一页，齐页后自动递增纸签。':'自动准备下一纸签。'}</small><details><summary>自动与人工图片处理记录</summary>{frame.asset?.operation_log?.map((entry:R,index:number)=><p key={index}>{entry.source==='automatic'?'自动':'人工'} · {operationDetail(entry)}</p>)}</details></aside></div>
  </div>}
  {phase==='success'&&receipt&&<div className='stream-success'><span aria-hidden='true'>✓</span><h3>{captureReceiptText(receipt)}</h3><p>{rosterComplete?'本名单此页已拍完。':`${countdownSeconds===0?'立即回到拍摄准备':`${countdown} 秒后回到拍摄准备`}，Enter 才会拍下一张。`}{localOnly?'照片已在本地图库中，等待批量提交。':'后台处理继续运行。'}</p></div>}
  {session&&<details className='stream-queue' open><summary>本轮已确认照片 · {sessionLocal?'仅保存本地':`流式并发 ${session.config?.parallelism||1}`} · 按拍摄顺序</summary><p className='hint'>{sessionLocal?'照片按纸签与页码保存在图库。结束拍摄后点击“开始处理”统一提交，使用独立的批处理并发度。':'逐张入队，有空位即识别；返回可以先后不同，未完成页始终保留原位。停止拍摄不取消已入队处理。'}</p><ol>{detail.pages.filter((p:R)=>p.capture_session_id===session.id).sort((a:R,b:R)=>a.seq-b.seq).map((p:R)=>{const sub=detail.submissions.find((s:R)=>s.id===p.parent_id);const status=capturePageStatus(p,detail);return <li key={p.id}><b>纸签 {sub?.paper_tag} · 第 {p.slot}/{sub?.page_count} 页</b><span>{status}</span></li>})}</ol></details>}
  {editor&&frame&&<div className='editor-overlay'><ImageEditor asset={frame.asset} api={api} close={()=>Editor(false)} changed={updateAsset}/></div>}
 </section>;
}
