type R=Record<string,any>;
export class TaskSessionCache {
 private owner='';readonly tasks=new Map<string,R>();readonly reports=new Map<string,{token:string,value:R}>();readonly views=new Map<string,R>();readonly selections=new Map<string,string[]>();
 get library(){return this.owner}
 bind(library:string){if(this.owner&&this.owner!==library){this.tasks.clear();this.reports.clear();this.views.clear();this.selections.clear();decoded.clear();pending.clear();pixels=0}this.owner=library}
 put(value:R){this.tasks.set(value.task.id,value)}
 invalidate(task:string){const value=this.tasks.get(task);if(value)this.tasks.set(task,{...value,cache_token:''});for(const key of this.reports.keys())if(key.startsWith(task+':'))this.reports.delete(key)}
}
export const taskCache=new TaskSessionCache();
const decoded=new Map<string,HTMLImageElement>(),pending=new Set<string>();let pixels=0;
export function warmImage(src:string){if(!src.startsWith('/api/'))return;const old=decoded.get(src);if(old){decoded.delete(src);decoded.set(src,old);return}if(pending.has(src))return;pending.add(src);const img=new Image();img.onerror=()=>pending.delete(src);img.onload=()=>{pending.delete(src);if(decoded.has(src))return;const cost=img.naturalWidth*img.naturalHeight;decoded.set(src,img);pixels+=cost;while((pixels>24_000_000||decoded.size>48)&&decoded.size>1){const key=decoded.keys().next().value!;const prior=decoded.get(key)!;pixels-=prior.naturalWidth*prior.naturalHeight;decoded.delete(key)}};img.src=src;}
