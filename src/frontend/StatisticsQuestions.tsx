import {useEffect,useRef,useState} from 'react';
import {ImageViewport} from './ImageViewport.js';
import {versionName} from './ReferenceVersionPreview.js';
import './statisticsQuestions.css';
type R=Record<string,any>;
function locatorBox(q:R){const b=q.locator?.box;return b&&[b.x1,b.y1,b.x2,b.y2].every(Number.isFinite)&&b.x2>b.x1&&b.y2>b.y1?b:null}
function QuestionLocation({question:q,close}:{question:R,close:()=>void}){
 const ref=useRef<HTMLDivElement>(null),[whole,Whole]=useState(false);const b=locatorBox(q);
 const context=b?{x1:Math.max(0,b.x1-.05),y1:Math.max(0,b.y1-.06),x2:Math.min(1,b.x2+.05),y2:Math.min(1,b.y2+.06)}:undefined;
 useEffect(()=>{const old=document.activeElement as HTMLElement;ref.current?.focus();const key=(e:KeyboardEvent)=>{if(e.key==='Escape'){e.stopPropagation();close()}};addEventListener('keydown',key);return()=>{removeEventListener('keydown',key);old?.focus()}},[]);
 return <div className='statistics-location-backdrop'><div ref={ref} tabIndex={-1} role='dialog' aria-modal='true' aria-label='统计题目图示' className='statistics-location' onKeyDown={e=>{if(e.key!=='Tab')return;const els=ref.current?.querySelectorAll<HTMLElement>('button,input,[tabindex="0"]');if(!els?.length)return;const first=els[0],last=els[els.length-1];if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus()}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus()}}}>
  <header><div><h2>{q.label||`第${q.slot}页 · 第${q.number}题`}</h2><p>{q.locator?.page_label||'题目位置'} · 参考答案：<b>{q.expected_display||q.expected||'未提供'}</b></p></div><div className='actions'>{b&&<button onClick={()=>Whole(!whole)}>{whole?'定位整题':'查看全页'}</button>}<button onClick={close}>关闭图示</button></div></header>
  <ImageViewport key={String(whole)} src={'/api/assets/'+q.locator.asset_id+'/display'} alt='统计题目位置图' focusBox={whole?undefined:context} focusKey={q.question_id} focusZoom={!whole&&Boolean(b)}>
   {b&&<svg className='statistics-location-mark' viewBox='0 0 1 1' preserveAspectRatio='none'><rect x={b.x1} y={b.y1} width={b.x2-b.x1} height={b.y2-b.y1} fill='#7353b61a' stroke='#7353b6' strokeWidth='3' vectorEffect='non-scaling-stroke'/></svg>}
  </ImageViewport><p className='hint'>{b?'紫框标明整题位置，可缩放、拖动查看上下文。':'该历史记录缺少可靠整题位置，当前显示整页，请自行查找题目。'}图示来自本次批改所用的答案快照或对应作业页。</p>
 </div></div>
}
export function StatisticsQuestions({questions,mixed,detail}:{questions:R[],mixed:boolean,detail:R}){
 const [current,Current]=useState<R|null>(null),[errorsOnly,ErrorsOnly]=useState(true);
 const visible=questions.filter(q=>!errorsOnly||q.errors>0);
 return <section className='statistics-questions' aria-label='题目错误分析'><div className='statistics-question-tools'><span>按错误率从高到低 · {visible.length} 题</span><label><input type='checkbox' checked={errorsOnly} onChange={e=>ErrorsOnly(e.target.checked)}/>只看有错误的题目</label></div>
  <div className='statistics-question-head'><span>题目与图示</span><span>参考答案</span><span>常见错误 · 按人数排序</span><span>错误人数 / 有效人数</span></div>
  {!visible.length&&<p className='empty'>{questions.length?'当前完整复核作业中没有错误题目。':'完整复核一份作业后即可查看错误分析。'}</p>}
  {visible.map(q=>{const b=locatorBox(q),answers:R[]=q.wrong_answers||[];return <article className='statistics-question' key={q.reference_version_id+':'+q.question_id}>
   <div className='statistics-question-name'>{q.locator?.asset_id&&<button className='statistics-question-picture' title={b?'打开大图并定位整题':'打开整页图片'} aria-label={'查看图示 '+q.label} onClick={()=>Current(q)}><img loading='lazy' src={'/api/assets/'+q.locator.asset_id+'/thumbnail'} alt='题目位置缩略图'/>{b&&<svg viewBox='0 0 1 1' preserveAspectRatio='none'><rect x={b.x1} y={b.y1} width={b.x2-b.x1} height={b.y2-b.y1} fill='#7353b633' stroke='#7353b6' strokeWidth='2' vectorEffect='non-scaling-stroke'/></svg>}<span>{b?'放大定位 ↗':'查看整页 ↗'}</span></button>}<div><b>{q.label||`第${q.slot}页 · 第${q.number}题`}</b><small>{q.kind_label||'题目'}{mixed?' · '+versionName(detail,q.reference_version_id):''}</small>{!q.locator?.asset_id&&<small>该历史题目未保存可用图示</small>}</div></div>
   <div className='statistics-question-answer'><span className='statistics-mobile-label'>参考答案</span>{q.expected_display||q.expected||'未提供'}</div>
   <div className='statistics-wrong-answers'>{answers.length?<><div>{answers.slice(0,3).map((a,i)=><span key={i}><em>{a.text}</em><b>{a.count} 人</b></span>)}</div>{answers.length>3&&<details><summary>另外 {answers.length-3} 种错误</summary><div>{answers.slice(3).map((a,i)=><span key={i}><em>{a.text}</em><b>{a.count} 人</b></span>)}</div></details>}</>:<small>{q.errors?'该旧记录未保存具体作答内容':'暂无错误'}</small>}</div>
   <div className='statistics-question-rate'><b>{q.errors} / {q.known}</b><span>{q.error_rate==null?'暂无有效判定':(q.error_rate*100).toFixed(1)+'%'}</span><meter aria-label='错误率' min='0' max='1' value={q.error_rate||0}/></div>
  </article>})}
  {current&&<QuestionLocation question={current} close={()=>Current(null)}/>}
 </section>
}
