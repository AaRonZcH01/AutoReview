export type CameraControls=Record<string,number|string>;
export const cameraControlKeys=['focusMode','focusDistance','exposureMode','exposureTime','brightness','contrast','sharpness','whiteBalanceMode','colorTemperature','saturation','exposureCompensation'] as const;
const numeric=cameraControlKeys.filter(k=>!k.endsWith('Mode'));
const busy=new WeakSet<MediaStreamTrack>();let profiles:Record<string,{controls:CameraControls,settle_frames?:number}>={};
export function configureCameraProfiles(value:typeof profiles|undefined){profiles=value||{}}
export function rememberCameraProfile(label:string,controls:CameraControls){profiles={...profiles,[label]:{controls}}}
export function savedCameraControls(track:MediaStreamTrack){return profiles[track.label]?.controls}
export function cameraSettleFrames(track:MediaStreamTrack){return profiles[track.label]?.settle_frames===1?1:0}
export function cameraControlsBusy(track:MediaStreamTrack){return busy.has(track)}
export function currentCameraControls(track:MediaStreamTrack){
 const settings=track.getSettings() as Record<string,any>,value:CameraControls={};
 for(const key of cameraControlKeys)if(settings[key]!=null)value[key]=settings[key];
 if(value.focusMode!=='manual')delete value.focusDistance;
 if(value.exposureMode!=='manual')delete value.exposureTime;
 if(value.whiteBalanceMode!=='manual')delete value.colorTemperature;
 return value;
}
export async function applyCameraControls(track:MediaStreamTrack,requested:CameraControls){
 if(track.readyState!=='live')throw Error('相机已关闭，请重新连接。');
 if(busy.has(track))throw Error('相机参数正在调整，请等待完成。');
 const caps=(track.getCapabilities?.()||{}) as Record<string,any>,changes:CameraControls={};
 for(const [key,value] of Object.entries(requested)){
  if(!cameraControlKeys.includes(key as any))throw Error('不支持的相机参数。');
  if(key.endsWith('Mode')){if(typeof value!=='string'||!caps[key]?.includes(value))throw Error('设备不支持此自动／手动模式。')}
  else {const range=caps[key];if(!numeric.includes(key as any)||!range||typeof value!=='number'||!Number.isFinite(value)||value<range.min||value>range.max)throw Error('相机参数超出设备支持范围。')}
  changes[key]=value;
 }
 if(changes.focusMode==='continuous')delete changes.focusDistance;
 if(changes.exposureMode==='continuous')delete changes.exposureTime;
 if(changes.whiteBalanceMode==='continuous')delete changes.colorTemperature;
 if(!Object.keys(changes).length)return currentCameraControls(track);
 busy.add(track);let timer:ReturnType<typeof setTimeout>|undefined;
 try{
  // Chromium rejects combined video-mode and ImageCapture controls. Do not
  // spread getConstraints() here: it may contain width/height/frameRate.
  await Promise.race([track.applyConstraints({advanced:[changes] as MediaTrackConstraintSet[]}),new Promise<never>((_,reject)=>{timer=setTimeout(()=>{track.stop();reject(Error('相机参数调整超时，已释放镜头；请重新连接。'))},2500)})]);
  if(track.readyState!=='live')throw Error('相机已关闭，未保存参数。');
  const actual=track.getSettings() as Record<string,any>;
  for(const key of Object.keys(changes))if(actual[key]==null||key.endsWith('Mode')&&actual[key]!==changes[key])throw Error('设备未应用请求的相机参数，请恢复自动后重试。');
  return currentCameraControls(track);
 }finally{clearTimeout(timer);busy.delete(track)}
}
