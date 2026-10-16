"""Explicit, revision-bound page confirmations; never resolve unknown answers."""
from .core import require, Problem
from .review_readiness import needs_review,pending_summary


def reasons(service, db, page, action):
    if page['state']!='processed' or not page.get('observation_id'):return ['尚未处理完成或缺少机器观察']
    if action=='unconfirm':return [] if page.get('reviewed') else ['本页尚未复核，无需取消']
    if page.get('reviewed'):return ['本页已复核，无需重复确认']
    issues=[]
    sub=service.store.get(db,'submissions',page['parent_id'])
    pages=service.store.list(db,'pages',parent_id=sub['id'],include_deleted=False)
    if len(pages)!=sub['page_count']:issues.append('整份作业缺页，请先补齐')
    if page.get('photo_status') in ('pending','retake_required'):issues.append('照片尚待确认或需要重拍')
    rows=service.results(db,page)
    if not rows:issues.append('本页没有可复核题目')
    pending=[r for r in rows if needs_review(r)]
    if pending:issues.append(f'还有{len(pending)}题待核实（{pending_summary(pending)}），批量操作不会自动改判')
    return issues



def batch(service, task_id, data):
    if not data.get('preview'):
        from .review_jobs import enqueue, process
        job, created = enqueue(service, task_id, data)
        if created: job = process(service, job['id'])
        return job['result']
    targets=data.get('pages',[]);action=data.get('action','confirm')
    require(action in ('confirm','unconfirm'),'BATCH_ACTION','批量复核操作无效。')
    require(targets and len(targets)<=500 and len({p['id'] for p in targets})==len(targets),'BATCH_SCOPE','请选择1至500张不重复页面。')
    require(data.get('request_id'),'REQUEST_ID','缺少批量操作标识。')
    accepted=[];rejected=[];store=service.store
    with store.connection() as db:
        service.require_available(db,'tasks',task_id)
        for target in targets:
            page=store.get(db,'pages',target['id'],optional=True)
            if not page or page.get('task_id')!=task_id:
                rejected.append({'id':target['id'],'reasons':['页面不存在或不属于本任务']});continue
            sub=store.get(db,'submissions',page['parent_id'])
            row={'id':page['id'],'slot':page['slot'],'paper_tag':sub['paper_tag']}
            issues=reasons(service,db,page,action)
            if page['revision']!=target.get('revision'):issues.insert(0,'页面已发生变化，请读取最新状态后重新选择')
            (rejected if issues else accepted).append({**row,**({'reasons':issues} if issues else {})})
    return {'action':action,'accepted':accepted,'rejected':rejected,'preview':True}
