"""Durable receipts for local image work; request retries never rotate twice."""
from .core import require, Problem, now


def enqueue(service, data):
    store=service.store
    require(data.get('request_id'),'REQUEST_ID','图片操作缺少请求标识。')
    with store.connection(write=True) as db:
        key='local-images:'+data['request_id']
        cached=store.once(db,key,data)
        if cached is not None: return store.get(db,'local_image_jobs',cached['id']),False
        if data.get('undo_id'):
            group=store.get(db,'image_operation_groups',data['undo_id'])
            entries=[{'id':c['page_id']} for c in group['changes']]
        else: entries=data.get('pages',[])
        require(entries and len({e['id'] for e in entries})==len(entries),'PAGE_SCOPE','请选择不重复的图片。')
        pages=[store.get(db,'pages',e['id']) for e in entries]
        if not data.get('undo_id'):
            require(all(p['revision']==e.get('revision') for p,e in zip(pages,entries)),
                    'REVISION_CONFLICT','图片已变化，请刷新范围。',409)
        require(len({p['task_id'] for p in pages})==1,'PAGE_SCOPE','请选择同一任务的图片。')
        task_id=pages[0]['task_id'];service.require_available(db,'tasks',task_id)
        require(not any(j['state'] in ('queued','running') for j in store.list(db,'local_image_jobs',task_id=task_id)),'IMAGE_JOB_ACTIVE','本任务已有图片操作，请等待完成。',409)
        require(data.get('action') in (None,'prepare'),'IMAGE_OPERATION','不支持的图片任务。')
        row=store.put(db,'local_image_jobs',{'task_id':task_id,'state':'queued','stage':'queued','request':data,
            'completed_count':0,'total_count':len(pages),'result':None})
        store.remember(db,key,data,{'id':row['id']})
        return row,True


def process(service, identifier):
    store=service.store
    def update(**fields):
        with store.connection(write=True) as db:
            old=store.get(db,'local_image_jobs',identifier)
            return store.put(db,'local_image_jobs',{**old,**fields})
    job=update(state='running',stage='reading',started_at=now())
    try:
        progress=lambda done,total:update(completed_count=done,total_count=total)
        if job['request'].get('action')=='prepare':
            result=service.batch_prepare_images(job['request'],progress,lambda **fields:update(**fields),job_id=identifier)
        else:
            result=service.batch_images(job['request'],progress,lambda **fields:update(**fields))
        return update(state='completed',stage='completed',result=result,completed_count=job['total_count'],finished_at=now())
    except Problem as error:
        return update(state='failed',stage='failed',error_code=error.code,error_message=error.message,finished_at=now())
    except Exception as error:
        service.diagnostics.record('IMAGE_JOB_FAILED',str(error),component='images',job_id=identifier)
        message='定位未完成，请查看诊断；已生成的定位预览保留。' if job['request'].get('action')=='prepare' else '图片操作未完成，请查看诊断后重试；未切换部分图片。'
        return update(state='failed',stage='failed',error_code='IMAGE_JOB_FAILED',error_message=message,finished_at=now())


def recover(service):
    with service.store.connection(write=True) as db:
        for job in service.store.list(db,'local_image_jobs'):
            if job['state'] in ('queued','running'):
                cached=service.store.once(db,'image-batch:'+job['request']['request_id'],job['request']) if job['request'].get('action')!='prepare' else None
                service.store.put(db,'local_image_jobs',{**job,'state':'completed' if cached else 'interrupted',
                    'stage':'completed' if cached else 'interrupted','result':cached,
                    **({'completed_count':job['total_count'],'finished_at':now(),'current_page_id':None,'current_label':None} if cached else {}),
                    'error_message':None if cached else '上次图片操作因程序中断而停止；已生成图或定位预览保留，未自动重发。'})
