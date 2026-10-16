"""One submission review transaction updates judgment, score and undo history."""
from .core import require, uid

FIELDS=('overrides','reviewed','photo_status','history','history_cursor')


def state(service, submission_id, db=None):
    from contextlib import nullcontext
    with service.store.connection() if db is None else nullcontext(db) as db:
        sub=service.store.get(db,'submissions',submission_id)
        service.require_available(db,'tasks',sub['task_id'])
        pages=service.store.list(db,'pages',parent_id=submission_id,include_deleted=False)
        for page in pages:
            version=service.store.get(db,'image_versions',page['image_version_id'])
            page['asset_id']=version['asset_id']
            page['results']=service.results(db,page)
            observation=service.store.get(db,'observations',page['observation_id']) if page.get('observation_id') else {}
            page['photo_issues']=observation.get('photo_issues',[])
            from .photo_issues import describe
            page['photo_details']=describe(page['photo_issues'],page['results'],observation.get('structure',[]))
        score=service.store.get(db,'score_snapshots',sub.get('score_snapshot_id') or '',optional=True)
        return {'submission':sub,'pages':pages,'score_snapshot':score}


def review(service, submission_id, data):
    store=service.store
    with store.connection(write=True) as db:
        sub=store.get(db,'submissions',submission_id)
        service.require_available(db,'tasks',sub['task_id'])
        key='submission-review:'+submission_id+':'+data.get('request_id','')
        require(data.get('request_id'),'REQUEST_ID','缺少复核操作标识。')
        cached=store.once(db,key,data)
        if cached is not None: return cached
        require(sub['revision']==data.get('base_revision'),'REVISION_CONFLICT',
                '整份复核结果已由其他操作更新。本次修改未提交，请读取最新结果后决定是否重试。',409,
                current_revision=sub['revision'],reload_path='/submissions/'+submission_id+'/review-state')
        pages=store.list(db,'pages',parent_id=submission_id,include_deleted=False)
        history=sub.get('review_history',[]);cursor=sub.get('review_cursor',-1)
        action=data.get('action','edit')
        if action in ('undo','redo'):
            index=cursor if action=='undo' else cursor+1
            require(0<=index<len(history),'NO_HISTORY','没有可撤销或重做的操作。')
            record=store.get(db,'submission_reviews',history[index])
            page=store.get(db,'pages',record['page_id'])
            require(page['image_version_id']==record['image_version_id'] and page.get('observation_id')==record['observation_id'],
                    'REVIEW_BASE_CHANGED','图片或机器识别已变化，不能恢复旧判定。',409)
            restored=record['before'] if action=='undo' else record['after']
            result=store.put(db,'pages',{**page,**restored})
            sub=store.put(db,'submissions',{**sub,'review_cursor':cursor+(-1 if action=='undo' else 1)})
            service.update_submission(db,submission_id)
        else:
            page=next((p for p in pages if p['id']==data.get('page_id')),None)
            require(page,'PAGE_SCOPE','请选择当前作业中的页面。')
            before={k:page.get(k) for k in FIELDS}
            result=service.review(page['id'],{**data,'revision':data.get('revision')},db=db)
            record=store.put(db,'submission_reviews',{'parent_id':submission_id,'task_id':sub['task_id'],'group_id':sub['group_id'],
                'page_id':page['id'],'before':before,'after':{k:result.get(k) for k in FIELDS},
                'image_version_id':page['image_version_id'],'observation_id':page.get('observation_id')})
            latest=store.get(db,'submissions',submission_id)
            store.put(db,'submissions',{**latest,'review_history':history[:cursor+1]+[record['id']],'review_cursor':cursor+1})
        latest=store.get(db,'submissions',submission_id)
        if latest.get('paper_feedback')=='done' and action!='confirm':
            latest=store.put(db,'submissions',{**latest,'paper_feedback':'needs_update'})
        value=state(service,submission_id,db)
        value['page']=next(p for p in value['pages'] if p['id']==result['id'])
        return store.remember(db,key,data,value)
