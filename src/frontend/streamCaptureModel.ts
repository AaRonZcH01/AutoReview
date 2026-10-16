export type CapturePhase='idle'|'starting'|'ready'|'capturing'|'preparing'|'review'|'submitting'|'uncertain'|'success'|'stopped';
export function captureShortcut(phase:CapturePhase,event:{key:string,repeat?:boolean,isComposing?:boolean,ctrlKey?:boolean,metaKey?:boolean,altKey?:boolean},editable=false){
 if(event.key==='Escape')return 'stop';
 if(event.key!=='Enter'||event.repeat||event.isComposing||editable||event.ctrlKey||event.metaKey||event.altKey)return '';
 return phase==='ready'?'capture':phase==='review'?'confirm':'';
}
export function captureReceiptText(value:Record<string,any>){
 const label=`纸签 ${value.paper_tag} · 第 ${value.slot}/${value.page_count} 页`;
 if(value.state==='saved_local')return `${label} 已保存本地；结束拍摄后在图库点击“开始处理”批量提交。`;
 return value.state==='waiting_dependencies'?`${label} 已保存并加入等待队列，相关页面收齐后开始识别。`:`${label} 已成功入队。`;
}
export function captureErrorText(error:any){
 if(error?.name==='NotAllowedError')return '摄像头权限未开放，请在系统或浏览器中允许摄像头访问，或改用本地图片。';
 if(error?.name==='NotFoundError')return '未检测到摄像头，请连接设备，或使用本地图片代替拍摄。';
 if(error?.name==='NotReadableError')return '摄像头被其他程序占用或无法读取，请关闭占用程序后重试。';
 if(error?.name==='OverconstrainedError')return '所选摄像头已不可用，请刷新设备列表，重新选择或连接设备。';
 if(error?.name==='AbortError')return '摄像头连接中断，请重新连接；已拍照片仍保留。';
 return error?.message||'操作未完成，请查看当前状态后重试。';
}

export function captureCountdownSeconds(session?:Record<string,any>|null){
 const value=session?.capture_countdown_seconds;
 return Number.isInteger(value)&&value>=0&&value<=60?value:3;
}
