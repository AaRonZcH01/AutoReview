type R=Record<string,any>;
export const galleryCategories=[['all','全部图片'],['unreviewed','未核实'],['reviewed','已核实'],['retake','需要重拍'],['photo','照片待确认']];
export function galleryMatches(page:R,filter:string){
 if(filter==='unreviewed')return !page.reviewed;
 if(filter==='reviewed')return Boolean(page.reviewed);
 if(filter==='retake')return page.photo_status==='retake_required';
 if(filter==='photo')return page.photo_status==='pending';
 return true;
}
// Keep the entry order even when confirming removes a page from its filter.
export function reviewScopePages(pages:R[],ids:string[]){const byId=new Map(pages.map(p=>[p.id,p]));return ids.flatMap(id=>byId.has(id)?[byId.get(id)!]:[])}
