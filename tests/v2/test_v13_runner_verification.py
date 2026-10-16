"""Isolated fake-provider fault injection; no account or handwriting claims."""
import asyncio
import pytest
from test_workflow import service, intake, enqueue, FakeProvider
from autoreview.v2.runner import Runner
from autoreview.v2.core import Problem


def scene(service, provider, pages=1, degree=1, writing=False):
    task=service.create('tasks',{'name':'独立复看故障测试'})
    group=service.create('groups',{'name':'测试班','parent_id':task['id']})
    zone={'x1':.1,'y1':.2,'x2':.7,'y2':.5}
    draft=service.draft(task['id'],{'page_count':1,
        'writing_rules':[{'id':'handwriting','description':'笔迹检查'}] if writing else [],
        'items':[{'id':'q','number':'1','slot':1,'kind':'fill_blank','response_encoding':'text',
                  'english':'apple','uncertain':False,'box':zone,'answer_zone':zone,'geometry_checked':True}]})
    service.confirm_reference(task['id'],draft['revision'])
    intake(service,group,pages)
    runner=Runner(service,provider)
    run=enqueue(runner,task,[group])
    with service.store.connection(write=True) as db:
        run=service.store.put(db,'runs',{**run,'config':{**run['config'],'parallelism':degree,
            'conservative_review':True,'verification_effort':'medium'}})
    return runner,run,task


class ClearProvider(FakeProvider):
    async def infer(self,*args):
        result=await super().infer(*args)
        for cell in result['output'].get('cells',[]):
            cell.update(raw_english='apple',response_evidence='clear',response_reason='',
                        transcription_uncertain=False,literal_evidence='clear',ambiguous_glyphs=[])
        return result


@pytest.mark.parametrize('code',['VERIFY_FAILURE','USAGE_LIMIT'])
def test_failed_second_pass_keeps_first_but_cannot_release_correct(service,code):
    class Failure(ClearProvider):
        async def infer(self,*args):
            result=await super().infer(*args)
            if args[1]=='student_verify':
                raise Problem(code,'复看失败',usage={'inputTokens':7,'outputTokens':2})
            return result
    provider=Failure();runner,run,task=scene(service,provider,writing=code=='USAGE_LIMIT')
    asyncio.run(runner.tick())
    page=service.detail(task['id'])['pages'][0]
    result=page['results'][0]
    if code == 'VERIFY_FAILURE':
        # A failed processing stage is now a page failure, not a completed page
        # full of synthetic unknown answers. The paid first pass stays resumable.
        assert page['state'] == 'failed' and result['status'] != 'correct'
        with service.store.connection() as db:
            assert any(c['purpose'] == 'student' and c['eligible'] for c in service.store.list(db, 'checkpoints'))
            assert service.store.get(db, 'runs', run['id'])['state'] == 'running'
        return
    assert page['state']=='processed' and result['status']=='unknown'
    assert result['machine_raw_english']=='apple' and result['verification']['status']=='pending'
    with service.store.connection() as db:
        attempts=service.store.list(db,'attempts')
        failed=next(a for a in attempts if a['purpose']=='student_verify')
        assert failed['state']=='failed' and failed['usage']['inputTokens']==7
        assert result['verification']['second_attempt_id']==failed['id']
        assert service.store.get(db,'runs',run['id'])['state']==('paused' if code=='USAGE_LIMIT' else 'running')
        observation=service.store.get(db,'observations',page['observation_id'])
        if code=='USAGE_LIMIT':
            assert observation['writing'] and all(w['status']=='unknown' for w in observation['writing'])
            assert not any(c['purpose']=='writing' for c in provider.calls)


