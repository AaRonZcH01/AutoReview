"""Session read caching, safe batch review and restart policy (offline)."""
from copy import deepcopy
import asyncio, threading, time
import httpx
import pytest
from autoreview.v2.core import uid,Problem,MachineSettings
from autoreview.v2.api import create_app
from autoreview.v2.read_cache import detail,statistics,ReadCache
from autoreview.v2.batch_review import batch
from autoreview.v2.submission_review import state
from autoreview.v2.contracts import prompt_record
from autoreview.v2.contracts_v11 import record as frozen_record
from autoreview.v2.contracts_v5 import decode_output
from test_workflow import service,setup
from test_v11_review import processed,change

def test_detail_hit_skips_reconstruction_and_invalidates_writes(service,monkeypatch):
    task,_,_=setup(service);calls=[];original=service.detail
    monkeypatch.setattr(service,'detail',lambda *args:(calls.append(1),original(*args))[1])
    first=detail(service,task['id']);assert len(calls)==1
    assert detail(service,task['id'])==first and len(calls)==1
    assert detail(service,task['id'],first['cache_token'])['detail_unchanged']
    with service.store.connection(write=True) as db:
        row=service.store.get(db,'tasks',task['id']);service.store.put(db,'tasks',{**row,'name':'changed'})
    next=detail(service,task['id'],first['cache_token']);assert next['task']['name']=='changed' and len(calls)==2

def test_statistics_cached_and_review_invalidates(service,monkeypatch):
    import autoreview.v2.reports as reports
    task,sub=processed(service);calls=[];original=reports.statistics
    monkeypatch.setattr(reports,'statistics',lambda *args:(calls.append(1),original(*args))[1])
    a=statistics(service,task['id']);assert statistics(service,task['id'])==a and len(calls)==1
    change(service,sub,question_id='q1',change={'status':'incorrect'})
    b=statistics(service,task['id']);assert a['cache_token']!=b['cache_token'] and len(calls)==2

def test_read_cache_evicts_and_isolated_tokens():
    c=ReadCache(2);assert c.get('a','x',lambda:1)==1
    assert c.get('a','x',lambda:2)==1
    c.get('b','x',lambda:3);c.get('c','x',lambda:4)
    assert c.get('a','x',lambda:2)==2
    assert c.get('a','y',lambda:5)==5


def test_archived_detail_remains_readable_and_deleted_is_rejected(service):
    task,_,_=setup(service)
    with service.store.connection(write=True) as db:
        service.store.put(db,'tasks',{**service.store.get(db,'tasks',task['id']),'state':'archived'})
    assert detail(service,task['id'])['task']['state']=='archived'
    with service.store.connection(write=True) as db:
        service.store.put(db,'tasks',{**service.store.get(db,'tasks',task['id']),'state':'deleted'})
    with pytest.raises(Problem):detail(service,task['id'])

def plan(service,task,action='confirm'):
    return {'action':action,'request_id':uid(),'pages':[{'id':p['id'],'revision':p['revision']} for p in detail(service,task['id'])['pages']]}

def test_bulk_review_preview_execute_replay_and_revoke(service):
    task,sub=processed(service,True);data=plan(service,task)
    preview=batch(service,task['id'],{**data,'preview':True});assert len(preview['accepted'])==2
    assert not any(p['reviewed'] for p in state(service,sub)['pages'])
    result=batch(service,task['id'],{**data,'confirm':True});assert len(result['accepted'])==2
    assert state(service,sub)['submission']['state']=='reviewed'
    assert batch(service,task['id'],{**data,'confirm':True})==result
    old=deepcopy(state(service,sub)['pages'][0]['results'])
    revoked=batch(service,task['id'],{**plan(service,task,'unconfirm'),'confirm':True})
    assert len(revoked['accepted'])==2 and not any(p['reviewed'] for p in state(service,sub)['pages'])
    assert state(service,sub)['pages'][0]['results']==old

