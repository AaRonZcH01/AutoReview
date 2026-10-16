import {useEffect,useId,useRef,type ReactNode} from 'react';
import {createPortal} from 'react-dom';
import './confirmationDialog.css';

type Props={title:string,children:ReactNode,confirmLabel:string,onConfirm:()=>void,onCancel:()=>void,busy?:boolean,confirmDisabled?:boolean,cancelLabel?:string,tone?:'danger'|'primary',error?:string,returnFocus?:HTMLElement|null};

// A portal keeps confirmations out of card layout; the native top layer also
// makes the rest of the workspace inert, including other open panels.
export function ConfirmationDialog({title,children,confirmLabel,onConfirm,onCancel,busy=false,confirmDisabled=false,cancelLabel='取消',tone='primary',error,returnFocus}:Props){
 const dialog=useRef<HTMLDialogElement>(null),heading=useId();
 useEffect(()=>{
  const prior=returnFocus||document.activeElement as HTMLElement|null,current=dialog.current!;
  current.showModal();
  current.querySelector<HTMLButtonElement>('[data-confirmation-primary]:not(:disabled)')?.focus({preventScroll:true});
  return()=>{current.close();if(prior?.isConnected)prior.focus({preventScroll:true})};
 },[]);
 useEffect(()=>{
  if(busy)dialog.current?.focus({preventScroll:true});
  else dialog.current?.querySelector<HTMLButtonElement>('[data-confirmation-primary]:not(:disabled)')?.focus({preventScroll:true});
 },[busy]);
 return createPortal(<dialog ref={dialog} className='confirmation-dialog' role='dialog' tabIndex={-1} aria-modal='true' aria-labelledby={heading} aria-busy={busy} onCancel={e=>{e.preventDefault();if(!busy)onCancel()}} onClick={e=>e.stopPropagation()} onPointerDown={e=>e.stopPropagation()} onKeyDown={e=>{
  e.stopPropagation();
  if(e.key==='Tab'){
   const controls=Array.from(dialog.current!.querySelectorAll<HTMLElement>('button:not(:disabled),a[href],input:not(:disabled),select:not(:disabled),textarea:not(:disabled),[tabindex="0"]')).filter(el=>el.getClientRects().length);
   const first=controls[0],last=controls.at(-1);
   if(!first){e.preventDefault();dialog.current?.focus()}
   else if(e.shiftKey&&(document.activeElement===first||document.activeElement===dialog.current)){e.preventDefault();last?.focus()}
   else if(!e.shiftKey&&(document.activeElement===last||document.activeElement===dialog.current)){e.preventDefault();first.focus()}
  }
  if(e.key==='Escape'){e.preventDefault();if(!busy)onCancel()}
  if(e.key==='Enter'&&(e.repeat||e.nativeEvent.isComposing||busy))e.preventDefault();
 }}>
  <div className='confirmation-dialog-head'><h2 id={heading}>{title}</h2><button className='quiet' aria-label='关闭确认窗口' disabled={busy} onClick={onCancel}>×</button></div>
  <div className='confirmation-dialog-body'>{children}{error&&<p className='message error' role='alert'>{error}</p>}</div>
  <div className='confirmation-dialog-actions'><button disabled={busy} onClick={onCancel}>{cancelLabel}</button><button data-confirmation-primary className={tone} disabled={busy||confirmDisabled} onClick={onConfirm}>{busy?'正在执行，请稍候…':confirmLabel}</button></div>
 </dialog>,document.body);
}
