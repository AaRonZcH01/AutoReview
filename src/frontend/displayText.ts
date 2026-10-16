// Presentation only: official identifiers and persisted values remain unchanged.
type R=Record<string,any>;
const labels:Record<string,string>={
 low:'低',minimal:'最低',medium:'中等',high:'高',xhigh:'很高',max:'最高',ultra:'超高',none:'无',
 standard:'标准速度',fast:'快速模式',priority:'优先处理',default:'默认',auto:'自动',flex:'弹性处理',
 student:'学生作业',reference:'范本解析',writing:'书写检查',region_retry:'局部复看',
 orientation:'图片正向检查',deskew:'倾斜校正',rotate:'旋转',perspective:'四角透视校正',perspective_keep:'透视校正并保留画面',crop:'裁剪',restore:'恢复原件',disable:'停用步骤',automatic:'自动检查正向',
 completed:'已完成',disabled:'已关闭',skipped:'已跳过',failed:'失败',undone:'已撤销',reverted:'已撤销',cancelled:'已取消',canceled:'已取消',superseded:'已被新版替代',stale:'旧版本结果',interrupted:'已中断',active:'有效',deleted:'已删除',archived:'已归档',retake_required:'需要重拍',photo_pending:'照片待确认',
 pending:'待处理',unprocessed:'未处理',processed:'待复核',reviewed:'已复核',incomplete:'缺页',queued:'排队中',running:'处理中',paused:'已暂停',draft:'待确认',confirmed:'已确认',parsing:'解析中',correct:'正确',incorrect:'错误',missing:'漏答',unknown:'未知',
 needs_review:'待核对',not_applicable:'不适用',not_checked:'未检查',candidate:'待教师确认',accepted:'已接受',rejected:'已排除',visible:'可见',not_visible:'不可见',ambiguous:'无法确定',
 primary:'主要额度窗口',secondary:'次要额度窗口',free:'免费版',plus:'Plus 套餐',pro:'Pro 套餐',team:'团队版',business:'商业版',enterprise:'企业版',edu:'教育版',
 x1:'左边界',y1:'上边界',x2:'右边界',y2:'下边界',
};
export const displayText=(value:unknown,fallback?:string):string=>value==null?'未报告':labels[String(value)]||fallback||String(value);
export function operationDetail(log:R):string{
 const reason=String(log.reason||'').replace(/EXIF方向/g,'照片方向信息');
 if(reason)return reason;
 const p=log.parameters||{},parts:string[]=[];
 if(p.angle!=null)parts.push(`角度 ${Number(p.angle).toFixed(2)}°`);
 if(p.box)parts.push('保留区域：'+['x1','y1','x2','y2'].map(k=>`${displayText(k)} ${(p.box[k]*100).toFixed(1)}%`).join('，'));
 if(Array.isArray(p.points))parts.push('四角位置：'+p.points.map((v:number[],i:number)=>`${i+1}（${(v[0]*100).toFixed(1)}%，${(v[1]*100).toFixed(1)}%）`).join('、'));
 return parts.join('；');
}
const subjects:Record<string,string>={tasks:'任务',groups:'班级',folders:'文件夹',class_presets:'预设班级',asset:'图片',image:'图片',images:'批量图片',reference:'参考答案',intake:'导入',page:'作业页',review:'教师复核',run:'处理轮次',attempt:'模型调用',result:'处理结果',library:'资料库',report:'报告',timer:'纸本计时',paper_feedback:'纸本反馈',legacy:'旧版资料'};
const actions:Record<string,string>={created:'已创建',updated:'已更新',imported:'已导入',prepared:'已生成处理图',preparation_confirmed:'准备已确认',preparation_previewed:'已生成定位预览',batch_operation:'已调整',confirmed:'已确认',committed:'已保存',replaced:'已替换',edit:'已修改',patch:'已修改',undo:'已撤销',redo:'已重做',confirm:'已确认',reopen:'已重新打开',queued:'已入队',region_retry_queued:'局部复看已入队',recovered:'已恢复',recovery_paused:'恢复已暂停',user_stopped:'教师已停止',resumed:'已继续',completed:'已完成',started:'已开始',failed:'失败',stale:'旧结果已隔离',processed:'已处理',merged:'已合并',exported:'已导出',stopped:'已停止'};
export function eventText(value:string):string{const [subject,action]=value.split('.');return subjects[subject]&&actions[action]?`${subjects[subject]}${actions[action]}`:'其他运行事件'}
export function usageText(usage:R|null|undefined):string{
 if(!usage)return '未报告';
 const fields:Record<string,string>={input_tokens:'输入',output_tokens:'输出',total_tokens:'合计',cached_input_tokens:'缓存输入',reasoning_output_tokens:'推理输出',inputTokens:'输入',outputTokens:'输出',totalTokens:'合计',cachedInputTokens:'缓存输入',reasoningOutputTokens:'推理输出'};
 const result=Object.entries(usage).filter(([k,v])=>fields[k]&&v!=null&&typeof v==='number').map(([k,v])=>`${fields[k]} ${v}`);
 return result.length?result.join('，')+' 词元':'用量明细见诊断原始记录';
}
export function errorText(value:unknown):string{
 const text=String(value||'操作失败');
 if(/Failed to fetch|NetworkError|Load failed/i.test(text))return '连接本地程序失败，请检查程序是否仍在运行；若已退出，请重新启动。';
 if(/Unexpected (token|end)|not valid JSON/i.test(text))return '程序返回的数据无法读取，请重试；重复出现时可导出诊断记录。';
 return /[\u4e00-\u9fff]/.test(text)?text:'操作未完成。原始错误：'+text;
}