def test_limit_in_one_worker_blocks_sibling_new_passes_but_preserves_first(service):
    async def check():
        class Limited(ClearProvider):
            def __init__(self):
                super().__init__();self.students=0;self.two_started=asyncio.Event();self.release_second=asyncio.Event()
            async def infer(self,*args):
                if args[1]=='student':
                    self.students+=1;index=self.students
                    if index==2:
                        self.two_started.set();await self.release_second.wait()
                    else:await self.two_started.wait()
                result=await super().infer(*args)
                if args[1]=='student_verify':raise Problem('USAGE_LIMIT','额度不足')
                return result
        provider=Limited();runner,run,task=scene(service,provider,pages=3,degree=2,writing=True)
        jobs=[asyncio.create_task(runner.tick()) for _ in range(2)]
        async def paused():
            while True:
                with service.store.connection() as db:
                    if service.store.get(db,'runs',run['id'])['state']=='paused':return
                await asyncio.sleep(.005)
        await asyncio.wait_for(paused(),3)
        provider.release_second.set();await asyncio.gather(*jobs)
        assert [c['purpose'] for c in provider.calls].count('student')==2
        assert [c['purpose'] for c in provider.calls].count('student_verify')==1
        assert not any(c['purpose']=='writing' for c in provider.calls)
        assert not await runner.tick()
        with service.store.connection() as db:
            observations=service.store.list(db,'observations')
            assert len(observations)==2
            assert all(o['verification']['q']['status']=='pending' for o in observations)
            assert all(o['writing'] and all(w['status']=='unknown' for w in o['writing']) for o in observations)
            assert len([w for w in service.store.list(db,'work_items') if w['state']=='pending'])==1
        assert not runner.active_attempts and not runner.active_work
    asyncio.run(check())


def test_quota_failure_cannot_lose_success_or_repeat_paid_checkpoint(service):
    class BadQuota(ClearProvider):
        async def quota(self,profile_id):raise OSError('隔离额度观测异常')
    provider=BadQuota();runner,run,task=scene(service,provider)
    asyncio.run(runner.tick())
    assert service.detail(task['id'])['pages'][0]['results'][0]['status']=='correct'
    assert [c['purpose'] for c in provider.calls]==['student','student_verify']
    with service.store.connection(write=True) as db:
        live=service.store.get(db,'runs',run['id'])
        live=service.store.put(db,'runs',{**live,'state':'paused','error_code':'USAGE_LIMIT'})
        item=service.store.list(db,'work_items')[0]
        page=service.store.get(db,'pages',item['page_id'])
        version=service.store.get(db,'image_versions',page['image_version_id'])
        attempts=service.store.list(db,'attempts')
        assert len(attempts)==2 and all(a['state']=='completed' for a in attempts)
        assert all(a['quota_before'] is None and a['quota_after'] is None for a in attempts)
        assert len(service.store.list(db,'checkpoints'))==2
    result,identifier=asyncio.run(runner.call(live,item,'student',[service.asset(version['asset_id'])],provider.calls[0]['variables']))
    assert result['output']['cells'][0]['raw_english']=='apple' and identifier
    assert len(provider.calls)==2
    with service.store.connection() as db:
        assert len(service.store.list(db,'recovery_adoptions'))==1


@pytest.mark.parametrize('code',['USAGE_LIMIT','QUEUE_PAUSED'])
def test_writing_limit_pauses_new_pages_but_retains_inflight_success(service,code):
    async def check():
        class WritingLimit(ClearProvider):
            def __init__(self):
                super().__init__();self.writing_count=0;self.two_writing=asyncio.Event();self.release_second=asyncio.Event()
            async def infer(self,*args):
                result=await super().infer(*args)
                if args[1]=='writing':
                    self.writing_count+=1;index=self.writing_count
                    if index==1:
                        await self.two_writing.wait()
                        raise Problem(code,'书写检查额度不足')
                    self.two_writing.set();await self.release_second.wait()
                    result['output']['items']=[{'question_id':'q','rule_id':'handwriting',
                        'status':'no_issue','evidence':'清楚','box':None}]
                return result
        provider=WritingLimit();runner,run,task=scene(service,provider,pages=3,degree=2,writing=True)
        jobs=[asyncio.create_task(runner.tick()) for _ in range(2)]
        async def paused():
            while True:
                with service.store.connection() as db:
                    live=service.store.get(db,'runs',run['id'])
                    if live['state']=='paused':return live
                await asyncio.sleep(.005)
        live=await asyncio.wait_for(paused(),3)
        assert live['error_code']==code and live['generation']==run['generation']
        assert not await runner.tick()  # The third image must not be submitted.
        provider.release_second.set();await asyncio.gather(*jobs)
        assert [c['purpose'] for c in provider.calls]==['student','student_verify','writing']*2
        with service.store.connection() as db:
            observations=service.store.list(db,'observations')
            assert len(observations)==2
            assert all(o['cells'][0]['raw_english']=='apple' and o['verification']['q']['status']=='verified' for o in observations)
            assert sorted(o['writing'][0]['status'] for o in observations)==['no_issue','unknown']
            assert len([w for w in service.store.list(db,'work_items') if w['state']=='pending'])==1
            live=service.store.get(db,'runs',run['id'])
            assert live['state']=='paused' and live['error_code']==code
        assert sorted(p['results'][0]['writing_status'] for p in service.detail(task['id'])['pages'] if p['state']=='processed')==['clear','pending']
        assert not await runner.tick() and not runner.active_attempts and not runner.active_work
    asyncio.run(check())


