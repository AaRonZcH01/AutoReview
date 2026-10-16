import time
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient
from autoreview.v2.core import Problem, uid
from autoreview.v2.api import create_app
from autoreview.v2.scoring import manage, check, gate
from autoreview.v2.image_jobs import enqueue, process, recover
from test_workflow import service, setup, intake, FakeProvider


def request(service, task, action, **extra):
    draft=manage(service,task['id'])['draft']
    return manage(service,task['id'],{'action':action,'revision':draft['revision'],**extra})


def assigned(service,task):
    draft=manage(service,task['id'])['draft']
    return request(service,task,'save',units=[{**u,'max_points_minor':200} for u in draft['units']],declared_total_minor=400)


def publish(service,task):
    draft=manage(service,task['id'])['draft']
    data={'action':'publish','revision':draft['revision'],'scope':'future'}
    preview=manage(service,task['id'],data)
    return manage(service,task['id'],{**data,'preview':False,'fingerprint':preview['fingerprint']})


def test_live_check_is_read_only_and_receipts_can_be_revoked(service):
    task,_,_=setup(service,2)
    assigned(service,task)
    one=request(service,task,'confirm',page_slot=1)
    assert one['pending_question_ids']==['q2']
    with pytest.raises(Problem,match='还有|尚有'):
        request(service,task,'confirm',total_only=True)
    request(service,task,'confirm',page_slot=2)
    confirmed=request(service,task,'confirm',total_only=True)
    assert confirmed['total_reviewed'] and not confirmed['pending_question_ids']
    draft=deepcopy(confirmed['draft']);draft['units'][0]['max_points_minor']=300
    inspected=check(service,task['id'],draft)
    assert inspected['pending_question_ids']==['q1'] and not inspected['total_reviewed']
    assert manage(service,task['id'])['draft']==confirmed['draft']
    revoked=request(service,task,'revoke',page_slot=1)
    assert revoked['pending_question_ids']==['q1'] and not revoked['total_reviewed']
    assert revoked['draft']['units'][1]['confirmed_hash']
    request(service,task,'confirm',page_slot=1)
    request(service,task,'confirm',total_only=True)
    revoked=request(service,task,'revoke',total_only=True)
    assert not revoked['pending_question_ids'] and not revoked['total_reviewed']


def test_missing_total_reports_field_and_unpublish_keeps_frozen_history(service):
    task,_,ref=setup(service,2)
    request(service,task,'toggle',enabled=True)
    with pytest.raises(Problem,match='试卷总分') as error:
        request(service,task,'confirm',total_only=True)
    assert any(i.get('field')=='declared_total_minor' for i in error.value.details['issues'])
    assigned(service,task)
    request(service,task,'confirm',page_slot=1)
    with pytest.raises(Problem,match='所有题目分值'):
        publish(service,task)
    request(service,task,'confirm',page_slot=2)
    request(service,task,'confirm',total_only=True)
    publish(service,task)
    with service.store.connection() as db:
        current=service.store.get(db,'tasks',task['id'])
        old=service.store.get(db,'scoring_versions',current['scoring_version_id'])
    request(service,task,'unpublish',confirm=True)
    assert not manage(service,task['id'])['ready_to_confirm']
    with service.store.connection() as db:
        assert service.store.get(db,'scoring_versions',old['id'])==old
        with pytest.raises(Problem): gate(service.store,db,task['id'])
    with pytest.raises(Problem): service.confirm_reference(task['id'],service.draft(task['id'])['revision'])


def test_noop_reference_confirmation_reuses_snapshot_but_edit_creates_version(service):
    task,_,ref=setup(service,2)
    draft=service.draft(task['id'])
    saved=service.draft(task['id'],{'revision':draft['revision'],'items':draft['items']})
    assert service.confirm_reference(task['id'],saved['revision'])['id']==ref['id']
    saved=service.draft(task['id'],{'revision':service.draft(task['id'])['revision'],'items':[{**a,'english':'pear'} if a['id']=='q1' else a for a in draft['items']]})
    assert service.confirm_reference(task['id'],saved['revision'])['id']!=ref['id']
    with service.store.connection() as db: assert len(service.store.list(db,'reference_versions',task_id=task['id']))==2


def test_batch_rotation_all_targets_retry_once_undo_and_restart(service):
    task,groups,_=setup(service,2);intake(service,groups[0],4)
    pages=service.detail(task['id'])['pages'];before={p['id']:p['image_version_id'] for p in pages};before_assets={p['id']:p['asset_id'] for p in pages}
    data={'request_id':uid(),'angle':90,'pages':[{'id':p['id'],'revision':p['revision']} for p in pages[:3]]}
    job,created=enqueue(service,data);assert created
    assert enqueue(service,data)==(job,False)
    done=process(service,job['id']);assert done['state']=='completed' and done['completed_count']==3
    after=service.detail(task['id'])['pages'];assert sum(p['image_version_id']!=before[p['id']] for p in after)==3
    assert service.batch_images(data)['id']==done['result']['id']
    with service.store.connection(write=True) as db:
        service.store.put(db,'local_image_jobs',{**done,'state':'running'})
    recover(service)
    with service.store.connection() as db: assert service.store.get(db,'local_image_jobs',job['id'])['state']=='completed'
    undo,_=enqueue(service,{'request_id':uid(),'undo_id':done['result']['id']})
    assert process(service,undo['id'])['state']=='completed'
    assert all(p['asset_id']==before_assets[p['id']] for p in service.detail(task['id'])['pages'])


