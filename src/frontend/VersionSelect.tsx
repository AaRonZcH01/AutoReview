import {useRef} from 'react';
type R=Record<string,any>;
export function VersionSelect({versions,currentId,value,onChange}:{versions:R[],currentId:string,value:string,onChange:(id:string)=>void}){
 const root=useRef<HTMLDetailsElement>(null),current=versions.find(v=>v.id===(value||currentId));
 const choices=[{id:'',name:'当前正式版本（默认）',official:true},...versions.filter(v=>v.id!==currentId)];
 return <details ref={root} className='version-select' onKeyDown={e=>{if(e.key==='Escape'){e.currentTarget.open=false;e.currentTarget.querySelector('summary')?.focus()}if(['ArrowDown','ArrowUp'].includes(e.key)){e.preventDefault();const buttons=Array.from(e.currentTarget.querySelectorAll<HTMLButtonElement>('button'));const i=buttons.indexOf(document.activeElement as HTMLButtonElement);buttons[(i+(e.key==='ArrowDown'?1:-1)+buttons.length)%buttons.length]?.focus()}}}><summary aria-label='选择答案版本'>{value?(current?.name||'答案 v'+current?.version):'当前正式版本（默认）'}</summary><div role='listbox' aria-label='答案版本列表'>{choices.map(v=><button role='option' aria-selected={value===v.id} key={v.id} onClick={()=>{onChange(v.id);if(root.current)root.current.open=false}}><span>{v.name||'答案 v'+v.version}</span><small>{v.official?'默认用于接下来的操作':'历史版本 · 只读 · 不会自动成为正式版'}</small></button>)}</div></details>
}