@pytest.mark.parametrize('degree',[1,3])
def test_stop_during_second_pass_discards_late_results_for_all_active_pages(service,degree):
    async def check():
        class WaitForVerify(ClearProvider):
            def __init__(self):
                super().__init__();self.verify_count=0;self.ready=asyncio.Event();self.release_verify=asyncio.Event()
            async def infer(self,*args):
                result=await super().infer(*args)
                if args[1]=='student_verify':
                    self.verify_count+=1
                    if self.verify_count==degree:self.ready.set()
                    await self.release_verify.wait()
                return result
        provider=WaitForVerify();runner,run,task=scene(service,provider,pages=degree,degree=degree)
        jobs=[asyncio.create_task(runner.tick()) for _ in range(degree)]
        await asyncio.wait_for(provider.ready.wait(),3)
        await runner.stop(run['id']);provider.release_verify.set();await asyncio.gather(*jobs)
        assert len(set(provider.interruptions))==degree
        with service.store.connection() as db:
            assert not service.store.list(db,'observations')
            assert all(w['state']=='pending' for w in service.store.list(db,'work_items'))
            assert all(not c['eligible'] for c in service.store.list(db,'checkpoints') if c['purpose']=='student_verify')
        assert not runner.active_attempts and not runner.active_work
        # On an explicit resume, first observations completed before stop are
        # durable and reusable. A late, invalid second result is not adopted.
        runner.resume(run['id'])
        await asyncio.gather(*(runner.tick() for _ in range(degree)))
        assert [c['purpose'] for c in provider.calls].count('student')==degree
        assert [c['purpose'] for c in provider.calls].count('student_verify')==degree*2
        with service.store.connection() as db:
            assert len(service.store.list(db,'observations'))==degree
            assert len(service.store.list(db,'recovery_adoptions'))==degree
    asyncio.run(check())


def test_new_run_freezes_v2_while_legacy_resume_reuses_v1_checkpoints(service):
    from autoreview.v2.core import digest
    provider=ClearProvider();runner,run,task=scene(service,provider)
    assert run['verification_policy']=='blind-native-2'
    # Missing field is the exact representation of a persisted pre-v2 run.
    with service.store.connection(write=True) as db:
        run=service.store.put(db,'runs',{k:v for k,v in run.items() if k!='verification_policy'})
    asyncio.run(runner.tick())
    assert provider.calls[1]['variables']['crop_manifest']['policy_version']=='blind-native-1'
    with service.store.connection(write=True) as db:
        live=service.store.get(db,'runs',run['id'])
        service.store.put(db,'runs',{**live,'state':'paused','error_code':'PROCESS_INTERRUPTED'})
        work=service.store.list(db,'work_items',parent_id=run['id'])[0]
        service.store.put(db,'work_items',{**work,'state':'pending'})
        page=service.store.get(db,'pages',work['page_id'])
        old_observation=service.store.get(db,'observations',page['observation_id'])
        assert old_observation['verification_policy']=='blind-native-1'
        frozen=digest(old_observation)
        service.store.put(db,'pages',{**page,'state':'pending'})
    resumed=runner.resume(run['id'])
    assert 'verification_policy' not in resumed
    asyncio.run(runner.tick())
    assert [c['purpose'] for c in provider.calls]==['student','student_verify']
    with service.store.connection() as db:
        assert len(service.store.list(db,'recovery_adoptions'))==2
        assert digest(service.store.get(db,'observations',old_observation['id']))==frozen
        assert all(o['verification_policy']=='blind-native-1' for o in service.store.list(db,'observations'))