def test_failed_render_does_not_switch_any_page_or_leave_job_locked(service,monkeypatch):
    task,groups,_=setup(service,2);intake(service,groups[0],2)
    pages=service.detail(task['id'])['pages'];calls=0;original=service.prepare_asset
    def fail(*args):
        nonlocal calls
        calls+=1
        if calls==2: raise Problem('TEST_RENDER','测试生成失败')
        return original(*args)
    monkeypatch.setattr(service,'prepare_asset',fail)
    job,_=enqueue(service,{'request_id':uid(),'angle':90,'pages':[{'id':p['id'],'revision':p['revision']} for p in pages]})
    assert process(service,job['id'])['state']=='failed'
    assert [p['image_version_id'] for p in service.detail(task['id'])['pages']]==[p['image_version_id'] for p in pages]
    assert enqueue(service,{'request_id':uid(),'angle':90,'pages':[{'id':p['id'],'revision':p['revision']} for p in pages]})[1]


def test_preparation_cannot_change_revisions_during_rotation(service):
    task,groups,_=setup(service,2);intake(service,groups[0],2)
    pages=service.detail(task['id'])['pages']
    job,_=enqueue(service,{'request_id':uid(),'angle':90,'pages':[{'id':p['id'],'revision':p['revision']} for p in pages]})
    with pytest.raises(Problem,match='旋转正在进行'):
        service.prepare_page(pages[0]['id'],{})
    assert process(service,job['id'])['state']=='completed'
    assert service.prepare_page(pages[0]['id'],{})['preparation']


def test_bulk_commit_conflict_rolls_back_all_targets(service,monkeypatch):
    task,groups,_=setup(service,2);intake(service,groups[0],2)
    pages=service.detail(task['id'])['pages'];original=service.prepare_asset;calls=0
    def conflicting(*args):
        nonlocal calls
        asset=original(*args);calls+=1
        if calls==2:
            with service.store.connection(write=True) as db:
                last=service.store.get(db,'pages',pages[-1]['id'])
                service.store.put(db,'pages',{**last,'photo_status':'pending'})
        return asset
    monkeypatch.setattr(service,'prepare_asset',conflicting)
    job,_=enqueue(service,{'request_id':uid(),'angle':90,'pages':[{'id':p['id'],'revision':p['revision']} for p in pages]})
    assert process(service,job['id'])['state']=='failed'
    assert [p['image_version_id'] for p in service.detail(task['id'])['pages']]==[p['image_version_id'] for p in pages]


def test_permanent_group_delete_removes_only_its_annotation_cache(service):
    task,groups,_=setup(service,2)
    for group in groups: intake(service,group,2)
    pages=service.detail(task['id'])['pages'];paths={}
    for page in pages:
        path=service.store.root/'cache'/'annotated'/page['id']/'cached.png'
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'synthetic cached image')
        paths[page['id']]=path
    entry=service.trash.move('groups',groups[0]['id']);service.trash.purge(entry['id'])
    assert all(path.exists()==(next(p for p in pages if p['id']==identifier)['group_id']==groups[1]['id']) for identifier,path in paths.items())


def test_slow_image_job_keeps_http_responsive_and_images_cache_by_revision(service,monkeypatch):
    service.close()
    app=create_app(service.settings,FakeProvider(),nonce='test-secret',start_worker=False)
    local=app.state.service;task,groups,_=setup(local,2);intake(local,groups[0],2)
    pages=local.detail(task['id'])['pages'];original=local.prepare_asset
    def slow(*args):time.sleep(.7);return original(*args)
    monkeypatch.setattr(local,'prepare_asset',slow)
    with TestClient(app) as client:
        client.headers['X-AutoReview-Session']='test-secret'
        data={'request_id':uid(),'angle':90,'pages':[{'id':p['id'],'revision':p['revision']} for p in pages]}
        started=time.monotonic();job=client.post('/api/images/jobs',json=data).json()
        assert time.monotonic()-started<.6
        t=time.monotonic();assert client.get('/api/state').status_code==200;assert time.monotonic()-t<.6
        deadline=time.monotonic()+8
        while job['state'] in ('queued','running') and time.monotonic()<deadline:
            time.sleep(.1);job=client.get('/api/images/jobs/'+job['id']).json()
        assert job['state']=='completed' and job['completed_count']==2
        p=local.detail(task['id'])['pages'][0]
        assert 'immutable' in client.get('/api/assets/'+p['asset_id']+'/display').headers['cache-control']
        first=client.get(f"/api/pages/{p['id']}/annotated?r={p['revision']}&thumbnail=true")
        assert first.status_code==200 and 'immutable' in first.headers['cache-control']
        from autoreview.v2 import api
        monkeypatch.setattr(api,'annotate',lambda *args:pytest.fail('cached revision must not render again'))
        assert client.get(f"/api/pages/{p['id']}/annotated?r={p['revision']}&thumbnail=true").content==first.content
        assert client.get(f"/api/pages/{p['id']}/annotated?r={p['revision']-1}").status_code==409
        assert client.get('/api/state').headers['cache-control']=='no-store'
