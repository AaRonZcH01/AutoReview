"""Durable review jobs. Commit once per submission, retain exact page history."""
from collections import OrderedDict
from .core import require, Problem, now
from .submission_review import FIELDS


def history(service, task_id):
    with service.store.connection() as db:
        service.require_available(db,'tasks',task_id)
        task=service.store.get(db,'tasks',task_id)
        ids,cursor=task.get('batch_review_history',[]),task.get('batch_review_cursor',-1)
        def label(index):
            if not 0<=index<len(ids):return None
            row=service.store.get(db,'review_batches',ids[index])
            return ('确认' if row['action']=='confirm' else '取消')+f"{len(row['changes'])}页复核"
        return {'can_undo':cursor>=0,'can_redo':cursor+1<len(ids),'undo_label':label(cursor),'redo_label':label(cursor+1)}


def enqueue(service,task_id,data,history_action=False):
    store=service.store
    require(data.get('request_id'),'REQUEST_ID','缺少批量操作标识。')
    key='review-job:'+task_id+':'+data['request_id']
    with store.connection(write=True) as db:
        service.require_available(db,'tasks',task_id)
        task=service.store.get(db,'tasks',task_id)
        old=store.once(db,key,data)
        if old is not None:return store.get(db,'review_batch_jobs',old['id']),False
        require(not any(j['state'] in ('queued','running') for j in store.list(db,'review_batch_jobs',task_id=task_id)),
                'REVIEW_JOB_ACTIVE','本任务正在批量复核，请等待当前操作完成。',409)
        if history_action:
            action=data.get('action')
            require(action in ('undo','redo'),'BATCH_ACTION','撤销或重做操作无效。')
            ids,cursor=task.get('batch_review_history',[]),task.get('batch_review_cursor',-1)
            index=cursor if action=='undo' else cursor+1
            require(0<=index<len(ids),'NO_HISTORY','没有可撤销或重做的批量操作。')
            operation=store.get(db,'review_batches',ids[index])
            entries=[c for c in operation['changes'] if c.get('undone',False)==(action=='redo')]
            targets=[{'id':c['id'],'revision':c['expected_revision']} for c in entries]
        else:
            targets=data.get('pages',[])
            require(data.get('action','confirm') in ('confirm','unconfirm'),'BATCH_ACTION','批量复核操作无效。')
            require(targets and len(targets)<=500 and len({p['id'] for p in targets})==len(targets),'BATCH_SCOPE','请选择1至500张不重复页面。')
            require(data.get('confirm'),'CONFIRM_REQUIRED','请确认本次批量复核范围。')
            operation=store.put(db,'review_batches',{'parent_id':task_id,'task_id':task_id,'action':data.get('action','confirm'),'changes':[]})
        row=store.put(db,'review_batch_jobs',{'parent_id':task_id,'task_id':task_id,'state':'queued','stage':'queued',
            'request':data,'history_action':history_action,'batch_id':operation['id'],'targets':targets,
            'completed':0,'total':len(targets),'current':None,
            'result':{'action':data.get('action','confirm'),'accepted':[],'rejected':[],'preview':False}})
        store.remember(db,key,data,{'id':row['id']})
        return row,True


def status(service,task_id,identifier):
    with service.store.connection() as db:
        service.require_available(db,'tasks',task_id)
        job=service.store.get(db,'review_batch_jobs',identifier)
        require(job['task_id']==task_id,'PAGE_SCOPE','操作不属于当前任务。')
    return {**job,**getattr(service,'_review_progress',{}).get(identifier,{})}


def _apply(service,db,sub,page,action,accept_unknown=False):
    store=service.store
    before={k:page.get(k) for k in FIELDS}
    result=service.review(page['id'],{'revision':page['revision'],'action':action,'accept_unknown':accept_unknown},db=db,defer_update=True)
    record=store.put(db,'submission_reviews',{'parent_id':sub['id'],'task_id':sub['task_id'],'group_id':sub['group_id'],
        'page_id':page['id'],'before':before,'after':{k:result.get(k) for k in FIELDS},
        'image_version_id':page['image_version_id'],'observation_id':page.get('observation_id')})
    latest=store.get(db,'submissions',sub['id']);cursor=latest.get('review_cursor',-1)
    store.put(db,'submissions',{**latest,'review_history':latest.get('review_history',[])[:cursor+1]+[record['id']],'review_cursor':cursor+1})
    return result


def _advance_history_revisions(store,db,task,job,changes):
    # Undo/redo itself increments revisions. Carry this known, safe transition
    # to the next stack entry; an unrelated detail edit never gets this update.
    ids=task.get('batch_review_history',[])
    index=ids.index(job['batch_id'])
    undoing=job['request']['action']=='undo'
    candidates=reversed(ids[:index]) if undoing else iter(ids[index+1:])
    pending={c['id']:c for c in changes if c.get('undone',False)==undoing}
    for identifier in candidates:
        other=store.get(db,'review_batches',identifier);updated=False
        for prior in other['changes']:
            latest=pending.pop(prior['id'],None)
            if latest is None:continue
            current=latest['before_reviewed'] if undoing else latest['after_reviewed']
            expected=prior['after_reviewed'] if undoing else prior['before_reviewed']
            if current==expected and prior['image_version_id']==latest['image_version_id'] and prior['observation_id']==latest['observation_id']:
                prior['expected_revision']=latest['expected_revision'];updated=True
        if updated:store.put(db,'review_batches',other)
        if not pending:break


