import {useEffect,useId,useRef,useState,RefObject,ReactNode} from 'react';
import {cameraInfo,CameraInfo} from './cameraTransport.js';
import {CameraParameters} from './CameraParameters.js';

export function CameraPreview({video,info,ready,compact=false,sideContent}:{video:RefObject<HTMLVideoElement|null>,info:CameraInfo|null,ready:boolean,compact?:boolean,sideContent?:ReactNode}){
 const viewport=useRef<HTMLDivElement>(null),drag=useRef<{x:number,y:number,left:number,top:number}|null>(null);
 const [width,Width]=useState(900),[height,Height]=useState(480),[mode,Mode]=useState<'fit'|'pixels'|'zoom'>('fit'),[zoom,Zoom]=useState(1),[large,Large]=useState(false);
 const [fps,Fps]=useState<number|null>(null),[,ControlsVersion]=useState(0);
 const [panel,Panel]=useState<'capture'|'parameters'|'help'>('capture'),panelId=useId();
 useEffect(()=>{
  const element=video.current;if(!element||!ready){Fps(null);return}let frames=0,previous=performance.now(),callback=0,disposed=false;
  const next=()=>{if(disposed)return;frames++;callback=element.requestVideoFrameCallback(next)};
  if(element.requestVideoFrameCallback)callback=element.requestVideoFrameCallback(next);
  const timer=setInterval(()=>{const time=performance.now(),visible=element.getClientRects().length>0;
   Fps(visible&&frames?Math.round(frames*10000/(time-previous))/10:null);frames=0;previous=time;
  },2000);return()=>{disposed=true;clearInterval(timer);if(callback)element.cancelVideoFrameCallback(callback)};
 },[ready,video]);
 useEffect(()=>{const element=viewport.current;if(!element)return;const observer=new ResizeObserver(entries=>{const value=entries[0].contentRect;if(value.width>0)Width(value.width);if(value.height>0)Height(value.height)});observer.observe(element);return()=>observer.disconnect()},[]);
 useEffect(()=>{if(viewport.current){viewport.current.scrollLeft=0;viewport.current.scrollTop=0}},[mode,info?.width,info?.height]);
 const details=ready&&video.current?.srcObject?cameraInfo(video.current,video.current.srcObject as MediaStream):info;
 const w=details?.width||1280,h=details?.height||720,fit=Math.min(Math.max(1,width)/w,Math.max(1,height)/h,1),scale=mode==='fit'?fit:mode==='pixels'?1:zoom;
 return <div className={'camera-preview camera-preview-workspace '+(compact?'camera-preview-compact ':'')+(large?'camera-preview-large':'')}><div className='camera-preview-main'>
  <div className='camera-preview-bar'><div className='actions wrap'>
   <button aria-pressed={mode==='fit'} onClick={()=>Mode('fit')}>完整画面</button><button disabled={!ready} aria-pressed={mode==='pixels'} onClick={()=>Mode('pixels')}>原始像素</button>
   <label>预览缩放<input aria-label='相机预览缩放' disabled={!ready} type='range' min='.25' max='1.5' step='.05' value={scale} onChange={e=>{Zoom(Number(e.target.value));Mode('zoom')}}/><span>{Math.round(scale*100)}%</span></label>
  </div><button onClick={()=>Large(!large)}>{large?'收起大屏预览':'大屏预览'}</button></div>
  <div className='camera-preview-scroll' ref={viewport} onPointerDown={e=>{if(e.button!==0||mode==='fit')return;const el=e.currentTarget;drag.current={x:e.clientX,y:e.clientY,left:el.scrollLeft,top:el.scrollTop};el.setPointerCapture(e.pointerId)}} onPointerMove={e=>{const point=drag.current;if(point){e.currentTarget.scrollLeft=point.left-e.clientX+point.x;e.currentTarget.scrollTop=point.top-e.clientY+point.y}}} onPointerUp={()=>{drag.current=null}} onPointerCancel={()=>{drag.current=null}}>
   <div className='camera-preview-stage' style={{width:w*scale,height:h*scale}}><video ref={video} autoPlay playsInline muted aria-label='摄像头实时画面'/>{!ready&&<div className='camera-preview-wait'>等待实时画面。连接成功后才可拍摄。</div>}</div>
  </div>
  </div><div className='camera-preview-side'>{sideContent}<nav className='camera-sidebar-tabs' role='tablist' aria-label='相机信息栏目'>{([['capture','拍摄概览'],['parameters','相机参数'],['help','画面说明']] as const).map(([key,label],index)=><button key={key} id={panelId+'-'+key+'-tab'} role='tab' aria-selected={panel===key} aria-controls={panelId+'-'+key} tabIndex={panel===key?0:-1} onClick={()=>Panel(key)} onKeyDown={e=>{if(['ArrowLeft','ArrowRight'].includes(e.key)){e.preventDefault();const keys=['capture','parameters','help'] as const,next=(index+(e.key==='ArrowRight'?1:2))%3;Panel(keys[next]);(e.currentTarget.parentElement?.children[next] as HTMLElement)?.focus()}}}>{label}</button>)}</nav>
  <div className='camera-sidebar-panel' role='tabpanel' id={panelId+'-capture'} aria-labelledby={panelId+'-capture-tab'} hidden={panel!=='capture'}><div className='camera-preview-caption' title={details?.note}><strong>{ready?'镜头已就绪':'等待相机连接'}</strong><p>实时预览 {w} × {h}</p><p className='hint'>拍照时另取最高可用原生尺寸，实际尺寸见照片检查页。</p><p>{fps==null?'测量帧率中':`实测约 ${fps} fps`}</p><span>{details?.focus} · {details?.whiteBalance}</span><p className='hint'>先放好本张对应的试卷，检查完整入镜后拍摄。拍照后还可检查或重拍。</p></div></div>
  <div className='camera-sidebar-panel' role='tabpanel' id={panelId+'-parameters'} aria-labelledby={panelId+'-parameters-tab'} hidden={panel!=='parameters'}><CameraParameters video={video} ready={ready} inline changed={()=>ControlsVersion(n=>n+1)}/></div>
  <div className='camera-sidebar-panel camera-preview-help' role='tabpanel' id={panelId+'-help'} aria-labelledby={panelId+'-help-tab'} hidden={panel!=='help'}><h3>画面与拍照</h3><p>完整画面保持相机原始比例，不裁边、不镜像。</p><p>预览使用流畅模式，快门时使用最高可用原生分辨率。放大或缩小预览不会改变照片清晰度。</p><h3>检查清晰度</h3><p>让镜头正对纸面、压平试卷，使用均匀照明并避开反光。拍照后检查四角细字，再确认使用。</p><h3>快捷键</h3><p><kbd>Enter</kbd> 拍摄或确认本张。<br/><kbd>Esc</kbd> 停止或关闭拍摄。</p></div>
 </div></div>;
}
