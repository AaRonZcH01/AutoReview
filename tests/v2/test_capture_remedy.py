import asyncio
import pytest
from autoreview.v2.capture_remedy import remedy
from autoreview.v2.core import Problem, uid
from autoreview.v2.read_cache import detail
from autoreview.v2 import stream_capture as capture
from test_capture_withdrawal import begin, submit
from test_workflow import service
from test_v130_capture import shot


def test_old_unique_indexes_migrate_and_ordinary_trash_still_occupies_identity(service):
    from autoreview.v2.core import Store
    import sqlite3
    _, _, session, runner = begin(service)
    first, _ = submit(service, session, runner)
    with service.store.connection(write=True) as db:
        for name, table, field in [('submission_tag','submissions','paper_tag'),('page_slot','pages','slot')]:
            db.execute('DROP INDEX '+name)
            db.execute(f"CREATE UNIQUE INDEX {name} ON {table}(parent_id,json_extract(data,'$.{field}'))")
    reopened = Store(service.store.root, lock=False)
    with reopened.connection(write=True) as db:
        assert 'capture_withdrawn' in db.execute("SELECT sql FROM sqlite_master WHERE name='page_slot'").fetchone()[0]
        page = reopened.get(db,'pages',first['page_id'])
        reopened.put(db,'pages',{**page,'state':'deleted'})
        with pytest.raises(sqlite3.IntegrityError):
            reopened.put(db,'pages',{**page,'id':uid(),'state':'pending'})


def perform(service, runner, page_id, action, **extra):
    plan = remedy(service, runner, page_id, {'preview': True, 'action': action})
    request = {'action': action, 'fingerprint': plan['fingerprint'], 'request_id': uid(), **extra}
    return remedy(service, runner, page_id, request), request


def test_replayed_undo_interrupts_only_original_work_not_redo(service):
    task, _, session, runner = begin(service)
    service.settings.data['active_profile_id'] = session['config']['profile_id']
    first, _ = submit(service, session, runner)
    held, request = perform(service, runner, first['page_id'], 'hold')
    perform(service, runner, first['page_id'], 'resume', cloud_consent=True)
    work = service.detail(task['id'])['work_items']
    assert len(work) == 2
    runner.active_attempts = {'old': {'run_id':work[0]['parent_id'],'work_id':work[0]['id'],'page_id':first['page_id']},
                             'new': {'run_id':work[1]['parent_id'],'work_id':work[1]['id'],'page_id':first['page_id']}}
    replay = remedy(service, runner, first['page_id'], request)
    asyncio.run(runner.interrupt_active(work_ids=set(replay['interrupted_work_ids'])))
    assert runner.provider.interruptions == ['old']
    assert service.detail(task['id'])['work_items'][1]['state'] == 'pending'


def test_middle_hold_and_redo_preserve_identity_tags_and_later_work(service):
    task, group, session, runner = begin(service)
    service.settings.data['active_profile_id'] = session['config']['profile_id']
    records = [submit(service, session, runner)[0] for _ in range(3)]
    before = service.detail(task['id'])
    held, request = perform(service, runner, records[1]['page_id'], 'hold')
    assert remedy(service, runner, records[1]['page_id'], request) == held
    after = service.detail(task['id'])
    assert [(p['id'],p['seq']) for p in after['pages']] == [(p['id'],p['seq']) for p in before['pages']]
    assert [s['paper_tag'] for s in after['submissions']] == ['001','002','003']
    assert capture.read(service, session['id'])['next']['paper_tag'] == '004'
    assert after['pages'][1]['capture_withheld']
    assert [w['state'] for w in after['work_items']] == ['pending','superseded','pending']
    restored, _ = perform(service, runner, records[1]['page_id'], 'resume', cloud_consent=True)
    assert restored['queue']['queued_page_ids'] == [records[1]['page_id']]
    assert not service.detail(task['id'])['pages'][1]['capture_withheld']
    while asyncio.run(runner.tick()): pass
    assert all(p['state']=='processed' for p in service.detail(task['id'])['pages'])


def test_unconfirmed_photo_invalidates_read_cache_and_discard_releases_no_number(service):
    task, _, session, runner = begin(service)
    before = detail(service, task['id'])
    frame = shot(service, session)
    staged = detail(service, task['id'], before['cache_token'])
    assert staged['capture_drafts'][0]['id'] == frame['id']
    capture.discard_draft(service, frame['id'], {'request_id':uid(),'revision':frame['revision']})
    after = detail(service, task['id'], staged['cache_token'])
    assert after['capture_drafts']==[] and after['pages']==[]
    assert capture.read(service, session['id'])['next']['paper_tag']=='001'
    with pytest.raises(Problem):
        capture.commit(service, frame['id'], {'request_id':uid()},runner.enqueue_capture)


