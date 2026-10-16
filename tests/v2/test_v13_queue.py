import asyncio
import pytest
from test_workflow import service, setup, intake, enqueue, FakeProvider
from autoreview.v2.runner import Runner
from autoreview.v2.core import Problem

@pytest.mark.parametrize('degree', [1,2,3,4])
def test_bounded_queue_parallelism_and_frozen_settings(service, degree):
    task, groups, _ = setup(service, 1)
    intake(service, groups[0], 5)
    class Measured(FakeProvider):
        active=0
        peak=0
        async def infer(self,*args):
            self.active += 1
            self.peak = max(self.peak,self.active)
            try:
                await asyncio.sleep(.05)
                return await super().infer(*args)
            finally:self.active -= 1
    provider=Measured(); runner=Runner(service,provider)
    run=enqueue(runner,task,groups[:1])
    with service.store.connection(write=True) as db:
        service.store.put(db,'runs',{**run,'config':{**run['config'],'parallelism':degree}})
    service.settings.save(parallelism=4 if degree==1 else 1)
    async def check():
        for _ in range(8):await asyncio.gather(*(runner.tick() for _ in range(4)))
    asyncio.run(check())
    assert provider.peak==degree
    assert len(provider.calls)==5
    with service.store.connection() as db:
        assert service.store.get(db,'runs',run['id'])['state']=='completed'
        assert len(service.store.list(db,'observations'))==5
        assert all(a['state']=='completed' for a in service.store.list(db,'attempts'))

@pytest.mark.parametrize('degree',[3,7,10,13])
def test_parallel_stop_interrupts_every_turn_and_rejects_late_results(service,degree):
    task, groups, _=setup(service,1);intake(service,groups[0],degree)
    async def check():
        provider=FakeProvider();provider.started=asyncio.Event();provider.release=asyncio.Event()
        runner=Runner(service,provider);run=enqueue(runner,task,groups[:1])
        with service.store.connection(write=True) as db:
            service.store.put(db,'runs',{**run,'config':{**run['config'],'parallelism':degree}})
        jobs=[asyncio.create_task(runner.tick()) for _ in range(degree)]
        while len(provider.calls)<degree:await asyncio.sleep(.01)
        await runner.stop(run['id']);provider.release.set();await asyncio.gather(*jobs)
        assert len(set(provider.interruptions))==degree
        assert not runner.active_attempts and not runner.active_work
        with service.store.connection() as db:
            assert service.store.list(db,'observations')==[]
            assert all(w['state']=='pending' for w in service.store.list(db,'work_items'))
    asyncio.run(check())


@pytest.mark.parametrize('degree',[4,7,10,13])
def test_real_worker_loop_reaches_high_frozen_limit_and_does_not_poll_idle_slots(service,degree):
    task,groups,_=setup(service,1);intake(service,groups[0],14)
    class Measured(FakeProvider):
        def __init__(self):
            super().__init__();self.active=0;self.peak=0
            self.reached=asyncio.Event();self.release_calls=asyncio.Event()
        async def infer(self,*args):
            self.active+=1;self.peak=max(self.peak,self.active)
            if self.active==degree:self.reached.set()
            try:
                await self.release_calls.wait()
                return await super().infer(*args)
            finally:self.active-=1
    async def exercise():
        provider=Measured();runner=Runner(service,provider)
        run=enqueue(runner,task,groups[:1])
        with service.store.connection(write=True) as db:
            service.store.put(db,'runs',{**run,'config':{**run['config'],'parallelism':degree}})
        # Settings edited after enqueue do not affect the frozen limit.
        service.settings.save(parallelism=1)
        runner.start()
        try:
            await asyncio.wait_for(provider.reached.wait(),8)
            assert provider.peak==degree
            provider.release_calls.set()
            for _ in range(160):
                with service.store.connection() as db:
                    finished=service.store.get(db,'runs',run['id'])['state']=='completed'
                if finished:break
                await asyncio.sleep(.05)
            assert finished
            assert provider.peak==degree and len(provider.calls)==14
            with service.store.connection() as db:
                attempts=service.store.list(db,'attempts')
                assert len(attempts)==14 and all(a['state']=='completed' for a in attempts)
                assert len({a['page_id'] for a in attempts})==14
                assert len(service.store.list(db,'observations'))==14
            # Once no work remains only slot zero polls the store.
            await asyncio.sleep(.6)
            assert runner.worker_capacity==1
        finally:
            provider.release_calls.set();await runner.close()
    asyncio.run(exercise())

def test_failure_pauses_dispatch_but_retains_inflight_success(service):
    task,groups,_=setup(service,1);intake(service,groups[0],4)
    class Partial(FakeProvider):
        index=0
        async def infer(self,*args):
            self.index+=1; index=self.index
            await asyncio.sleep(.01 if index==1 else .05)
            if index==1:raise Problem('USAGE_LIMIT','额度不足')
            return await super().infer(*args)
    provider=Partial();runner=Runner(service,provider);run=enqueue(runner,task,groups[:1])
    with service.store.connection(write=True) as db:
        service.store.put(db,'runs',{**run,'config':{**run['config'],'parallelism':3}})
    async def check():await asyncio.gather(*(runner.tick() for _ in range(3)))
    asyncio.run(check())
    with service.store.connection() as db:
        assert service.store.get(db,'runs',run['id'])['state']=='paused'
        assert len(service.store.list(db,'observations'))==2
        assert [w['state'] for w in service.store.list(db,'work_items')].count('pending')==2
