import {useEffect,useRef,useState} from 'react';
import {ConfirmationDialog} from './ConfirmationDialog.js';
type R=Record<string,any>;
export function AccountPanel({settings,account,onAccount,api,refresh,login,setLogin,onExit}:{settings:R,account:R|null,onAccount:(v:R|null)=>void,api:(p:string,d?:R)=>Promise<any>,refresh:()=>Promise<any>,login:R|null,setLogin:(v:R|null)=>void,onExit:()=>void}){
 const [busy,Busy]=useState(false),[error,ErrorText]=useState(''),[confirm,Confirm]=useState<R|null>(null);
 const generation=useRef(0),pending=useRef(login);pending.current=login;
 const run=async(fn:()=>Promise<void>)=>{Busy(true);ErrorText('');try{await fn()}catch(e){ErrorText((e as Error).message)}finally{Busy(false);try{await refresh()}catch(e){ErrorText((e as Error).message)}}};
 const check=async()=>{
  const current=pending.current;
  if(current){const status=await api('/account/login-status',{profile_id:current.profile_id});if(!status.complete)return;setLogin(null)}
  onAccount(await api('/account'));
 };
 useEffect(()=>{
  if(!login)return;
  const mine=++generation.current;let timer:ReturnType<typeof setTimeout>;
  const poll=async()=>{try{const status=await api('/account/login-status',{profile_id:login.profile_id});if(generation.current!==mine)return;if(status.complete){const connected=await api('/account');if(generation.current!==mine)return;setLogin(null);onAccount(connected);await refresh();return}}catch(e){if(generation.current===mine)ErrorText((e as Error).message)}if(generation.current===mine)timer=setTimeout(poll,2500)};
  timer=setTimeout(poll,1500);return()=>{generation.current++;clearTimeout(timer)};
 },[login?.profile_id]);
 const cancel=()=>run(async()=>{generation.current++;const current=pending.current;if(current)await api('/account/cancel',{profile_id:current.profile_id,login_id:current.loginId});setLogin(null)});
 return <section aria-label='本副本账户管理'>
  <div className='row-between'><h3>{account?.account?.email||'登录 OpenAI / ChatGPT 账户'}</h3><button disabled={busy} onClick={()=>run(check)}>刷新登录与额度</button></div>
  <p className='hint'>本副本独立保存账户。登录成功后自动记住；Windows 加密保存，不继承其他软件账户。</p>
  {error&&<p role='alert' className='message error'>{error}</p>}{account?.warning&&<p role='status' className='message warning'>{account.warning}</p>}
  {account?.quota_warning&&<p role='status' className='message warning'>{account.quota_warning}</p>}
  <div className='actions wrap'><button className='primary' disabled={busy||!!login} onClick={()=>run(async()=>setLogin(await api('/account/login',{name:'ChatGPT 账户'})))}>添加并登录账户</button>
  {settings.active_profile_id&&<><button disabled={busy||!!login} onClick={()=>{if(!account?.account)Confirm({repair:true,...settings.profiles.find((p:R)=>p.id===settings.active_profile_id)});else void run(async()=>setLogin(await api('/account/login',{profile_id:settings.active_profile_id})))}}>重新登录</button><button disabled={busy} onClick={()=>run(async()=>{await api('/account/logout',{});onAccount(null)})}>退出当前账户</button></>}</div>
  {login&&<div className='login-box' role='status'><b>{settings.profiles.some((p:R)=>p.id===login.profile_id)?'等待官方重新登录，原账户记录保留':'等待官方登录完成，尚未添加新账户'}</b>{login.authUrl&&<button onClick={()=>run(async()=>{await api('/account/open-login',{profile_id:login.profile_id})})}>打开官方登录页面 ↗</button>}<p>完成后会自动确认并读取额度，也可点击“刷新登录与额度”。取消不会留下空账户。</p><button onClick={cancel}>取消本次登录</button></div>}
  {settings.profiles.length>0?<><label>切换账户<select disabled={busy||!!login} value={settings.active_profile_id||''} onChange={e=>run(async()=>{onAccount(null);onAccount(await api('/account/switch',{profile_id:e.target.value}))})}><option value='' disabled>选择要连接的账户</option>{settings.profiles.map((p:R)=><option key={p.id} value={p.id}>{p.display_email||p.name}</option>)}</select></label>
  <div className='account-list'>{settings.profiles.map((p:R)=><div className='row-between' key={p.id}><span>{p.display_email||p.name}{p.id===settings.active_profile_id?' · 当前账户':''}</span><button className='danger' disabled={busy||!!login} onClick={()=>Confirm(p)}>删除账户…</button></div>)}</div><button className='danger' disabled={busy} onClick={()=>Confirm({all:true,name:'本副本全部账户'})}>清除全部账户并退出…</button></>:<p className='hint'>本副本尚未保存账户。</p>}
  {confirm&&<ConfirmationDialog title={confirm.repair?'修复此账户的登录？':confirm.all?'清除本副本全部账户并退出？':'删除此账户？'} tone='danger' busy={busy} error={error} confirmLabel={confirm.repair?'清除失效登录并重新登录':'确认清除'} onCancel={()=>{Confirm(null);ErrorText('')}} onConfirm={()=>run(async()=>{if(confirm.repair){onAccount(null);setLogin(await api('/account/repair-login',{profile_id:confirm.id,confirm:true}));Confirm(null)}else if(confirm.all){await api('/account/clear',{confirm:true});setLogin(null);onAccount(null);Confirm(null);onExit()}else{await api('/account/delete',{profile_id:confirm.id,confirm:true});if(confirm.id===settings.active_profile_id)onAccount(null);Confirm(null)}})}><b>{confirm.display_email||confirm.name}</b><p>{confirm.repair?'清除本账户失效的本地登录，随后打开官方登录流程。保留原账户记录与任务关联，需由你完成登录；不会自动使用其他账户。':'清理本副本的登录凭据及账户配置，需要时可重新登录。'}作业资料与历史成绩保留；其他副本不受影响。</p></ConfirmationDialog>}
 </section>;
}
