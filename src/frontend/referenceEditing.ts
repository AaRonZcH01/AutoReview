type R=Record<string,any>;
export const signature=(a:R)=>JSON.stringify(Object.fromEntries(Object.entries(a).filter(([k])=>!['geometry_checked'].includes(k))));
export const modification=(a:R,p:R|undefined)=>!p?.baseline?'':!p.baseline[a.id]?'新增':p.baseline[a.id]!==signature(a)?'修改':'';
export function copyAt(source:R,point:{x:number,y:number},target:{slot:number,pageId:string,width:number,height:number},items:R[]):R{
 const a=structuredClone(source.item),base=a.answer_zone||a.box;if(!base)throw Error('所复制答案尚无完整作答区域，请先补齐位置。');
 const oldSlot=a.slot,oldPage=source.pageId,sx=source.width/target.width,sy=source.height/target.height;
 const regions:any[]=[base,a.evidence_box,...(a.options||[]).filter((o:R)=>!o.page_id||o.page_id===oldPage).map((o:R)=>o.box),...(a.ignored_regions||[]).map((o:R)=>o.box),...(a.matching_rule?.nodes||[]).filter((n:R)=>n.slot===oldSlot).map((n:R)=>n.box)].filter(Boolean);
 const minX=Math.min(...regions.map(b=>b.x1*sx)),maxX=Math.max(...regions.map(b=>b.x2*sx)),minY=Math.min(...regions.map(b=>b.y1*sy)),maxY=Math.max(...regions.map(b=>b.y2*sy));
 if(maxX-minX>1||maxY-minY>1)throw Error('目标页放不下整份答案区域，请选择更大的页面；未缩小或裁切。');
 const dx=Math.max(-minX,Math.min(1-maxX,point.x-base.x1*sx)),dy=Math.max(-minY,Math.min(1-maxY,point.y-base.y1*sy));
 const place=(b:R|null)=>b?{x1:b.x1*sx+dx,y1:b.y1*sy+dy,x2:b.x2*sx+dx,y2:b.y2*sy+dy}:null;
 a.id=crypto.randomUUID();a.option_group_id=null;a.slot=target.slot;a.geometry_checked=false;a.answer_zone=place(base);a.box=a.answer_zone;a.evidence_box=place(a.evidence_box);
 a.options=(a.options||[]).map((o:R)=>({...o,region_id:crypto.randomUUID(),page_id:!o.page_id||o.page_id===oldPage?target.pageId:o.page_id,box:!o.page_id||o.page_id===oldPage?place(o.box):o.box}));
 a.ignored_regions=(a.ignored_regions||[]).map((r:R)=>({...r,box:place(r.box)}));
 if(a.matching_rule){const ids:R={};for(const n of a.matching_rule.nodes)ids[n.id]=crypto.randomUUID();a.matching_rule.nodes=a.matching_rule.nodes.map((n:R)=>({...n,id:ids[n.id],slot:n.slot===oldSlot?target.slot:n.slot,box:n.slot===oldSlot?place(n.box):n.box}));a.matching_rule.allowed_edges=a.matching_rule.allowed_edges.map((edge:string[])=>edge.map(n=>ids[n]))}
 a.dependencies=[...new Set((a.dependencies||[]).filter((s:number)=>s!==target.slot))];
 let number=Number(a.number)||1;while(items.some(x=>x.slot===a.slot&&x.section===a.section&&x.number===String(number)&&(x.part_id||'')===(a.part_id||'')))number++;a.number=String(number);return a;
}

export const optionModification=(a:R,o:R,p:R|undefined)=>{const baseline=p?.baseline?.[a.id];if(!baseline)return p?.baseline?'新增':'';try{const old=JSON.parse(baseline).options?.find((x:R)=>x.id===o.id);return !old?'新增':JSON.stringify(old)!==JSON.stringify(o)?'修改':''}catch{return ''}};
export const onPage=(a:R,slot:number,ids:string[])=>a.slot===slot||(a.dependencies||[]).includes(slot)||(a.matching_rule?.nodes||[]).some((n:R)=>n.slot===slot)||(a.options||[]).some((o:R)=>o.page_id===ids[slot-1]);