@pytest.mark.parametrize('issue',['unknown','photo','retake','revision'])
def test_bulk_partially_rejects_with_reasons(service,issue):
    task,sub=processed(service);data=plan(service,task)
    if issue=='unknown':change(service,sub,question_id='q1',change={'status':'unknown'});data=plan(service,task)
    if issue in ('photo','retake'):
        change(service,sub,photo_status='pending' if issue=='photo' else 'retake_required');data=plan(service,task)
    if issue=='revision':data['pages'][0]['revision']-=1
    result=batch(service,task['id'],{**data,'confirm':True});assert len(result['rejected'])==1 and len(result['accepted'])==1
    assert not state(service,sub)['pages'][0]['reviewed']

def test_batch_intent_and_explicit_confirmation(service):
    task,sub=processed(service);data=plan(service,task)
    with pytest.raises(Problem) as e:batch(service,task['id'],data)
    assert e.value.code=='CONFIRM_REQUIRED'
    batch(service,task['id'],{**data,'confirm':True})
    with pytest.raises(Problem):batch(service,task['id'],{**data,'action':'unconfirm','confirm':True})

def test_new_task_handwriting_default_and_old_draft_preserved(service):
    a=service.create_task({'name':'New','custom':'班级','request_id':uid()});b=service.draft(a['task']['id']) if 'task' in a else service.draft(a['id'])
    assert {'hooked-y','handwritten-forms'}<={r['id'] for r in b['writing_rules']}
    old=service.create('tasks',{'name':'Old'});d=service.draft(old['id']);service.draft(old['id'],{**d,'writing_rules':[]})
    assert service.draft(old['id'])['writing_rules']==[]

def test_new_prompt_keeps_frozen_schema_and_literal_safeguards():
    current=prompt_record('student');assert current['schema_version']=='6.0'
    assert 'single-storey a/g' in current['template_text'] and 'dod' in current['template_text']
    from autoreview.v2.contracts import PROMPTS
    frozen=frozen_record(PROMPTS['student']);assert frozen['schema_version']=='5.2'
    from autoreview.v2.contracts_v12 import record as frozen_v12
    previous=frozen_v12(PROMPTS['student'])
    assert previous['schema_hash']==frozen['schema_hash'] and previous['prompt_hash']!=frozen['prompt_hash']
    assert current['schema_hash']!=previous['schema_hash']
    assert decode_output('student','5.3','{"cells":[],"photo_issues":[]}')=={'cells':[],'photo_issues':[]}

def test_api_sync_activity_and_logs_do_not_stall_event_loop(tmp_path,monkeypatch):
    settings=MachineSettings(tmp_path/'m');settings.save(library_location=str(tmp_path/'l'))
    app=create_app(settings,nonce='test',start_worker=False);svc=app.state.service;started=threading.Event();release=threading.Event()
    from contextlib import contextmanager
    task=svc.create('tasks',{'name':'lock test'})
    original=svc.store.connection
    @contextmanager
    def block(write=False):
        if write:started.set();release.wait(3)
        with original(write=write) as db:yield db
    monkeypatch.setattr(svc.store,'connection',block)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver',headers={'X-AutoReview-Session':'test'}) as c:
            pending=asyncio.create_task(c.post('/api/activity',json={'id':'test','task_id':task['id'],'sequence':1,'active_ms':1}))
            assert await asyncio.to_thread(started.wait,1)
            before=time.monotonic();assert (await c.get('/api/state')).status_code==200;assert time.monotonic()-before<.5
            release.set();assert (await pending).status_code==200
    try:asyncio.run(run())
    finally:release.set();svc.close()

def test_queue_offload_keeps_api_loop_alive_and_rpc_on_owner_loop(service,monkeypatch):
    from autoreview.v2.runner import Runner
    started=threading.Event();release=threading.Event();loops=[]
    class Provider:
        async def quota(self,*args):loops.append(asyncio.get_running_loop());return {'ok':True}
    r=Runner(service,Provider())
    async def tick():
        started.set();release.wait(2)
        assert (await r.rpc('quota','test'))['ok']
        return True
    async def run():
        owner=asyncio.get_running_loop();pending=asyncio.create_task(r.offload(tick))
        assert await asyncio.to_thread(started.wait,1)
        before=time.monotonic();await asyncio.sleep(.01);assert time.monotonic()-before<.2
        release.set();assert await pending;assert loops==[owner]
    try:asyncio.run(run())
    finally:release.set()
