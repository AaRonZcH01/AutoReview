"""v1.2.1 local review transactions, typed statistics and lifecycle regressions."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import os
import threading
import time
from types import SimpleNamespace
import pytest
import httpx
from autoreview.v2.core import uid,MachineSettings,Problem
from autoreview.v2.api import create_app
from autoreview.v2.batch_review import batch
from autoreview.v2 import review_jobs,lifetime
from autoreview.v2.submission_review import state
from autoreview.v2.reports import statistics
from autoreview.v2.statistics_detail import expected,wrong_answers
from test_workflow import service
from test_v11_review import processed,change
from test_v12 import plan


def execute(service,task,action='confirm'):
    data={**plan(service,task,action),'confirm':True}
    job,created=review_jobs.enqueue(service,task['id'],data)
    assert created
    result=review_jobs.process(service,job['id'])
    assert result['state']=='completed',result
    return result,data


def travel(service,task,action):
    job,_=review_jobs.enqueue(service,task['id'],{'action':action,'request_id':uid()},True)
    result=review_jobs.process(service,job['id'])
    assert result['state']=='completed',result
    return result


def test_bulk_compact_single_recompute_per_submission_and_idempotency(service,monkeypatch):
    task,sub=processed(service,True);calls=[];original=service.update_submission
    monkeypatch.setattr(service,'update_submission',lambda *a:(calls.append(a[1]),original(*a))[1])
    result,data=execute(service,task)
    assert calls==[sub] and result['completed']==result['total']==2
    assert len(result['result']['accepted'])==2 and not result['result']['rejected']
    replay,created=review_jobs.enqueue(service,task['id'],data)
    assert not created and replay==result
    with service.store.connection() as db:
        assert len(service.store.list(db,'submission_reviews',parent_id=sub))==2
        assert len(service.store.list(db,'review_revisions',task_id=task['id']))==2
    assert state(service,sub)['submission']['state']=='reviewed'


def test_bulk_multiple_undo_redo_preserves_teacher_answers(service):
    task,sub=processed(service,True)
    change(service,sub,question_id='q1',change={'status':'incorrect'})
    original=deepcopy(state(service,sub)['pages'][0]['results'])
    execute(service,task);execute(service,task,'unconfirm')
    for action,reviewed in [('undo',True),('undo',False),('redo',True),('redo',False)]:
        result=travel(service,task,action)
        assert len(result['result']['accepted'])==2 and not result['result']['rejected']
        assert all(bool(p['reviewed'])==reviewed for p in state(service,sub)['pages'])
        assert state(service,sub)['pages'][0]['results']==original
    assert review_jobs.history(service,task['id'])['can_undo']
    assert not review_jobs.history(service,task['id'])['can_redo']


def test_bulk_history_rejects_changed_page_without_overwriting(service):
    task,sub=processed(service)
    execute(service,task)
    change(service,sub,question_id='q1',change={'status':'incorrect'})
    before=state(service,sub)['pages'][0]
    result=travel(service,task,'undo')['result']
    assert len(result['accepted'])==1 and len(result['rejected'])==1
    after=state(service,sub)['pages'][0]
    assert before==after
    result=travel(service,task,'redo')['result']
    assert len(result['accepted'])==1 and not result['rejected']
    assert state(service,sub)['pages'][0]==before


def test_bulk_job_exposes_current_page_without_uncommitted_completion(service,monkeypatch):
    task,sub=processed(service);started=threading.Event();release=threading.Event();original=review_jobs._apply
    def hold(*a,**kw):
        started.set();release.wait(3)
        return original(*a,**kw)
    monkeypatch.setattr(review_jobs,'_apply',hold)
    job,_=review_jobs.enqueue(service,task['id'],{**plan(service,task),'confirm':True})
    with pytest.raises(Problem) as error:review_jobs.enqueue(service,task['id'],{**plan(service,task),'confirm':True})
    assert error.value.code=='REVIEW_JOB_ACTIVE'
    with ThreadPoolExecutor(1) as pool:
        future=pool.submit(review_jobs.process,service,job['id'])
        try:
            assert started.wait(1)
            current=review_jobs.status(service,task['id'],job['id'])
            assert current['state']=='running' and current['current']['paper_tag']=='001'
            assert current['current']['slot']==1 and current['completed']==0
        finally:release.set()
        assert future.result()['completed']==2


def test_bulk_restart_receipt_keeps_committed_history(service):
    task,sub=processed(service);job,_=review_jobs.enqueue(service,task['id'],{**plan(service,task),'confirm':True})
    review_jobs.recover(service)
    result=review_jobs.status(service,task['id'],job['id'])
    assert result['state']=='interrupted' and result['completed']==0
    assert not any(p['reviewed'] for p in state(service,sub)['pages'])


def test_job_http_routes_progress_and_history(tmp_path):
    settings=MachineSettings(tmp_path/'m');settings.save(library_location=str(tmp_path/'l'))
    app=create_app(settings,nonce='test',start_worker=False);svc=app.state.service
    task,sub=processed(svc)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver',headers={'X-AutoReview-Session':'test'}) as client:
            response=await client.post('/api/tasks/'+task['id']+'/batch-review-jobs',json={**plan(svc,task),'confirm':True})
            assert response.status_code==200,response.text
            identifier=response.json()['id']
            for _ in range(100):
                value=(await client.get('/api/tasks/'+task['id']+'/batch-review-jobs/'+identifier)).json()
                if value['state'] not in ('queued','running'):break
                await asyncio.sleep(.01)
            assert value['state']=='completed' and value['completed']==2
            history=(await client.get('/api/tasks/'+task['id']+'/batch-review-history')).json()
            assert history['can_undo'] and history['undo_label']=='确认2页复核'
    try:asyncio.run(run())
    finally:svc.close()


def test_merge_keeps_review_history_and_stops_unfinished_jobs(service,tmp_path):
    from autoreview.v2.service import Service
    from autoreview.v2.libraries import merge_library
    task,sub=processed(service);execute(service,task)
    review_jobs.enqueue(service,task['id'],{**plan(service,task,'unconfirm'),'confirm':True})
    settings=MachineSettings(tmp_path/'target-machine');settings.save(library_location=str(tmp_path/'target-library'))
    other=Service(settings)
    try:
        merge_library(other,service.store.root)
        with other.store.connection() as db:
            imported=other.store.list(db,'tasks')[0]
            assert len(other.store.list(db,'review_batches'))==2
            jobs=other.store.list(db,'review_batch_jobs')
            assert {j['state'] for j in jobs}=={'completed','interrupted'}
        assert review_jobs.history(other,imported['id'])['can_undo']
        value=travel(other,imported,'undo')
        assert len(value['result']['accepted'])==2
    finally:other.close()


def test_purge_group_removes_shared_batch_receipts_and_stack(service):
    task,sub=processed(service);execute(service,task)
    with service.store.connection() as db:group=service.store.get(db,'submissions',sub)['group_id']
    entry=service.trash.move('groups',group);service.trash.purge(entry['id'])
    with service.store.connection() as db:
        assert not service.store.list(db,'review_batches') and not service.store.list(db,'review_batch_jobs')
    assert not review_jobs.history(service,task['id'])['can_undo']


def test_statistics_typed_errors_locator_and_version_labels(service):
    task,sub=processed(service)
    change(service,sub,question_id='q1',change={'status':'incorrect','raw_english':'aple'})
    execute(service,task)
    report=statistics(service,task['id']);q=next(q for q in report['questions'] if q['question_id']=='q1')
    assert q['label']=='第1页 · 未分组 · 第1题' and q['expected_display']=='apple'
    assert q['wrong_answers']==[{'text':'aple','count':1}]
    assert q['locator']['asset_id'] and q['locator']['box']['x1']<.1
    assert q['reference_version_name']=='答案 v1'
    assert q['known']==1 and q['errors']==1


def test_ordering_display_uses_structured_rank_even_when_raw_blank():
    a={'kind':'ordering','response_encoding':'rank','english':'','answer_spec':{'rank':'3'},
       'status':'incorrect','raw_english':'','observation':{'rank':'2'}}
    assert expected(a)=='3' and wrong_answers(a)==['填为序号 2（应为 3）']
    a['answer_spec']={'sequence':['B','A','C']}
    assert expected(a)=='B → A → C'


def test_matching_wrong_details_omit_correct_teacher_pairs():
    a={'id':'q','kind':'matching','status':'incorrect','matching_rule':{'source_group':'left','directed':False,
       'nodes':[{'id':'L1','group':'left','label':'1'},{'id':'L2','group':'left','label':'2'},
                {'id':'A','group':'right','label':'A'},{'id':'B','group':'right','label':'B'}],
       'allowed_edges':[['L1','A'],['L2','B']]},
       'observation':{'edges':[{'from_node':'L1','to_node':'B'},{'from_node':'L2','to_node':'A'}]},
       'score_unit_status':{'u1':'correct','u2':'incorrect'}}
    policy={'units':[{'id':'u1','question_id':'q','source_id':'L1'},{'id':'u2','question_id':'q','source_id':'L2'}]}
    assert expected(a)=='1 → A；2 → B'
    assert wrong_answers(a,policy)==['端点2 → A']


def test_heartbeat_frame_rejects_partial_or_malformed_payload():
    frame=lifetime.pulse_frame('running',1.25)
    assert lifetime.decode_pulse_frame(frame)=='running:1.25'
    for malformed in (b'\0'*128,frame[:24],bytes([frame[0]^1])+frame[1:]):
        with pytest.raises(OSError):lifetime.decode_pulse_frame(malformed)


def test_heartbeat_retries_transport_error_and_preserves_cancellation(monkeypatch):
    calls=[];records=[];original_sleep=asyncio.sleep
    def beat(*args):
        calls.append(1)
        if len(calls)==1:raise OSError('simulated Windows sharing failure')
    async def fast_sleep(*args):
        if len(calls)>=3:raise asyncio.CancelledError
        await original_sleep(0)
    monkeypatch.setattr(lifetime,'pulse',beat)
    monkeypatch.setattr(lifetime.asyncio,'sleep',fast_sleep)
    monkeypatch.setattr(lifetime,'parent_alive',lambda _:True)
    monkeypatch.setattr(lifetime,'record_watch_error',lambda *a,**k:None,raising=False)
    lease=lifetime.UiLease(record=lambda *a,**k:records.append(a))
    async def run():
        with pytest.raises(asyncio.CancelledError):await lifetime.watch(SimpleNamespace(state=SimpleNamespace()),lease,manage_lease=False)
    asyncio.run(run());assert len(calls)==3


@pytest.mark.skipif(os.name!='nt',reason='Windows shared-memory heartbeat')
def test_heartbeat_bypasses_saturated_default_executor(tmp_path,monkeypatch):
    monkeypatch.setenv('AUTOREVIEW_WATCHDOG_FILE',str(tmp_path/'pulse.ready'))
    release=threading.Event()
    async def run():
        loop=asyncio.get_running_loop();pool=ThreadPoolExecutor(1);loop.set_default_executor(pool)
        blocked=loop.run_in_executor(None,release.wait)
        job=asyncio.create_task(lifetime.watch(SimpleNamespace(state=SimpleNamespace()),lifetime.UiLease(),manage_lease=False))
        try:
            await asyncio.sleep(1.2)
            assert lifetime.read_pulse(tmp_path/'pulse.ready').startswith('running:')
        finally:
            release.set();job.cancel();await asyncio.gather(job,blocked,return_exceptions=True)
    asyncio.run(run())


def test_exit_scheduled_even_when_pulse_and_cleanup_fail(tmp_path,monkeypatch):
    settings=MachineSettings(tmp_path/'m');settings.save(library_location=str(tmp_path/'l'))
    app=create_app(settings,nonce='test',start_worker=False);stopped=[]
    app.state.shutdown=lambda:stopped.append(True)
    monkeypatch.setattr(lifetime,'pulse',lambda *a:(_ for _ in ()).throw(OSError('simulated')))
    @contextmanager
    def broken(*a,**kw):raise OSError('disk busy');yield
    monkeypatch.setattr(app.state.service.store,'connection',broken)
    async def run():
        assert (await app.state.request_shutdown())['ok']
        await asyncio.sleep(.3)
        assert stopped and app.state.shutting_down
    try:asyncio.run(run())
    finally:app.state.service.close()
