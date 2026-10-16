import {pendingQuestionCount,pageProcessing,processingIndex} from './pageProcessing.js';
type R=Record<string,any>;
export function capturePageStatus(page:R,detail:R){
 const pending=pendingQuestionCount(page),suffix=pending==null?'':` · 待核实 ${pending} 题`;
 if(page.capture_withheld)return '已撤回识别 · 可重新提交';
 if(page.photo_status==='retake_required')return '照片需要重拍'+suffix;
 if(page.photo_status==='pending')return '照片可读性待确认'+suffix;
 if(page.reviewed)return '已复核'+(pending?` · 保留待核实 ${pending} 题`:'');
 if(page.state==='processed'){const count=pendingQuestionCount(page);return '已判分 · 待复核 · '+(count==null?'待核实题数读取中':`待核实 ${count} 题`)}
 const progress=pageProcessing(page,processingIndex(detail));
 if(progress.kind==='paused')return '识别已暂停 · 保留原位';
 if(['cloud','local','queued','failed'].includes(progress.kind))return progress.label;
 if(page.capture_delivery_mode==='local')return progress.label;
 if(page.capture_dependency_wait)return '等待补正跨页照片';
 if(!(page.preparation||{}).confirmed)return '待核对照片与作答区';
 return '等待必要跨页图片';
}
