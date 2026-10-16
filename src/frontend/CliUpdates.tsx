import {useEffect,useRef,useState} from 'react';
import {ConfirmationDialog} from './ConfirmationDialog.js';
type R=Record<string,any>;
type Api=(path:string,data?:any)=>Promise<any>;
const active=(phase:string)=>['downloading','testing','waiting_idle','switching'].includes(phase);
const stateEvent='autoreview:cli-update-state',checkEvent='autoreview:check-cli-update';

export function CliUpdates({settings,api,onAccount}:{settings:R,api:Api,onAccount:(a:R)=>void}){
 const [offer,Offer]=useState<R|null>(null),[error,ErrorText]=useState(''),[busy,Busy]=useState(false);
 const started=useRef(false),checking=useRef(false),mounted=useRef(true),lastPhase=useRef('');
 const handle=(value:R)=>{dispatchEvent(new CustomEvent(stateEvent,{detail:value}));
  if(value.phase==='updated'&&lastPhase.current!=='updated')api('/account?refresh_models=true').then(onAccount).catch(()=>{});
  lastPhase.current=value.phase;
 };
 const check=async(trigger='manual')=>{
  if(checking.current)return;checking.current=true;ErrorText('');
  try{const value=await api('/cli-update/check',{trigger});if(mounted.current){handle(value);if(value.phase==='available')Offer(value)}}
  catch(e){dispatchEvent(new CustomEvent(stateEvent,{detail:{phase:'check_failed',message:(e as Error).message}}))}
  finally{checking.current=false}
 };
 useEffect(()=>{mounted.current=true;const manual=()=>{void check()};addEventListener(checkEvent,manual);
  // Status polling performs no remote version checks; it also follows an update
  // confirmed before a restart. The backend persists that consented intent.
  let timer:ReturnType<typeof setTimeout>|undefined,disposed=false;
  const poll=async()=>{try{const value=await api('/cli-update');if(!disposed){handle(value);timer=setTimeout(poll,active(value.phase)?1000:5000)}}catch{if(!disposed)timer=setTimeout(poll,5000)}};void poll();
  return()=>{mounted.current=false;disposed=true;if(timer)clearTimeout(timer);removeEventListener(checkEvent,manual)};
 },[]);
 useEffect(()=>{if(!started.current){started.current=true;if(settings.cli_check_on_startup)void check('startup')}},[]);
 return offer?<ConfirmationDialog title='更新 Codex CLI 组件？' confirmLabel='确认下载并更新' cancelLabel='暂不更新' busy={busy} error={error}
  onCancel={()=>{Offer(null);ErrorText('')}} onConfirm={async()=>{Busy(true);ErrorText('');try{const value=await api('/cli-update/confirm',{offer_id:offer.offer_id,confirm:true});handle(value);Offer(null)}catch(e){ErrorText((e as Error).message)}finally{Busy(false)}}}>
  <p>当前：{offer.current_version}<br/>兼容稳定版：{offer.available_version}</p><p>预计下载 {(offer.download_bytes/1024/1024).toFixed(1)} MB。来源：OpenAI 官方 Codex 发布。</p>
  <p>下载后校验 SHA256 并测试接口，不调用模型。处理、采集或登录期间会等待空闲，切换失败会回退原组件。</p>
  <p>只更新本副本的组件；账户与作业保留，模型设置保持。</p>
 </ConfirmationDialog>:null;
}

export function CliUpdateSettings({settings,save,api}:{settings:R,save:(data:R)=>Promise<any>,api:Api}){
 const [state,State]=useState<R>({}),[busy,Busy]=useState(false),[error,ErrorText]=useState('');
 const [enabled,Enabled]=useState(!!settings.cli_check_on_startup);
 useEffect(()=>{if(!busy)Enabled(!!settings.cli_check_on_startup)},[settings.cli_check_on_startup,busy]);
 useEffect(()=>{let live=true;api('/cli-update').then(s=>{if(live)State(s)}).catch(()=>{});const changed=(e:Event)=>State((e as CustomEvent).detail);addEventListener(stateEvent,changed);return()=>{live=false;removeEventListener(stateEvent,changed)}},[]);
 return <section className='cli-update-settings'><h3>Codex CLI 组件更新</h3>
  <label className='check'><input type='checkbox' checked={enabled} disabled={busy} onChange={async e=>{const checked=e.target.checked;Enabled(checked);Busy(true);ErrorText('');try{await save({cli_check_on_startup:checked})}catch(e){Enabled(!checked);ErrorText((e as Error).message)}finally{Busy(false)}}}/>启动时检查 Codex CLI 更新（有更新时询问）</label>
  <p className='hint'>默认关闭。点击“刷新模型目录”始终检查更新；确认后才下载，等待任务空闲后切换，失败回退。兼容系列 {state.compatible_series||'0.163.x'}。</p>
  {state.message&&<p role='status'>{state.message}{state.current_version&&<> · {state.current_version}</>}</p>}
  {state.phase==='downloading'&&<><progress aria-label='CLI组件下载进度' max={state.total_bytes||1} value={state.downloaded_bytes||0}/><small>{Math.min(100,Math.round((state.downloaded_bytes||0)/Math.max(1,state.total_bytes||1)*100))}%</small></>}
  {active(state.phase)&&state.phase!=='switching'&&<button onClick={async()=>{try{State(await api('/cli-update/cancel',{}))}catch(e){ErrorText((e as Error).message)}}}>取消组件更新</button>}
  {error&&<p role='alert' className='message error'>{error}</p>}
 </section>;
}
