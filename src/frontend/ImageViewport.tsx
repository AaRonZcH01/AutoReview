import React,{useEffect,useLayoutEffect,useRef,useState} from 'react';
import {warmImage} from './sessionCache.js';
type Rect={x1:number,y1:number,x2:number,y2:number};
type Props={focusBox?:Rect,focusKey?:string,focusZoom?:boolean,src:string,alt?:string,children?:React.ReactNode,editing?:boolean,onSize?:(w:number,h:number)=>void,navigation?:{x:number,y:number,zoom:number,sequence:number},onNavigate?:(v:{x:number,y:number,zoom:number})=>void};
export function ImageViewport(props:Props){return <ViewportImage key={props.src} {...props}/>}
function ViewportImage({src,alt='作业图片',children,editing=false,focusBox,focusKey,focusZoom=false,onSize,navigation,onNavigate}:Props){
 useEffect(()=>{warmImage(src)},[src]);
 const view=useRef<HTMLDivElement>(null),image=useRef<HTMLImageElement>(null),reported=useRef(''),drag=useRef<any>(null),anchor=useRef<any>(null);
 const [zoom,Z]=useState(1),[loadedSrc,LoadedSrc]=useState(''),[size,S]=useState({w:0,h:0}),[bounds,B]=useState({w:1,h:1}),[panning,P]=useState(false),[failed,F]=useState(false),[retry,Retry]=useState(0);
 const fit=size.w?Math.min(bounds.w/size.w,bounds.h/size.h):1;
 const width=size.w*fit*zoom,height=size.h*fit*zoom,maxZoom=Math.max(8,1/fit);
 const offsetX=(w:number)=>focusZoom?bounds.w/2:Math.max(0,(bounds.w-w)/2),offsetY=(h:number)=>focusZoom?bounds.h/2:Math.max(0,(bounds.h-h)/2);
 const state=useRef({zoom,width,height,maxZoom,bounds});state.current={zoom,width,height,maxZoom,bounds};
 useEffect(()=>{const el=view.current!;const measure=()=>{const w=Math.max(1,el.clientWidth),h=Math.max(1,el.clientHeight);B(old=>Math.abs(old.w-w)<.5&&Math.abs(old.h-h)<.5?old:{w,h})};const observer=new ResizeObserver(measure);observer.observe(el);measure();return()=>observer.disconnect()},[]);
 // Source changes remount this state. A passive reset used to erase an earlier
 // cached image load, leaving a decoded image at 1 px behind "正在加载图片".
 const acceptImage=(el:HTMLImageElement)=>{if(el!==image.current||el.getAttribute('src')!==src||!el.naturalWidth)return;F(false);LoadedSrc(src);S(old=>old.w===el.naturalWidth&&old.h===el.naturalHeight?old:{w:el.naturalWidth,h:el.naturalHeight});const receipt=src+':'+retry+':'+el.naturalWidth+':'+el.naturalHeight;if(reported.current!==receipt){reported.current=receipt;onSize?.(el.naturalWidth,el.naturalHeight)}};
 useLayoutEffect(()=>{const el=image.current;if(!el)return;F(false);if(el.complete){if(el.naturalWidth)acceptImage(el);else F(true)}else{S({w:0,h:0});LoadedSrc('')}},[src,retry]);
 useEffect(()=>{const timer=setTimeout(()=>{const el=image.current;if(!el)return;if(el.complete&&el.naturalWidth)acceptImage(el);else F(true)},30000);return()=>clearTimeout(timer)},[src,retry]);
 useEffect(()=>()=>{if(navTimer.current)clearTimeout(navTimer.current)},[]);
 const zoomAt=(next:number,x?:number,y?:number)=>{const el=view.current;if(!el)return;const v=state.current;next=Math.max(.25,Math.min(v.maxZoom,next));if(!v.width||next===v.zoom)return;x??=el.clientWidth/2;y??=el.clientHeight/2;anchor.current={px:(el.scrollLeft+x-offsetX(v.width))/v.width,py:(el.scrollTop+y-offsetY(v.height))/v.height,x,y};Z(next)};
 const navigating=useRef(false),navTimer=useRef<ReturnType<typeof setTimeout>|null>(null);
 const emit=()=>{if(navigating.current||!onNavigate||!view.current||!width)return;const el=view.current;onNavigate({x:(el.scrollLeft+el.clientWidth/2-offsetX(width))/width,y:(el.scrollTop+el.clientHeight/2-offsetY(height))/height,zoom})};
 useEffect(()=>{if(!navigation||!size.w)return;navigating.current=true;const z=Math.max(.25,Math.min(maxZoom,navigation.zoom));Z(z);const frame=requestAnimationFrame(()=>{const el=view.current;if(el){el.scrollLeft=navigation.x*size.w*fit*z+offsetX(size.w*fit*z)-el.clientWidth/2;el.scrollTop=navigation.y*size.h*fit*z+offsetY(size.h*fit*z)-el.clientHeight/2}if(navTimer.current)clearTimeout(navTimer.current);navTimer.current=setTimeout(()=>{navigating.current=false},60)});return()=>{cancelAnimationFrame(frame);navigating.current=false}},[navigation?.sequence,size.w]);
 useEffect(()=>{emit()},[zoom]);
 const zoomAction=useRef(zoomAt);zoomAction.current=zoomAt;
 useLayoutEffect(()=>{const a=anchor.current,el=view.current;if(a&&el){el.scrollLeft=a.px*width+offsetX(width)-a.x;el.scrollTop=a.py*height+offsetY(height)-a.y;anchor.current=null}},[zoom,width,height,bounds]);
 useEffect(()=>{const el=view.current!;const wheel=(e:WheelEvent)=>{if(!e.ctrlKey||!el.contains(document.activeElement))return;e.preventDefault();e.stopPropagation();const r=el.getBoundingClientRect();zoomAction.current(state.current.zoom*Math.exp(-e.deltaY*.002),e.clientX-r.left,e.clientY-r.top)};el.addEventListener('wheel',wheel,{passive:false});return()=>el.removeEventListener('wheel',wheel)},[]);
 useLayoutEffect(()=>{const el=view.current;if(!el||!size.w||(!focusBox&&!focusZoom))return;
  // Explicitly clearing review selection means whole-page view. Cancel pending
  // positioning on every key/source change so a late frame cannot jump back.
  const box=focusBox||{x1:0,y1:0,x2:1,y2:1};
  const z=!focusBox?1:focusZoom?Math.max(1,Math.min(maxZoom,2.5,el.clientWidth*.78/(size.w*fit*Math.max(.01,box.x2-box.x1)),el.clientHeight*.74/(size.h*fit*Math.max(.01,box.y2-box.y1)))):state.current.zoom;
  const w=size.w*fit*z,h=size.h*fit*z;
  anchor.current=null;Z(z);const frame=requestAnimationFrame(()=>{if(view.current!==el)return;el.scrollLeft=(box.x1+box.x2)*w/2+offsetX(w)-el.clientWidth/2;el.scrollTop=(box.y1+box.y2)*h/2+offsetY(h)-el.clientHeight/2});return()=>cancelAnimationFrame(frame);
 },[focusKey,focusBox?.x1,focusBox?.x2,focusBox?.y1,focusBox?.y2,size.w,size.h,bounds.w,bounds.h]);
 return <div className="image-viewport"><div className="viewport-controls"><span>缩放</span><input aria-label="图片缩放" type="range" min=".25" max={maxZoom} step=".01" value={zoom} onChange={e=>zoomAt(+e.target.value)}/><span>{Math.round(zoom*100)}% 窗口倍率</span><button onClick={()=>{anchor.current=null;Z(1);if(focusZoom)requestAnimationFrame(()=>{if(view.current){view.current.scrollLeft=size.w*fit/2;view.current.scrollTop=size.h*fit/2}})}}>适合窗口</button><button disabled={!size.w} onClick={()=>zoomAt(1/fit)}>实际像素</button><small>{editing?'编辑区域 · 拖框或四角手柄':'点击图片 · Ctrl+滚轮缩放 · 放大后左键拖图'}</small></div>
 <div ref={view} onScroll={emit} tabIndex={0} aria-label={alt+'查看区域'} className={'image-viewport-scroll '+(editing?'editing':panning?'panning':'browsing')} onPointerDown={e=>{if(e.button!==0)return;const el=e.currentTarget;el.focus({preventScroll:true});if(editing||!(el.scrollWidth>el.clientWidth+1||el.scrollHeight>el.clientHeight+1))return;drag.current={x:e.clientX,y:e.clientY,left:el.scrollLeft,top:el.scrollTop};P(true);el.setPointerCapture(e.pointerId);e.preventDefault()}} onPointerMove={e=>{if(!drag.current)return;e.currentTarget.scrollLeft=drag.current.left+drag.current.x-e.clientX;e.currentTarget.scrollTop=drag.current.top+drag.current.y-e.clientY}} onPointerUp={()=>{drag.current=null;P(false)}} onPointerCancel={()=>{drag.current=null;P(false)}}>
 {!size.w&&!failed&&<span className="image-load-state">正在加载图片…</span>}{failed&&<div className="image-load-state" role="alert">{alt}加载失败 <button onClick={()=>{F(false);Retry(retry+1)}}>重新加载</button></div>}
 <div className="viewport-stage" style={{width:focusZoom?width+bounds.w:Math.max(bounds.w,width),height:focusZoom?height+bounds.h:Math.max(bounds.h,height)}}><div className="geometry-canvas viewport-image" style={{width:width||1,height:height||1,visibility:failed?'hidden':'visible'}}><img ref={image} key={src+retry} src={src} alt={alt} draggable={false} onError={()=>F(true)} onLoad={e=>acceptImage(e.currentTarget)}/>{size.w>0&&loadedSrc===src&&children}</div></div></div></div>
}
