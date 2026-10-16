import {useEffect,useRef,useState} from 'react';
import {authorizeBackendRecovery} from './clientApi.js';
type R=Record<string,any>;
export const native=()=> (window as any).__TAURI_INTERNALS__;
export async function invoke(name:string,args:R={},timeoutMs=65000):Promise<any>{
 const bridge=native();if(!bridge)throw new Error('当前窗口没有桌面连接，请使用程序启动入口。');
 let timer:ReturnType<typeof setTimeout>|undefined;
 try{return await Promise.race([bridge.invoke(name,args),new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('桌面操作尚未返回，请重试连接查询当前状态；不要重复提交修改。')),timeoutMs)})])}finally{clearTimeout(timer)}
}
export function BackendRecovery({connected,refresh}:{connected:boolean,refresh:()=>Promise<any>}){
 const [busy,Busy]=useState(false),[status,Status]=useState(''),[tries,Tries]=useState(0),running=useRef(false),mounted=useRef(true),refreshRef=useRef(refresh);refreshRef.current=refresh;
 const update=(value:string)=>{if(mounted.current)Status(value)};
 const reload=async()=>{try{await refreshRef.current()}catch(e){if((e as any)?.code!=='RECOVERY_RETRY')throw e;await Promise.resolve();await refreshRef.current()}};
 const recover=async(force=false)=>{
  if(running.current||!native())return;
  if(force&&!window.confirm('重启本地服务会暂停未完成云端任务。当前界面与未保存修改保留，状态不明的操作需核对保存结果。确认重启？'))return;
  running.current=true;Busy(true);let progress:ReturnType<typeof setInterval>|undefined;
  try{
   update('正在检查本地服务进程…');
   progress=setInterval(()=>{invoke('backend_status',{},5000).then(r=>{if(r.phase)update(r.phase)}).catch(()=>{})},1500);
   const r=await invoke('restart_backend',{force});
   clearInterval(progress);progress=undefined;update(r.message);
   if(r.restarted){
    authorizeBackendRecovery();await reload();update('连接已恢复，已核对当前资料库。');if(mounted.current)Tries(0);
   }else if(!r.in_progress){const state=await invoke('backend_status',{},5000);if(mounted.current)Tries(state.attempts||0)}
  }catch(e){if(mounted.current){update('恢复暂未完成：'+String(e));Tries(n=>n+1)}}
  finally{clearInterval(progress);running.current=false;if(mounted.current)Busy(false)}
 };
 useEffect(()=>{mounted.current=true;return()=>{mounted.current=false}},[]);
 useEffect(()=>{if(connected){Tries(0);return}const timer=setInterval(()=>{if(tries<3)void recover()},5000);return()=>clearInterval(timer)},[connected,tries]);
 if(connected)return null;
 return <div className='backend-recovery' role='alert'><span>{status||'本地服务暂时不可达。界面与未保存修改已保留，正在尝试恢复连接。'}</span><button disabled={busy} onClick={()=>reload().catch(e=>update(e.message))}>重试连接</button>{native()&&<button disabled={busy} onClick={()=>recover(true)}>重启本地服务…</button>}<small>{busy?'正在恢复，请稍候…':tries>=3?'自动恢复已暂停，请检查日志或手动重启。':'云端请求不会自动重复提交。'}</small></div>
}
