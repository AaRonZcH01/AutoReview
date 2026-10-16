type R=Record<string,any>;
export const pageIds=(d:R):string[]=>d.asset_ids.map((id:string,i:number)=>d.page_ids?.[i]||id);
export function reorderPages(d:R,order:number[]):R{
 const map=new Map(order.map((old,i)=>[old+1,i+1]));
 const remap=(items:R[])=>(items||[]).filter(a=>map.has(a.slot)).map(a=>({...a,slot:map.get(a.slot),geometry_checked:false,
  uncertain:a.uncertain||(a.dependencies||[]).some((n:number)=>!map.has(n)),
  dependencies:(a.dependencies||[]).filter((n:number)=>map.has(n)).map((n:number)=>map.get(n)),
  matching_rule:a.matching_rule?{...a.matching_rule,nodes:a.matching_rule.nodes.filter((n:R)=>map.has(n.slot)).map((n:R)=>({...n,slot:map.get(n.slot)})),allowed_edges:a.matching_rule.allowed_edges.filter((edge:string[])=>edge.every(id=>a.matching_rule.nodes.some((n:R)=>n.id===id&&map.has(n.slot))))}:null}));
 return {asset_ids:order.map(i=>d.asset_ids[i]),page_ids:order.map(i=>pageIds(d)[i]),page_count:Math.max(1,order.length),
  page_labels:order.map((i,j)=>d.page_labels?.[i]||'第'+(j+1)+'页'),items:remap(d.items),machine_candidates:remap(d.machine_candidates)};
}
