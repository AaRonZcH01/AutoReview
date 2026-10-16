import {matchingSummary,nodeName} from './questionTypes.js';
type R=Record<string,any>;
const answerText=(value:any,separator=' → '):string=>Array.isArray(value)?value.map(v=>String(v)).join(separator):value==null?'':String(value);
export function reviewReferenceText(a:R){
 const spec=a.answer_spec||{},rule=a.matching_rule||spec;
 if(a.kind==='matching'&&rule.allowed_edges?.length)return matchingSummary({...a,matching_rule:{...rule,nodes:rule.nodes||[]}});
 const value=a.english!==''&&a.english!=null?a.english:['rank','sequence','order','expected','accepted','symbol'].map(k=>spec[k]).find(v=>v!==''&&v!=null&&(!Array.isArray(v)||v.length));
 return answerText(value,a.kind==='ordering'?' → ':'、')||'该历史版本未填写参考答案';
}
export function reviewStudentText(a:R){
 // A teacher's corrected observation must never become the model's transcript.
 const raw=a.machine_raw_english;
 if(raw!=null&&String(raw).trim())return String(raw);
 const observation='machine_observation' in a?a.machine_observation:!a.teacher_override?a.observation:null;
 if(observation){
  if(a.kind==='matching'||a.response_encoding==='edges'){
   const nodes:R[]=[...(a.observed_structure?.nodes||[]),...(a.matching_rule?.nodes||a.answer_spec?.nodes||[])];
   const label=(id:string)=>{const n=nodes.find(n=>n.id===id);return n?nodeName(n):id||'未知端点'};
   const edges:R[]=observation.edges||[];
   if(edges.length)return edges.map(e=>label(e.from_node)+' → '+label(e.to_node)+(e.erased?'（已擦除）':e.uncertain?'（待核实）':'')).join('；');
   return observation.uncertain?'连线未识别清楚':'未观察到连线';
  }
  if(a.kind==='ordering'||a.response_encoding==='rank'){const value=answerText(observation.rank);if(value)return value}
  if(a.kind==='true_false'||a.response_encoding==='symbol'){const value=answerText(observation.symbol);if(value)return value}
  if(['choice','letter_choice','marked_choice'].includes(a.kind)||['written_choice','marked_choice','mixed'].includes(a.response_encoding)){
   const marked=answerText(observation.selected_options,'、'),written=answerText(observation.written_options,'、');
   if(marked&&written&&marked!==written)return '圈选 '+marked+'；填写 '+written;
   if(marked||written)return marked||written;
  }
 }
 return raw==null?'未读出':'（空白）';
}
