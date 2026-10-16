"""Frozen capture account and multiworker recovery boundaries, fully offline."""
import asyncio

import pytest

from autoreview.v2 import stream_capture as capture
from autoreview.v2.core import Problem
from autoreview.v2.runner import Runner
from test_workflow import service, setup, intake, enqueue, FakeProvider
from test_v130_capture import begin, shot, confirmation


def test_capture_cannot_silently_append_to_run_resumed_on_another_account(service):
    task, group, _, session, _=begin(service,n=1)
    runner=Runner(service,FakeProvider())
    first=shot(service,session)
    result=capture.commit(service,first['id'],confirmation(first),runner.enqueue_capture)
    run=result['queue']['runs'][0]
    asyncio.run(runner.stop(run['id']))
    new_config={**run['config'],'profile_id':'a-different-explicitly-selected-account'}
    successor=runner.resume(run['id'],new_config)
    assert successor['config']['profile_id'] != session['config']['profile_id']
    second=shot(service,session)
    with pytest.raises(Problem) as caught:
        capture.commit(service,second['id'],confirmation(second),runner.enqueue_capture)
    assert caught.value.code=='CAPTURE_CONFIG_CHANGED'
    with service.store.connection() as db:
        assert len(service.store.list(db,'pages',task_id=task['id']))==1
        assert service.store.get(db,'groups',group['id'])['next_tag']==2
        assert service.store.get(db,'capture_frames',second['id'])['state']=='draft'
        assert service.store.get(db,'capture_sessions',session['id'])['config']==session['config']


def test_recovery_pauses_every_old_worker_and_preserves_newer_page_generation(service):
    task,groups,_=setup(service,1)
    intake(service,groups[0],4)
    runner=Runner(service,FakeProvider())
    run=enqueue(runner,task,groups[:1])
    with service.store.connection(write=True) as db:
        service.store.put(db,'runs',{**run,'state':'running','config':{**run['config'],'parallelism':4}})
        work=service.store.list(db,'work_items',parent_id=run['id'])
        for index,item in enumerate(work):
            service.store.put(db,'work_items',{**item,'state':'running'})
            page=service.store.get(db,'pages',item['page_id'])
            service.store.put(db,'pages',{**page,'state':'running','generation':page['generation']+(index==3)})
            service.store.put(db,'attempts',{'parent_id':item['id'],'task_id':task['id'],
                'state':'running','usage':{'totalTokens':index+1}})
    runner.recover(allow=False)
    with service.store.connection() as db:
        assert service.store.get(db,'runs',run['id'])['state']=='paused'
        assert all(item['state']=='pending' for item in service.store.list(db,'work_items',parent_id=run['id']))
        assert [service.store.get(db,'pages',item['page_id'])['state'] for item in work]==['pending']*3+['running']
        attempts=service.store.list(db,'attempts',task_id=task['id'])
        assert all(a['state']=='interrupted' and a['error_code']=='PROCESS_INTERRUPTED' for a in attempts)
        assert sorted(a['usage']['totalTokens'] for a in attempts)==[1,2,3,4]
    assert runner.provider.calls==[]


def test_paused_run_inflight_request_still_limits_other_runs(service):
    first,groups,_=setup(service,1)
    intake(service,groups[0],1)
    second,other_groups,_=setup(service,1)
    intake(service,other_groups[0],4)
    class Measured(FakeProvider):
        active=0
        peak=0
        async def infer(self,*args):
            self.active+=1;self.peak=max(self.peak,self.active)
            try:
                await asyncio.sleep(.04)
                return await super().infer(*args)
            finally:self.active-=1
    provider=Measured();runner=Runner(service,provider)
    first_run=enqueue(runner,first,groups[:1])
    second_run=enqueue(runner,second,other_groups[:1])
    with service.store.connection(write=True) as db:
        service.store.put(db,'runs',{**first_run,'state':'paused','error_code':'USAGE_LIMIT',
            'config':{**first_run['config'],'parallelism':2}})
        item=service.store.list(db,'work_items',parent_id=first_run['id'])[0]
        service.store.put(db,'work_items',{**item,'state':'running'})
        service.store.put(db,'runs',{**second_run,'config':{**second_run['config'],'parallelism':4}})
    async def advance():
        await asyncio.gather(*(runner.tick() for _ in range(4)))
    asyncio.run(advance())
    # One old request remains in flight. At most one of the four new workers may
    # dispatch, retaining the old request's frozen global limit of two.
    assert provider.peak==1 and len(provider.calls)==1
    with service.store.connection() as db:
        assert service.store.get(db,'work_items',item['id'])['state']=='running'


