export const tabId=crypto.randomUUID();
let token=new URLSearchParams(location.hash.slice(1)).get('session')||'';
let instance='',library='',writeBlocked=false,contextChanged=false,trustedRecovery=false;
let recoveryEpoch=0;
const pendingRequests=new Set<AbortController>();
const assetMetadata=new Map<string,any>();
window.addEventListener('autoreview:unreachable',()=>{writeBlocked=true});
export function authorizeBackendRecovery(){trustedRecovery=true;writeBlocked=true;contextChanged=false;recoveryEpoch++;for(const request of pendingRequests)request.abort()}
export function resumeWriting(){if(!contextChanged)writeBlocked=false}
export class ApiError extends Error{
 constructor(message:string,public status:number,public code:string,public details:any={},public requestId='',public instanceId=''){super(message)}
}
export async function api(path:string,data?:any,method?:string,retryCount=0):Promise<any>{
 if(data===undefined&&/^\/assets\/[^/]+$/.test(path)&&assetMetadata.has(path))return assetMetadata.get(path);
 if(data!==undefined&&writeBlocked&&path!=='/session')throw new ApiError(contextChanged?'服务或资料库已变化，旧窗口停止写入，请从当前入口重新打开。':'正在恢复连接并核对数据，请稍候再操作。',409,'RECOVERY_REQUIRED');
 const epoch=recoveryEpoch,form=data instanceof FormData,requestId=data?.request_id||crypto.randomUUID(),controller=new AbortController(),timer=setTimeout(()=>controller.abort(),60000);pendingRequests.add(controller);
 try{
  const r=await fetch('/api'+path,{method:method||(data===undefined?'GET':'POST'),headers:{...(form?{}:{'Content-Type':'application/json'}),...(token?{'X-AutoReview-Session':token}:{}),...(instance?{'X-AutoReview-Instance':instance}:{}),...(library?{'X-AutoReview-Library':library}:{}),'X-AutoReview-Tab':tabId,'X-Request-ID':requestId},body:data===undefined?undefined:form?data:JSON.stringify(data),signal:controller.signal});
  let result:any;try{result=await r.json()}catch{throw new ApiError('服务返回的数据格式不正确，请查看诊断日志。',r.status,'RESPONSE_FORMAT',{},requestId)}
  if(epoch!==recoveryEpoch)throw new ApiError('本地服务已恢复，正在重新读取最新状态。',409,'RECOVERY_RETRY',{},requestId);
  if(!r.ok&&result.code==='LIBRARY_MAINTENANCE'&&retryCount<4){await new Promise(resolve=>setTimeout(resolve,250));return api(path,data,method,retryCount+1)}
  if(!r.ok)throw new ApiError(r.status===401?'此窗口的本地会话已失效，请从当前版本入口重新打开。':(result.message||'操作失败')+(result.details?.missing_tags?' 编号：'+result.details.missing_tags.join('、'):''),r.status,result.code||'HTTP_ERROR',result.details,result.request_id||requestId,result.instance_id||r.headers.get('X-AutoReview-Instance')||'');
  if(path==='/state'){const next=result.instance_id,nextLibrary=result.library?.library_id;if(instance&&next&&instance!==next&&!trustedRecovery||library&&nextLibrary&&library!==nextLibrary){contextChanged=true;writeBlocked=true;throw new ApiError('服务或资料库已变化，请从当前入口重新打开；未保存草稿保留在本机。',409,'CONTEXT_CHANGED')}instance=next||instance;library=nextLibrary||library;trustedRecovery=false}
  if(data!==undefined&&!['/session','/activity'].includes(path))window.dispatchEvent(new CustomEvent('autoreview:data-changed',{detail:path}));
  if(data===undefined&&/^\/assets\/[^/]+$/.test(path)){assetMetadata.set(path,result);if(assetMetadata.size>256)assetMetadata.delete(assetMetadata.keys().next().value!)}
  return result;
 }catch(e){if(epoch!==recoveryEpoch)throw new ApiError('本地服务已恢复，正在重新读取最新状态；未确认结果的修改请核对后再操作。',409,'RECOVERY_RETRY',{},requestId);if(e instanceof ApiError)throw e;writeBlocked=true;window.dispatchEvent(new Event('autoreview:unreachable'));throw new ApiError((e as Error).name==='AbortError'?'本次请求响应超时，修改仍保留；请查询状态后重试。':'本地服务暂时不可达，正在尝试恢复连接。未保存修改仍保留。',0,(e as Error).name==='AbortError'?'REQUEST_TIMEOUT':'NETWORK_ERROR',{},requestId)}finally{clearTimeout(timer);pendingRequests.delete(controller)}
}
export async function openSession(){if(token){await api('/session',{});history.replaceState(null,'',location.pathname);token=''}}
