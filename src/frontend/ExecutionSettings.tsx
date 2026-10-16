import React,{useEffect,useState} from 'react';
import {DEFAULT_BATCH_PARALLELISM,DEFAULT_CAPTURE_PARALLELISM} from './executionDefaults.js';
type R=Record<string,any>;
export function ExecutionSettings({settings,save}:{settings:R,save:(change:R)=>void}){
 const [delay,Delay]=useState(String(settings.capture_countdown_seconds??2));
 useEffect(()=>Delay(String(settings.capture_countdown_seconds??2)),[settings.capture_countdown_seconds]);
 const validDelay=/^\d+$/.test(delay)&&Number(delay)<=60;
 return <section className="execution-settings">
  <h3>处理与复核</h3>
  <div className="form-grid"><label>批处理并行页数<select aria-label="批处理并行页数" value={settings.parallelism??DEFAULT_BATCH_PARALLELISM} onChange={e=>save({parallelism:Number(e.target.value)})}>{Array.from({length:13},(_,i)=>i+1).map(n=><option value={n} key={n}>{n===1?'1 · 逐页处理':n+' · 同时处理'+n+'页'+(n===DEFAULT_BATCH_PARALLELISM?'（默认）':'')}</option>)}</select></label>
   <label>流式识别槽位<select aria-label="流式处理并行页数" value={settings.capture_parallelism??DEFAULT_CAPTURE_PARALLELISM} onChange={e=>save({capture_parallelism:Number(e.target.value)})}>{Array.from({length:13},(_,i)=>i+1).map(n=><option value={n} key={n}>{n} · 最多同时识别 {n} 页{n===DEFAULT_CAPTURE_PARALLELISM?'（默认）':''}</option>)}</select></label>
   <label>保守复看推理强度<select value={settings.verification_effort||'medium'} onChange={e=>save({verification_effort:e.target.value})}><option value="low">低</option><option value="medium">中等（默认）</option><option value="high">高</option></select></label></div>
  <label className="check"><input type="checkbox" checked={settings.conservative_review===true} onChange={e=>save({conservative_review:e.target.checked})}/>开启保守复看（默认关闭）</label>
  <p className="hint">拟判正确的答案使用原始题区独立复看，两次一致且没有疑点才自动判正确。复看使用作业模型，会增加调用和用量；存在分歧时保留两次识别内容供教师核实。并行度限制同时处理的页面，已入队配置保持不变。</p>
  <p className="hint">可选1至13页，批处理默认{DEFAULT_BATCH_PARALLELISM}页，流式默认{DEFAULT_CAPTURE_PARALLELISM}槽。实际速度取决于当前账户和设备；不会增加可用额度。</p>
  <p className="hint">流式与批处理分别限流，互不影响并发设置。流式逐张确认、立即排队，有空位即开始；结果按拍摄顺序排列，未完成页保留占位。两种模式同时运行会同时使用账户额度；新设置只影响之后开始的任务。</p>
  <label className="check"><input type="checkbox" checked={settings.auto_orient_images!==false} onChange={e=>save({auto_orient_images:e.target.checked})}/>导入或拍摄后自动摆正文字方向（默认）</label>
  <p className="hint">使用本地文字方向检测，原图保留。方向证据不足会提示人工检查；开关仅影响后续导入和“重新检查正向”。</p>
  <label>拍摄间隔（秒）<input type="number" min="0" max="60" step="1" value={delay} aria-invalid={!validDelay} onChange={e=>Delay(e.target.value)} onBlur={()=>{if(validDelay&&Number(delay)!==(settings.capture_countdown_seconds??2))save({capture_countdown_seconds:Number(delay)})}}/></label>
  <p className="hint">默认2秒，可设0至60秒，新开采集会话生效。0秒立即准备下一张；仍需按 Enter 或点击拍摄，不会自动拍照。</p>
  {!validDelay&&<p role="alert" className="danger">请输入0至60的整数秒。</p>}
 </section>
}