@pytest.mark.parametrize('allow',[False,True])
def test_paused_inflight_restart_cleans_ghost_slots_and_keeps_manual_history(service,allow):
    from autoreview.v2.core import digest
    task,groups,_=setup(service,1);intake(service,groups[0],3)
    provider=FakeProvider();runner=Runner(service,provider);run=enqueue(runner,task,groups[:1])
    asyncio.run(runner.tick())
    with service.store.connection(write=True) as db:
        completed,running,pending=service.store.list(db,'work_items',parent_id=run['id'])
        done_page=service.store.get(db,'pages',completed['page_id'])
    done_page=service.review(done_page['id'],{'revision':done_page['revision'],'question_id':'q1',
        'change':{'status':'correct','note':'教师已经修改'},'action':'edit'})
    done_page=service.review(done_page['id'],{'revision':done_page['revision'],'action':'confirm'})
    frozen=digest(done_page)
    with service.store.connection(write=True) as db:
        live=service.store.get(db,'runs',run['id'])
        old=service.store.put(db,'runs',{**live,'state':'paused','error_code':'USAGE_LIMIT'})
        running=service.store.put(db,'work_items',{**running,'state':'running'})
        page=service.store.get(db,'pages',running['page_id'])
        service.store.put(db,'pages',{**page,'state':'running'})
        attempt=service.store.put(db,'attempts',{'parent_id':running['id'],'task_id':task['id'],
            'state':'running','usage':{'totalTokens':7}})
    runner.recover(allow=allow)
    with service.store.connection() as db:
        recovered=service.store.get(db,'runs',run['id'])
        assert recovered['state']=='paused' and recovered['generation']==old['generation']+1
        assert recovered['error_code']=='USAGE_LIMIT' and recovered['verification_policy']==run['verification_policy']
        assert not runner.has_inflight(db,run['id']) and not runner.valid(db,old,running)
        assert service.store.get(db,'work_items',running['id'])['state']=='pending'
        assert service.store.get(db,'pages',page['id'])['state']=='pending'
        interrupted=service.store.get(db,'attempts',attempt['id'])
        assert interrupted['state']=='interrupted' and interrupted['elapsed_ms'] is None and interrupted['usage']=={'totalTokens':7}
        assert digest(service.store.get(db,'pages',done_page['id']))==frozen
    runner.recover(allow=True)
    with service.store.connection() as db:
        assert service.store.get(db,'runs',run['id'])==recovered  # Idempotent when nothing remains in flight.
    assert not asyncio.run(runner.tick())
    runner.resume(run['id'])
    from test_workflow import drain
    asyncio.run(drain(runner))
    assert len(provider.calls)==3  # Completed, teacher-reviewed page is not submitted again.
    with service.store.connection() as db:
        assert digest(service.store.get(db,'pages',done_page['id']))==frozen
        assert service.store.get(db,'runs',run['id'])['state']=='completed'


@pytest.mark.parametrize('reason',['application_exit','ui_connection_lost'])
def test_shutdown_stop_cleans_paused_inflight_without_touching_newer_page(service,reason):
    task,groups,_=setup(service,1);intake(service,groups[0],1)
    runner=Runner(service,FakeProvider());run=enqueue(runner,task,groups[:1])
    with service.store.connection(write=True) as db:
        run=service.store.put(db,'runs',{**run,'state':'paused','error_code':'USAGE_LIMIT'})
        work=service.store.list(db,'work_items',parent_id=run['id'])[0]
        work=service.store.put(db,'work_items',{**work,'state':'running'})
        page=service.store.get(db,'pages',work['page_id'])
        newer=service.store.put(db,'pages',{**page,'state':'processed','generation':page['generation']+1,
            'reviewed':True,'overrides':{'q1':{'status':'correct'}}})
        attempt=service.store.put(db,'attempts',{'parent_id':work['id'],'task_id':task['id'],'state':'running','usage':None})
    asyncio.run(runner.stop(run['id'],reason=reason,interrupt=False))
    with service.store.connection() as db:
        stopped=service.store.get(db,'runs',run['id'])
        assert stopped['state']=='paused' and stopped['generation']==run['generation']+1
        assert not runner.has_inflight(db,run['id'])
        assert service.store.get(db,'pages',newer['id'])==newer
        assert service.store.get(db,'attempts',attempt['id'])['state']=='interrupted'
    runner.recover(allow=True)
    assert not asyncio.run(runner.tick()) and not runner.provider.calls


def test_closing_rejects_new_and_resumed_runs_but_allows_original_receipt(service):
    from test_workflow import config
    from autoreview.v2.core import uid
    task,groups,_=setup(service,1);intake(service,groups[0],1)
    runner=Runner(service,FakeProvider());run=enqueue(runner,task,groups[:1])
    asyncio.run(runner.stop(run['id']))
    # Create the original receipt now, before closure, then repeat it later.
    request={'request_id':uid(),'group_ids':[groups[0]['id']],'cloud_consent':True,'images_prepared':True}
    original=runner.create(task['id'],request,config())
    runner.closing=True
    assert runner.create(task['id'],request,config())==original
    with pytest.raises(Problem) as error:runner.create(task['id'],{**request,'request_id':uid()},config())
    assert error.value.code=='APPLICATION_CLOSING'
    with pytest.raises(Problem) as error:runner.resume(run['id'])
    assert error.value.code=='APPLICATION_CLOSING'
    with pytest.raises(Problem) as error:runner.retry_region('any',{'request_id':uid(),'cloud_consent':True},config())
    assert error.value.code=='APPLICATION_CLOSING'


