type R=Record<string,any>;
export function ClassPicker({presets,existing=[],value,change}:{presets:R[],existing?:R[],value:R,change:(v:R)=>void}){
 const ids:string[]=value.preset_ids||[];
 return <fieldset><legend>选择预设班级或添加临时班级</legend><div className="preset-grid">{presets.map(c=>{const joined=existing.some(g=>g.preset_id===c.id||g.name===c.name);return <label className="check" key={c.id}><input type="checkbox" disabled={joined} checked={joined||ids.includes(c.id)} onChange={e=>change({...value,preset_ids:e.target.checked?[...ids,c.id]:ids.filter(id=>id!==c.id)})}/>{c.name}{joined?'（已加入）':''}</label>})}</div>{!presets.length&&<p>尚无预设班级，可以在下方输入并加入预设。</p>}<label>临时班级／分组<input value={value.custom||''} placeholder="例如：二年级1班" onChange={e=>change({...value,custom:e.target.value})}/></label>{value.custom?.trim()&&<label className="check"><input type="checkbox" checked={!!value.save_preset} onChange={e=>change({...value,save_preset:e.target.checked})}/>加入预设班级，供以后选择</label>}</fieldset>
}
