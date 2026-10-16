// Camera bytes remain in this renderer and the owned local asset endpoint.
import {applyCameraControls,cameraControlsBusy,cameraSettleFrames,currentCameraControls,savedCameraControls,CameraControls} from './cameraControls.js';
const pending=new WeakMap<MediaDevices,()=>void>();
let channel:{frame:HTMLIFrameElement,devices:MediaDevices,media:MediaStream|null,owner:string}|null=null;
export function stopCamera(media:MediaStream|null){media?.getTracks().forEach(track=>track.stop())}
export function closeCameraChannel(owner?:string){
 if(owner&&channel?.owner!==owner)return;
 const old=channel;channel=null;if(!old)return;
 pending.get(old.devices)?.();stopCamera(old.media);old.frame.remove();
}
export async function requestCamera(devices:MediaDevices,deviceId:string,current:()=>boolean,timeoutMs=15000){
 if(pending.has(devices))throw new Error('上次相机请求尚未结束，请点击“重新初始化相机”，不要重复连接。');
 let expired=false,timer:ReturnType<typeof setTimeout>|undefined,poll:ReturnType<typeof setInterval>|undefined;
 let cancel=()=>{};
 const cancelled=new Promise<MediaStream>((_,reject)=>{cancel=()=>{expired=true;reject(new Error('相机操作已取消。'))}});
 pending.set(devices,cancel);
 const acquisition=Promise.resolve().then(()=>{if(expired||!current())throw new Error('相机操作已取消。');return devices.getUserMedia({audio:false,video:{...(deviceId?{deviceId:{exact:deviceId}}:{}),resizeMode:{ideal:'none'},width:{ideal:1920},height:{ideal:1080},frameRate:{ideal:30}} as MediaTrackConstraints})})
  .then(media=>{if(expired||!current()){stopCamera(media);throw new Error('相机操作已取消。')}return media})
  .finally(()=>{if(pending.get(devices)===cancel)pending.delete(devices)});
 try{return await Promise.race([acquisition,cancelled,new Promise<MediaStream>((_,reject)=>{
  timer=setTimeout(()=>{expired=true;reject(new Error('相机连接超时。请点击“重新初始化相机”释放旧连接，再尝试；已拍照片仍保留。'))},timeoutMs);
  poll=setInterval(()=>{if(!current())cancel()},50);
 })])}finally{clearTimeout(timer);clearInterval(poll)}
}
export async function connectCamera(deviceId:string,current:()=>boolean,stage:(text:string)=>void,ownerId='shared'){
 if(!current())throw new Error('相机操作已取消。');
 closeCameraChannel();
 // A separate same-origin document gives getUserMedia a cancellable lifetime.
 // Removing it aborts a stalled device request without reloading the workbench
 // or restarting the backend. Never reuse a timed-out document.
 const frame=document.createElement('iframe');frame.hidden=true;frame.setAttribute('aria-hidden','true');
 frame.srcdoc='<!doctype html><html><head><title>本地相机连接</title></head><body></body></html>';
 let loadTimer:ReturnType<typeof setTimeout>|undefined;
 try{
  await new Promise<void>((resolve,reject)=>{frame.onload=()=>resolve();loadTimer=setTimeout(()=>reject(new Error('相机连接通道未就绪，请重新初始化相机。')),2000);document.body.append(frame)});
  if(!current())throw new Error('相机操作已取消。');
  const devices=frame.contentWindow!.navigator.mediaDevices;
  if(!devices?.getUserMedia)throw new Error('当前窗口无法访问摄像头，请使用本地图片代替拍摄。');
  const owner={frame,devices,media:null as MediaStream|null,owner:ownerId};channel=owner;
  const media=await requestCamera(devices,deviceId,()=>current()&&channel===owner);owner.media=media;
  if(!current()||channel!==owner){stopCamera(media);throw new Error('相机操作已取消。')}
  stage('正在选择流畅预览模式，拍照保留原生清晰度');await optimizeCamera(media,current);
  if(!current()||channel!==owner){stopCamera(media);throw new Error('相机操作已取消。')}
  return media;
 }catch(error){if(channel?.frame===frame)closeCameraChannel();else frame.remove();throw error}
 finally{clearTimeout(loadTimer)}
}
export type CameraInfo={width:number,height:number,frameRate:number,focus:string,whiteBalance:string,note:string};
const cameraNotes=new WeakMap<MediaStreamTrack,string>();
export function cameraModes(track:MediaStreamTrack){
 const caps=track.getCapabilities?.()||{},settings=track.getSettings();
 const maxW=caps.width?.max||settings.width||0,maxH=caps.height?.max||settings.height||0;
 const candidates=[[maxW,maxH],[settings.width||0,settings.height||0]];
 for(const ratio of [settings.aspectRatio||16/9,16/9,4/3,3/2]){
  candidates.push([maxW,Math.round(maxW/ratio)],[Math.round(maxH*ratio),maxH]);
 }
 for(const mode of [[7680,4320],[4096,2160],[3840,2160],[3264,2448],[2560,1440],[2048,1536],[1920,1080],[1600,1200],[1280,720],[640,480]])candidates.push(mode);
 return candidates.filter(([w,h])=>Number.isInteger(w)&&Number.isInteger(h)&&w>0&&h>0&&w<=maxW&&h<=maxH)
  .filter(([w,h],i,all)=>all.findIndex(v=>v[0]===w&&v[1]===h)===i).sort((a,b)=>b[0]*b[1]-a[0]*a[1]);
}
export async function optimizeCamera(media:MediaStream,current:()=>boolean){
 const track=media.getVideoTracks()[0];if(!track)return;
 const caps=(track.getCapabilities?.()||{}) as MediaTrackCapabilities&{resizeMode?:string[]},settings=track.getSettings() as MediaTrackSettings&{resizeMode?:string};let chosen=false;
 // Continuous 4K decoding can run at 1 fps despite a reported 30 fps. The
 // full native mode is entered only during the shutter, without upscaling.
 for(const [w,h] of cameraModes(track).filter(([w,h])=>w*h<=1920*1080)){
  if(!current())throw new Error('相机操作已取消。');
  if(w==settings.width&&h==settings.height&&(!caps.resizeMode?.includes('none')||settings.resizeMode==='none')){chosen=true;break}
  try{await bounded(track.applyConstraints({width:{exact:w},height:{exact:h},frameRate:{ideal:30},...(caps.resizeMode?.includes('none')?{resizeMode:{exact:'none'}}:{})}),2500);
   const result=track.getSettings();if(result.width===w&&result.height===h){chosen=true;break}
  }catch(error){if((error as Error).message==='相机调整超时'){stopCamera(media);throw new Error('调整相机清晰模式超时，请重新初始化相机。')}}
 }
 const automatic:Record<string,string>={};const extended=caps as Record<string,any>,actual=track.getSettings() as Record<string,any>;
 for(const key of ['focusMode','whiteBalanceMode','exposureMode'])if(extended[key]?.includes('continuous')&&actual[key]!=='continuous')automatic[key]='continuous';
 if(Object.keys(automatic).length)try{await applyCameraControls(track,automatic)}catch{/* Optional device controls may be unavailable. */}
 const saved=savedCameraControls(track);let note='';
 if(saved)try{await applyCameraControls(track,saved);note='；已加载本机相机参数'}catch{note='；保存的参数未能应用，请展开相机参数检查'}
 cameraNotes.set(track,(chosen?'流畅预览；快门时使用最高可用原生分辨率':'使用设备实际返回的模式')+note);
}
export function cameraInfo(video:HTMLVideoElement,media:MediaStream):CameraInfo{
 const track=media.getVideoTracks()[0],s=track.getSettings() as Record<string,any>;
 return {width:s.width||video.videoWidth,height:s.height||video.videoHeight,frameRate:s.frameRate||0,focus:s.focusMode==='continuous'?'自动对焦':s.focusMode==='manual'?'手动对焦':'设备默认对焦',whiteBalance:s.whiteBalanceMode==='continuous'?'自动白平衡':'设备默认白平衡',note:cameraNotes.get(track)||'原生画面'};
}
export function cameraHasFrame(video:HTMLVideoElement|null,media:MediaStream|null){
 return !!video&&video.readyState>=2&&video.videoWidth>0&&video.videoHeight>0&&!!media?.getVideoTracks().some(track=>track.readyState==='live'&&!track.muted);
}
async function bounded<T>(promise:Promise<T>,ms:number){let timer:ReturnType<typeof setTimeout>|undefined;try{return await Promise.race([promise,new Promise<T>((_,reject)=>{timer=setTimeout(()=>reject(new Error('相机调整超时')),ms)})])}finally{clearTimeout(timer)}}
async function maintainImageControls(track:MediaStreamTrack,controls:CameraControls,modeChanged=false){
 const actual=track.getSettings() as Record<string,any>,changes=Object.fromEntries(Object.entries(controls).filter(([key,value])=>actual[key]!==value));
 // Some UVC drivers keep the requested values in getSettings while the lens
 // resets during a resolution change. Replay manual controls, then acquire a
 // new frame; a matching readback alone does not prove optical readiness.
 const manual=modeChanged&&['focusMode','exposureMode','whiteBalanceMode'].some(key=>controls[key]==='manual');
 if(!manual&&!Object.keys(changes).length)return false;
 // Reapply only the manual controls on a matching readback. Unnecessarily
 // restarting auto white balance/exposure adds driver work on every shutter.
 const requested:CameraControls={...changes};
 for(const [mode,value] of [['focusMode','focusDistance'],['exposureMode','exposureTime'],['whiteBalanceMode','colorTemperature']]){
  if(controls[mode]==='manual'&&(manual||value in changes||mode in changes)){requested[mode]='manual';if(controls[value]!=null)requested[value]=controls[value]}
 }
 await applyCameraControls(track,requested);return true;
}
export async function waitCameraFrame(video:HTMLVideoElement,media:MediaStream,current:()=>boolean,timeoutMs=8000){
 const end=Date.now()+timeoutMs;await bounded(video.play(),timeoutMs);
 const sized=()=>{const settings=media.getVideoTracks()[0]?.getSettings();return cameraHasFrame(video,media)&&(!settings?.width||video.videoWidth===settings.width)&&(!settings?.height||video.videoHeight===settings.height)};
 while(current()&&!sized()&&Date.now()<end)await new Promise(resolve=>setTimeout(resolve,25));
 if(!current())throw new Error('相机操作已取消。');
 if(!sized())throw new Error('摄像头没有返回可拍摄的画面，请点击重新初始化相机；已拍照片仍保留。');
 // Dimensions may belong to a frozen frame retained by the video element.
 // Confirm a newly delivered frame before declaring the preview ready.
 if(typeof video.requestVideoFrameCallback==='function')await freshSizedFrame(video,media,video.videoWidth,video.videoHeight,current,Math.max(1,end-Date.now()));
}
type PhotoInfo={width:number,height:number,method:string,previewRestored?:boolean};
const photoInfo=new WeakMap<File,PhotoInfo>();
const photoCapabilities=new WeakMap<MediaStreamTrack,Promise<any>>();
function stillCapabilities(track:MediaStreamTrack,capture:any){let promise=photoCapabilities.get(track);if(!promise){promise=bounded(capture.getPhotoCapabilities(),700).catch(()=>null);photoCapabilities.set(track,promise)}return promise}
export function photographInfo(file:File){return photoInfo.get(file)}
const changing=new WeakSet<MediaStreamTrack>(),photoModes=new WeakMap<MediaStreamTrack,number[]>();
const restorations=new WeakMap<MediaStreamTrack,Promise<boolean>>();
export function cameraChanging(track:MediaStreamTrack){return changing.has(track)}
export function cameraRestoration(media:MediaStream){return restorations.get(media.getVideoTracks()[0])||null}
async function freshSizedFrame(video:HTMLVideoElement,media:MediaStream,w:number,h:number,current:()=>boolean,ms=6500){
 const end=performance.now()+ms;await bounded(video.play(),ms);
 while(current()&&performance.now()<end){
  await new Promise<void>(resolve=>setTimeout(resolve,25));
  if(cameraHasFrame(video,media)&&video.videoWidth===w&&video.videoHeight===h){
   if(!video.requestVideoFrameCallback)return;
   const remaining=Math.max(1,end-performance.now());
   const received=await new Promise<boolean>(resolve=>{const id=video.requestVideoFrameCallback(()=>{clearTimeout(timer);resolve(true)});const timer=setTimeout(()=>{video.cancelVideoFrameCallback(id);resolve(false)},remaining)});
   if(received&&current()&&video.videoWidth===w&&video.videoHeight===h)return;
  }
 }
 throw new Error(current()?'清晰照片画面尚未就绪，请保持试卷不动后重拍。':'拍摄已取消，未保存本张照片。');
}
async function changeCameraMode(video:HTMLVideoElement,media:MediaStream,constraints:MediaTrackConstraints,w:number,h:number,current:()=>boolean,ms=6500){
 // Register before changing mode: its first full-size frame is the shutter
 // evidence. Waiting for an additional 4K frame can add a whole second.
 if(!video.requestVideoFrameCallback){await bounded(media.getVideoTracks()[0].applyConstraints(constraints),2500);await freshSizedFrame(video,media,w,h,current,ms);return}
 let callback=0,timer:ReturnType<typeof setTimeout>|undefined,settled=false;
 const fresh=new Promise<void>((resolve,reject)=>{
  const next=()=>{if(settled)return;if(!current()){settled=true;reject(new Error('拍摄已取消。'));return}
   if(video.videoWidth===w&&video.videoHeight===h&&cameraHasFrame(video,media)){settled=true;resolve();return}
   callback=video.requestVideoFrameCallback(next);
  };callback=video.requestVideoFrameCallback(next);timer=setTimeout(()=>{settled=true;reject(new Error('清晰照片画面尚未就绪，请保持试卷不动后重拍。'))},ms);
 });fresh.catch(()=>{});
 try{await bounded(media.getVideoTracks()[0].applyConstraints(constraints),2500);const accepted=media.getVideoTracks()[0].getSettings();if(accepted.width&&accepted.width!==w||accepted.height&&accepted.height!==h)throw new Error('相机未接受此原生分辨率');await fresh}
 finally{settled=true;clearTimeout(timer);video.cancelVideoFrameCallback(callback)}
}
export async function photograph(video:HTMLVideoElement,media:MediaStream,current:()=>boolean=()=>true){
 if(!cameraHasFrame(video,media))throw new Error('摄像头画面尚未就绪，请等待实时画面出现或重新连接后拍摄。');
 if(cameraControlsBusy(media.getVideoTracks()[0]))throw new Error('正在调整相机参数，请等待完成后再拍摄。');
 await waitCameraFrame(video,media,current);
 const track=media.getVideoTracks()[0];if(changing.has(track))throw new Error('相机正在拍摄，请等待本张完成。');
 const before=track.getSettings(),controls=currentCameraControls(track);let adjusted=false;
 const canvas=document.createElement('canvas');
 const context=canvas.getContext('2d');if(!context)throw new Error('当前窗口无法保存相机画面，请重新连接。');changing.add(track);
 let info:PhotoInfo={width:0,height:0,method:'原生清晰照片',previewRestored:true},restoring=false;
 const restore=()=>{
  if(!adjusted||restoring||track.readyState!=='live'||!current())return;
  restoring=true;info.previewRestored=undefined;
  const promise=(async()=>{try{await bounded(track.applyConstraints({width:{exact:before.width||1920},height:{exact:before.height||1080},frameRate:{ideal:before.frameRate||30},resizeMode:{ideal:'none'}} as MediaTrackConstraints),5000);if(!current()||track.readyState!=='live')return false;const actual=track.getSettings();if(actual.width!==before.width||actual.height!==before.height)throw new Error('相机未恢复预览模式');await maintainImageControls(track,controls,true);info.previewRestored=true;return true}catch{info.previewRestored=false;stopCamera(media);return false}finally{changing.delete(track);restorations.delete(track)}})();
  restorations.set(track,promise);
 };
 try{
  const modes=photoModes.has(track)?[photoModes.get(track)!]:cameraModes(track);
  for(const [w,h] of modes){
   if(!current())throw new Error('拍摄已取消，未保存本张照片。');
   if(w*h<=(before.width||video.videoWidth)*(before.height||video.videoHeight))break;
   try{adjusted=true;const native=(track.getCapabilities() as MediaTrackCapabilities&{resizeMode?:string[]}).resizeMode?.includes('none');await changeCameraMode(video,media,{width:{exact:w},height:{exact:h},...(native?{resizeMode:{exact:'none'}}:{})} as MediaTrackConstraints,w,h,current);
    const actual=track.getSettings();if(actual.width!==w||actual.height!==h)continue;
    photoModes.set(track,[w,h]);break;
   }catch(error){if(!current()||(error as Error).message.includes('画面尚未就绪'))throw error;if((error as Error).message==='相机调整超时'){stopCamera(media);throw error}}
  }
  const controlsChanged=await maintainImageControls(track,controls,adjusted);
  if(adjusted&&(controlsChanged||cameraSettleFrames(track)))await freshSizedFrame(video,media,video.videoWidth,video.videoHeight,current);
  if(!current()||!cameraHasFrame(video,media))throw new Error('拍摄已取消或相机断开，请重新连接后重拍。');
  canvas.width=video.videoWidth;canvas.height=video.videoHeight;context.drawImage(video,0,0);
  let blob:Blob|undefined;info.width=canvas.width;info.height=canvas.height;
  // Pixels are frozen in the canvas. Restore live preview during local encoding
  // and teacher inspection, rather than making the shutter await two mode changes.
  if(adjusted)restore();
  const ImageCapture=(window as any).ImageCapture;
  if(ImageCapture&&!adjusted){try{
   const track=media.getVideoTracks()[0],capture=new ImageCapture(track),caps=await stillCapabilities(track,capture);
   if(!caps)info.method+='（照片接口不可用时回退）';
   if(caps?.imageWidth?.max*caps?.imageHeight?.max>canvas.width*canvas.height){
    const still=await bounded(capture.takePhoto({imageWidth:caps.imageWidth.max,imageHeight:caps.imageHeight.max}),2000) as Blob;
    const bitmap=await createImageBitmap(still);
    if(bitmap.width*bitmap.height>=canvas.width*canvas.height){blob=still;info={width:bitmap.width,height:bitmap.height,method:'原生高分辨率照片'}}bitmap.close();
   }
  }catch{info.method+='（照片接口不可用时回退）'}}
  if(!current())throw new Error('拍摄已取消，未保存本张照片。');
  blob??=await new Promise<Blob>((resolve,reject)=>canvas.toBlob(b=>b?resolve(b):reject(new Error('拍摄图像失败，请重拍。')),'image/jpeg',.98));
  if(!current())throw new Error('拍摄已取消，未保存本张照片。');
  const file=new File([blob],'拍摄-'+new Date().toISOString().replace(/[:.]/g,'-')+(blob.type==='image/png'?'.png':'.jpg'),{type:blob.type||'image/jpeg'});photoInfo.set(file,info);return file;
 }finally{
  restore();if(!restoring)changing.delete(track);canvas.width=0;canvas.height=0;
 }
}