def test_closing_capture_rolls_back_page_allocation_and_keeps_draft(service):
    task,group,_,session,_=begin(service,n=1)
    runner=Runner(service,FakeProvider());frame=shot(service,session)
    with service.store.connection() as db:
        before=service.store.get(db,'groups',group['id'])['next_tag']
    runner.closing=True
    with pytest.raises(Problem) as error:capture.commit(service,frame['id'],confirmation(frame),runner.enqueue_capture)
    assert error.value.code=='APPLICATION_CLOSING'
    with service.store.connection() as db:
        assert not service.store.list(db,'pages',task_id=task['id']) and not service.store.list(db,'runs',task_id=task['id'])
        assert service.store.get(db,'groups',group['id'])['next_tag']==before
        assert service.store.get(db,'capture_frames',frame['id'])['state']=='draft'


def test_api_shutdown_cleans_paused_request_before_scheduling_exit(tmp_path):
    from autoreview.v2.api import create_app
    from autoreview.v2.core import MachineSettings
    settings=MachineSettings(tmp_path/'machine');settings.save(library_location=str(tmp_path/'library'))
    app=create_app(settings,nonce='isolated',start_worker=False)
    service=app.state.service;runner=app.state.runner
    try:
        task,groups,_=setup(service,1);intake(service,groups[0],1)
        run=enqueue(runner,task,groups[:1])
        with service.store.connection(write=True) as db:
            service.store.put(db,'runs',{**run,'state':'paused','error_code':'USAGE_LIMIT'})
            work=service.store.list(db,'work_items',parent_id=run['id'])[0]
            service.store.put(db,'work_items',{**work,'state':'running'})
            page=service.store.get(db,'pages',work['page_id'])
            service.store.put(db,'pages',{**page,'state':'running'})
            attempt=service.store.put(db,'attempts',{'parent_id':work['id'],'task_id':task['id'],'state':'running','usage':None})
        result=asyncio.run(app.state.request_shutdown())
        assert result['ok'] and runner.closing
        with service.store.connection() as db:
            live=service.store.get(db,'runs',run['id'])
            assert live['state']=='paused' and live['generation']==run['generation']+1
            assert not runner.has_inflight(db,run['id'])
            assert service.store.get(db,'attempts',attempt['id'])['state']=='interrupted'
        runner.recover(allow=True)
        assert not asyncio.run(runner.tick())
    finally:service.close()


@pytest.mark.parametrize('passed_gate',[False,True])
def test_shutdown_writer_barrier_serializes_delayed_run_creation(tmp_path,monkeypatch,passed_gate):
    """A request already holding the writer cannot escape the shutdown snapshot."""
    import threading
    from autoreview.v2.api import create_app
    from autoreview.v2.core import MachineSettings,uid
    from test_workflow import config
    settings=MachineSettings(tmp_path/'machine');settings.save(library_location=str(tmp_path/'library'))
    app=create_app(settings,nonce='isolated',start_worker=False)
    service=app.state.service;runner=app.state.runner
    entered=threading.Event();release=threading.Event()
    try:
        task,groups,_=setup(service,1);intake(service,groups[0],1)
        # Only preparation, no first queued run.
        for page in service.detail(task['id'])['pages']:
            p=service.prepare_page(page['id'],{})
            service.prepare_page(p['id'],{'action':'confirm','revision':p['revision'],'stamp':p['preparation']['stamp']})
        request={'request_id':uid(),'group_ids':[groups[0]['id']],'cloud_consent':True,'images_prepared':True}
        original_once=service.store.once;original_put=service.store.put
        def once(db,key,data):
            if not passed_gate and key==request['request_id']:
                entered.set();assert release.wait(3)
            return original_once(db,key,data)
        def put(db,table,data,**kwargs):
            if passed_gate and table=='runs' and data.get('state')=='queued':
                entered.set();assert release.wait(3)
            return original_put(db,table,data,**kwargs)
        monkeypatch.setattr(service.store,'once',once);monkeypatch.setattr(service.store,'put',put)
        async def exercise():
            pending=asyncio.create_task(asyncio.to_thread(runner.create,task['id'],request,config()))
            assert await asyncio.to_thread(entered.wait,3)
            shutdown=asyncio.create_task(app.state.request_shutdown())
            while not runner.closing:await asyncio.sleep(.001)
            release.set()
            result=(await asyncio.gather(pending,return_exceptions=True))[0]
            assert (await shutdown)['ok']
            if not passed_gate:assert isinstance(result,Problem) and result.code=='APPLICATION_CLOSING'
            else:assert result['runs']
        asyncio.run(exercise())
        with service.store.connection() as db:
            runs=service.store.list(db,'runs',task_id=task['id'])
            assert all(r['state']=='paused' for r in runs)
            assert len(runs)==int(passed_gate)
        runner.recover(allow=True)
        assert not asyncio.run(runner.tick())
    finally:
        release.set();service.close()
