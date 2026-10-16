"""No cloud. Real queue/SQLite contracts; recognition is an explicit local double."""
import asyncio
import pytest
from autoreview.v2.core import MachineSettings, Problem, uid
from autoreview.v2.execution_config import freeze, validate_settings
from autoreview.v2.runner import Runner
from autoreview.v2 import stream_capture as capture
from test_workflow import service, setup, FakeProvider, config, intake, enqueue
from test_v130_capture import shot, confirmation


@pytest.mark.parametrize('batch,stream', [(1,13),(13,1),(7,4),(4,10)])
def test_independent_settings_and_legacy_default(service,batch,stream):
    settings={**service.settings.data,'parallelism':batch,'capture_parallelism':stream}
    assert freeze(settings)['parallelism']==batch
    assert freeze(settings,purpose='capture')['parallelism']==stream
    assert freeze({**settings,'parallelism':2},purpose='capture')['parallelism']==stream
    del settings['capture_parallelism']
    assert freeze(settings,purpose='capture')['parallelism']==10


def test_new_defaults_and_saved_explicit_values_are_distinct(tmp_path):
    settings=MachineSettings(tmp_path/'machine')
    assert settings.data['parallelism']==10
    assert settings.data['capture_parallelism']==10
    batch_config=freeze(settings.data)
    stream_config=freeze(settings.data,purpose='capture')
    assert freeze(settings.data,purpose='reference')['parallelism']==1
    settings.save(parallelism=4,capture_parallelism=7)
    reopened=MachineSettings(settings.home)
    assert reopened.data['parallelism']==4
    assert reopened.data['capture_parallelism']==7
    assert batch_config['parallelism']==10
    assert stream_config['parallelism']==10


def test_missing_batch_preference_uses_default_without_reading_capture(service):
    settings={**service.settings.data,'capture_parallelism':3}
    settings.pop('parallelism')
    assert freeze(settings)['parallelism']==10
    assert freeze(settings,purpose='capture')['parallelism']==3


@pytest.mark.parametrize('value',[0,14,True,1.5,'4',None])
def test_stream_setting_validation(value):
    with pytest.raises(Problem):validate_settings({'capture_parallelism':value})


def start_capture(service,task,group,n=3):
    return capture.start(service,task['id'],{'group_id':group['id'],'source':'camera','request_id':uid(),'cloud_consent':True},
                         {**config(),'parallelism':n,'conservative_review':False})


def test_first_page_runs_before_second_is_captured(service):
    task,groups,_=setup(service,2)
    provider=FakeProvider();runner=Runner(service,provider)
    session=start_capture(service,task,groups[0])
    first=shot(service,session);result=capture.commit(service,first['id'],confirmation(first),runner.enqueue_capture)
    assert result['state']=='queued'
    asyncio.run(runner.tick())
    detail=service.detail(task['id'])
    assert len(detail['pages'])==1 and detail['pages'][0]['state']=='processed'
    assert detail['submissions'][0]['loaded_pages']==1
    assert len(provider.calls)==1
    capture.stop(service,session['id'])


@pytest.mark.parametrize('paused_batch',[False,True])
@pytest.mark.parametrize('batch_degree,stream_degree',[(1,3),(10,10)])
def test_lanes_keep_independent_capacity(service,paused_batch,batch_degree,stream_degree):
    task,groups,_=setup(service,1)
    runner=Runner(service,FakeProvider())
    intake(service,groups[0],batch_degree+2)
    batch=enqueue(runner,task,groups[:1])
    with service.store.connection(write=True) as db:
        service.store.put(db,'runs',{**batch,'config':{**batch['config'],'parallelism':batch_degree}})
    session=start_capture(service,task,groups[1],stream_degree)
    for _ in range(stream_degree+1):
        frame=shot(service,session);capture.commit(service,frame['id'],confirmation(frame),runner.enqueue_capture)
    async def run():
        entered=[];gate=asyncio.Event()
        async def blocked(run,item):
            entered.append(run.get('source','batch'))
            if paused_batch and run.get('source')!='stream_capture' and entered.count('batch')==batch_degree:
                with service.store.connection(write=True) as db:
                    current=service.store.get(db,'runs',run['id'])
                    service.store.put(db,'runs',{**current,'state':'paused','error_code':'LOCAL_PAUSE'})
            await gate.wait()
        runner.process=blocked
        pending=[asyncio.create_task(runner.tick()) for _ in range(batch_degree+stream_degree+4)]
        for _ in range(100):
            if len(entered)>=batch_degree+stream_degree:break
            await asyncio.sleep(.01)
        assert entered.count('batch')==batch_degree
        assert entered.count('stream_capture')==stream_degree
        assert runner.worker_capacity==batch_degree+stream_degree
        for t in pending:t.cancel()
        await asyncio.gather(*pending,return_exceptions=True)
    asyncio.run(run())


def test_out_of_order_and_middle_failure_keep_original_positions(service):
    task,groups,_=setup(service,1)
    class Delayed(FakeProvider):
        async def infer(self,*args,**kwargs):
            index=len(self.gates);gate=asyncio.Event();self.gates.append(gate)
            await gate.wait()
            if index==1:raise Problem('LOCAL_NETWORK_SIMULATION','本地模拟中断')
            return await super().infer(*args,**kwargs)
    provider=Delayed();provider.gates=[]
    runner=Runner(service,provider);session=start_capture(service,task,groups[0])
    results=[]
    for _ in range(3):
        frame=shot(service,session);results.append(capture.commit(service,frame['id'],confirmation(frame),runner.enqueue_capture))
    async def run():
        ticks=[asyncio.create_task(runner.tick()) for _ in range(3)]
        for _ in range(100):
            if len(provider.gates)==3:break
            await asyncio.sleep(.01)
        assert len(provider.gates)==3
        provider.gates[2].set();await ticks[2]
        provider.gates[1].set();await ticks[1]
        provider.gates[0].set();await ticks[0]
    asyncio.run(run())
    detail=service.detail(task['id'])
    assert [p['id'] for p in detail['pages']]==[r['page_id'] for r in results]
    assert [s['paper_tag'] for s in detail['submissions']]==['001','002','003']
    assert [p['state'] for p in detail['pages']]==['processed','failed','processed']
    assert detail['runs'][0]['state']=='running'
    failed = next(w for w in detail['work_items'] if w['page_id'] == results[1]['page_id'])
    assert failed['state'] == 'failed' and failed['error_code'] == 'LOCAL_NETWORK_SIMULATION'
    assert capture.frame(service,results[1]['frame_id'])['commit_result']['page_id']==results[1]['page_id']
