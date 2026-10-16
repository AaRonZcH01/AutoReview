import {reviewStudentText} from './reviewAnswerText.js';
import './verificationEvidence.css';
type R=Record<string,any>;
export function VerificationEvidence({answer}:{answer:R}){
 const evidence=answer.verification;if(!evidence)return null;
 const passed=evidence.status==='verified';
 const text=(cell:R|null)=>cell?reviewStudentText({...answer,machine_raw_english:cell.raw_english,machine_observation:cell}):'未获得有效观察';
 const form=(cell:R|null)=>cell&&(cell.selected_options?.length||cell.written_options?.length)?`圈选：${cell.selected_options?.join('、')||'未记录'}；填写：${cell.written_options?.join('、')||'未记录'}`:'';
 const observation=(label:string,cell:R|null)=><div><span>{label}</span><strong>{text(cell)}</strong>{form(cell)&&<small>{form(cell)}</small>}{cell?.response_reason&&<small>{cell.response_reason}</small>}</div>;
 return <details className={'verification-evidence '+(passed?'verified':'pending')} open={passed?undefined:true}>
  <summary>{passed?'独立复看：两次作答一致':'独立复看：需教师核实'}</summary>
  {evidence.reason&&<p>{evidence.reason}</p>}
  <div className='verification-comparison'>{observation('首次识别',evidence.first)}{observation('局部图片复看',evidence.second)}</div>
  {!!evidence.differences?.length&&<ul>{evidence.differences.map((value:string,i:number)=><li key={i}>{value}</li>)}</ul>}
  {!!evidence.form_differences?.length&&<p>{evidence.form_differences.join('；')}</p>}
  <small>{passed?'复看未收到参考答案或首次结论；两次一致仍可由教师改判。':'原始观察均已保留；不会凭首次拟正确结果自动得分。'}</small>
 </details>;
}