def process(service,identifier):
    from .batch_review import reasons
    store=service.store
    if not hasattr(service,'_review_progress'):service._review_progress={}
    with store.connection(write=True) as db:
        job=store.get(db,'review_batch_jobs',identifier)
        if job['state'] not in ('queued','running'):return job
        job=store.put(db,'review_batch_jobs',{**job,'state':'running','stage':'checking','started_at':now()})
        groups=OrderedDict()
        for target in job['targets']:
            page=store.get(db,'pages',target['id'],optional=True)
            groups.setdefault(page['parent_id'] if page and page.get('task_id')==job['task_id'] else None,[]).append(target)
    try:
        for sub_id,targets in groups.items():
            # One complete submission is atomic: a crash cannot leave its score
            # or reviewed flag inconsistent with the committed page flags.
            with store.connection(write=True) as db:
                job=store.get(db,'review_batch_jobs',identifier)
                service.require_available(db,'tasks',job['task_id'])
                task=store.get(db,'tasks',job['task_id'])
                operation=store.get(db,'review_batches',job['batch_id']);changes=operation['changes']
                sub=store.get(db,'submissions',sub_id) if sub_id else None
                pages=store.list(db,'pages',parent_id=sub_id,include_deleted=False) if sub else []
                page_map={p['id']:p for p in pages};changed=False
                for target in targets:
                    page=page_map.get(target['id'])
                    row={'id':target['id'],'slot':page['slot'] if page else None,'paper_tag':sub['paper_tag'] if sub else None}
                    service._review_progress[identifier]={'stage':'saving','current':row}
                    issues=[];change=next((c for c in changes if c['id']==target['id']),None)
                    if not page:issues=['页面不存在或不属于本任务']
                    elif page['revision']!=target['revision']:issues=['页面已发生变化，本次未覆盖；请打开该页核查后重试']
                    elif job['history_action']:
                        if page['image_version_id']!=change['image_version_id'] or page.get('observation_id')!=change['observation_id']:
                            issues=['图片或机器识别已变化，不能恢复旧复核状态']
                    else:issues=reasons(service,db,page,operation['action'])
                    if issues:
                        job['result']['rejected'].append({**row,'reasons':issues});continue
                    if job['history_action']:
                        undoing=job['request']['action']=='undo'
                        desired=change['before_reviewed'] if undoing else change['after_reviewed']
                        action='confirm' if desired else 'unconfirm'
                    else:action=operation['action']
                    result=_apply(service,db,sub,page,action,accept_unknown=job['history_action'])
                    if job['history_action']:change.update(expected_revision=result['revision'],undone=undoing)
                    else:changes.append({'id':page['id'],'before_reviewed':bool(page.get('reviewed')),'after_reviewed':bool(result['reviewed']),
                        'expected_revision':result['revision'],'image_version_id':page['image_version_id'],'observation_id':page.get('observation_id'),'undone':False})
                    changed=True;job['result']['accepted'].append(row)
                if changed:
                    service.update_submission(db,sub_id)
                    store.put(db,'review_batches',{**operation,'changes':changes})
                    if job['history_action']:
                        succeeded={r['id'] for r in job['result']['accepted']}
                        _advance_history_revisions(store,db,task,job,[c for c in changes if c['id'] in succeeded])
                    if not job['history_action'] and operation['id'] not in task.get('batch_review_history',[]):
                        cursor=task.get('batch_review_cursor',-1)
                        task=store.put(db,'tasks',{**task,'batch_review_history':task.get('batch_review_history',[])[:cursor+1]+[operation['id']],
                            'batch_review_cursor':cursor+1})
                job=store.put(db,'review_batch_jobs',{**job,'completed':job['completed']+len(targets),'current':row,'stage':'checking'})
            service._review_progress[identifier]={'stage':'checking','current':row}
        with store.connection(write=True) as db:
            job=store.get(db,'review_batch_jobs',identifier)
            if job['history_action']:
                task=store.get(db,'tasks',job['task_id'])
                store.put(db,'tasks',{**task,'batch_review_cursor':task.get('batch_review_cursor',-1)+(-1 if job['request']['action']=='undo' else 1)})
            return store.put(db,'review_batch_jobs',{**job,'state':'completed','stage':'completed','current':None,'finished_at':now()})
    except Exception as error:
        if not isinstance(error,Problem):service.diagnostics.record('REVIEW_JOB_FAILED',str(error),component='review',job_id=identifier)
        with store.connection(write=True) as db:
            job=store.get(db,'review_batch_jobs',identifier)
            return store.put(db,'review_batch_jobs',{**job,'state':'failed','stage':'failed',
                'error_code':error.code if isinstance(error,Problem) else 'REVIEW_JOB_FAILED',
                'error_message':error.message if isinstance(error,Problem) else '批量复核已停止；已完成页面保留，请核查剩余页面。','finished_at':now()})
    finally:service._review_progress.pop(identifier,None)


def recover(service):
    with service.store.connection(write=True) as db:
        for job in service.store.list(db,'review_batch_jobs'):
            if job['state'] in ('queued','running'):
                service.store.put(db,'review_batch_jobs',{**job,'state':'interrupted','stage':'interrupted','current':None,
                    'error_message':'上次批量复核已中断；已完成页面保留，未自动继续。'})