def test_hold_blocked_by_teacher_and_stale_preview(service):
    _, _, session, runner = begin(service)
    first, _ = submit(service, session, runner)
    plan = remedy(service, runner, first['page_id'], {'action':'hold','preview':True})
    with service.store.connection(write=True) as db:
        page=service.store.get(db,'pages',first['page_id'])
        service.store.put(db,'pages',{**page,'overrides':{'q1':{'judgment':'correct'}}})
    with pytest.raises(Problem) as exc:
        remedy(service,runner,first['page_id'],{'action':'hold','fingerprint':plan['fingerprint'],'request_id':uid()})
    assert exc.value.code=='CAPTURE_TEACHER_WORK'


def test_dependency_hold_waits_then_redo_queues_both(service):
    task, _, session, runner = begin(service,2,True)
    service.settings.data['active_profile_id']=session['config']['profile_id']
    first,_=submit(service,session,runner);second,_=submit(service,session,runner)
    result,_=perform(service,runner,second['page_id'],'hold')
    assert result['dependent_pages']==1
    assert not asyncio.run(runner.tick())
    result,_=perform(service,runner,second['page_id'],'resume',cloud_consent=True)
    assert set(result['queue']['queued_page_ids'])=={first['page_id'],second['page_id']}
    assert len(service.detail(task['id'])['pages'])==2


def test_replacement_needs_preparation_and_cloud_consent_and_original_account(service):
    task, _, session, runner = begin(service)
    first,_=submit(service,session,runner)
    asset=shot(service,session)['asset_id']
    result,_=perform(service,runner,first['page_id'],'replace',asset_id=asset)
    page=result['page'];assert page['capture_withheld'] and page['preparation'] is None
    with pytest.raises(Problem) as exc:
        perform(service,runner,page['id'],'resume')
    assert exc.value.code=='CLOUD_CONSENT'
    service.settings.data['active_profile_id']='other-account'
    with pytest.raises(Problem) as exc:
        perform(service,runner,page['id'],'resume',cloud_consent=True)
    assert exc.value.code=='CAPTURE_ACCOUNT_CHANGED'
    service.settings.data['active_profile_id']=session['config']['profile_id']
    with pytest.raises(Problem) as exc:
        perform(service,runner,page['id'],'resume',cloud_consent=True)
    assert exc.value.code=='IMAGE_PREVIEW_REQUIRED'
    preview=service.prepare_page(page['id'],{})
    service.prepare_page(page['id'],{'action':'confirm','stamp':preview['preparation']['stamp'],'revision':preview['revision'],'boundary_ack':True})
    result,_=perform(service,runner,page['id'],'resume',cloud_consent=True)
    assert result['queue']['queued_page_ids']==[page['id']]
    assert len(service.detail(task['id'])['submissions'])==1


def test_batch_does_not_reupload_withheld_photo(service):
    task,group,session,runner=begin(service)
    first,_=submit(service,session,runner)
    perform(service,runner,first['page_id'],'hold')
    with pytest.raises(Problem) as exc:
        runner.create(task['id'],{'request_id':uid(),'group_ids':[group['id']],'purpose':'student','cloud_consent':True,'images_prepared':True},session['config'])
    assert exc.value.code=='CAPTURE_WITHHELD'


@pytest.mark.parametrize('failure',[False,True])
def test_late_return_after_middle_hold_never_overwrites_redo(service,failure):
    task,_,session,runner=begin(service)
    first,_=submit(service,session,runner);submit(service,session,runner)
    from test_workflow import FakeProvider
    class Held(FakeProvider):
        async def infer(self,*args,**kwargs):
            self.started.set();await self.release.wait()
            if failure: raise RuntimeError('late synthetic failure')
            return await super().infer(*args,**kwargs)
    async def execute():
        provider=Held();provider.started=asyncio.Event();provider.release=asyncio.Event();runner.provider=provider
        tick=asyncio.create_task(runner.tick());await provider.started.wait()
        perform(service,runner,first['page_id'],'hold');provider.release.set();await tick
    asyncio.run(execute())
    after=service.detail(task['id'])
    assert after['pages'][0]['capture_withheld'] and after['pages'][0]['observation_id'] is None
    assert after['work_items'][1]['state']=='pending'
    assert all(r['state']!='paused' for r in after['runs'])
