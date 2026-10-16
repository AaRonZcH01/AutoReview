import {tabId} from './clientApi.js';

const PING_DEADLINE_MS=35000;
const WATCH_INTERVAL_MS=2500;
const RESUME_GAP_MS=10000;

export function attachLifetime(){
 let socket:WebSocket|undefined,retry:number|undefined,closed=false,generation=0,lost=false;
 let lastPing=Date.now(),lastCheck=lastPing,graceUntil=lastPing+PING_DEADLINE_MS;
 const resetClock=()=>{lastPing=lastCheck=Date.now();graceUntil=lastPing+PING_DEADLINE_MS};
 const announceLost=()=>{if(!lost){lost=true;window.dispatchEvent(new Event('autoreview:unreachable'))}};
 const scheduleReconnect=()=>{clearTimeout(retry);if(!closed)retry=window.setTimeout(connect,1000)};
 const disconnect=()=>{
  if(closed)return;
  announceLost();++generation;
  // A half-open socket need not emit close promptly. Ignore its later events
  // and reconnect explicitly, while retaining the current application state.
  try{socket?.close(4000,'heartbeat-timeout')}catch{}
  socket=undefined;scheduleReconnect();
 };
 const connect=()=>{
  if(closed)return;clearTimeout(retry);resetClock();
  const current=++generation,ws=new WebSocket(`ws://${location.host}/api/lifetime?tab=${encodeURIComponent(tabId)}`);socket=ws;
  ws.onmessage=event=>{
   if(current!==generation||closed||event.data!=='ping')return;
   lastPing=Date.now();
   if(ws.readyState===WebSocket.OPEN){try{ws.send('pong')}catch{disconnect()}}
  };
  ws.onopen=()=>{if(current===generation&&!closed){resetClock();if(lost){lost=false;window.dispatchEvent(new Event('autoreview:reconnected'))}}};
  ws.onclose=()=>{if(!closed&&current===generation){socket=undefined;announceLost();scheduleReconnect()}};
 };
 const watch=window.setInterval(()=>{
  if(closed)return;
  const now=Date.now(),elapsed=now-lastCheck;lastCheck=now;
  // Windows sleep, clock changes and background timer throttling are not
  // evidence of a failed service. Observe a fresh window after a long gap.
  if(elapsed<0||elapsed>RESUME_GAP_MS){resetClock();return}
  if(socket&&now>=graceUntil&&now-lastPing>=PING_DEADLINE_MS)disconnect();
 },WATCH_INTERVAL_MS);
 const leave=()=>{closed=true;++generation;clearTimeout(retry);try{socket?.close(1000,'pagehide')}catch{}socket=undefined};
 const resume=()=>{resetClock();if(closed){closed=false;connect()}};
 const visible=()=>{if(document.visibilityState==='visible')resetClock()};
 addEventListener('pagehide',leave);addEventListener('pageshow',resume);document.addEventListener('visibilitychange',visible);connect();
 return()=>{leave();window.clearInterval(watch);removeEventListener('pagehide',leave);removeEventListener('pageshow',resume);document.removeEventListener('visibilitychange',visible)};
}
