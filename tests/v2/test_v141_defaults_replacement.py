import asyncio
import json
import pytest
from autoreview.v2.core import MachineSettings, Problem, uid
from autoreview.v2.execution_config import freeze
from autoreview.v2.runner import Runner
from autoreview.v2 import stream_capture as capture
from test_workflow import service, setup, intake, images, config, FakeProvider, enqueue
from test_v130_capture import shot, confirmation


def test_v141_defaults_and_one_time_upgrade_preserve_later_choices(tmp_path):
    home=tmp_path/'machine';home.mkdir()
    (home/'settings.json').write_text(json.dumps({'conservative_review':True,'capture_countdown_seconds':3,
        'parallelism':7,'capture_parallelism':13,'model':'kept-model','auto_orient_images':False}),'utf-8')
    value=MachineSettings(home)
    assert value.data['conservative_review'] is False and value.data['capture_countdown_seconds']==2
    assert (value.data['parallelism'],value.data['capture_parallelism'],value.data['model'],value.data['auto_orient_images'])==(7,13,'kept-model',False)
    value.save(conservative_review=True,capture_countdown_seconds=5)
    reopened=MachineSettings(home)
    assert reopened.data['conservative_review'] is True and reopened.data['capture_countdown_seconds']==5


def test_upgrade_keeps_custom_delay_and_new_defaults_are_two_false(tmp_path):
    current=MachineSettings(tmp_path/'new')
    assert current.data['capture_countdown_seconds']==2 and not freeze(current.data)['conservative_review']
    old=tmp_path/'old';old.mkdir();(old/'settings.json').write_text(json.dumps({'capture_countdown_seconds':9,'conservative_review':False}),'utf-8')
    assert MachineSettings(old).data['capture_countdown_seconds']==9


def test_old_active_capture_configuration_does_not_change_with_new_defaults(service):
    task,groups,_=setup(service,1);service.settings.save(capture_countdown_seconds=3,conservative_review=True)
    session=capture.start(service,task['id'],{'request_id':uid(),'group_id':groups[0]['id'],'cloud_consent':True},freeze(service.settings.data,purpose='capture'))
    service.settings.save(capture_countdown_seconds=2,conservative_review=False)
    frozen=capture.read(service,session['id'])
    assert frozen['capture_countdown_seconds']==3 and frozen['config']['conservative_review'] is True


def prepared(service,n=1,dependencies=False):
    task,groups,ref=setup(service,n)
    if dependencies:
        draft=service.draft(task['id'],{'items':[{**q,'kind':'fill_blank','dependencies':[1] if q['slot']==2 else []} for q in ref['items']]})
        service.confirm_reference(task['id'],draft['revision'])
    intake(service,groups[0],n)
    runner=Runner(service,FakeProvider());enqueue(runner,task,[groups[0]])
    while asyncio.run(runner.tick()):
        pass
    return task,groups[0],runner,service.detail(task['id'])['pages']


def test_replacement_preview_is_readonly_and_confirm_is_idempotent(service):
    task,group,runner,pages=prepared(service)
    page=pages[0];before=service.detail(task['id'])
    preview=service.replace_page(page['id'],{'preview':True})
    assert service.detail(task['id'])==before
    asset=images(service,1)[0]
    request={'request_id':uid(),'asset_id':asset,'revision':page['revision'],'fingerprint':preview['fingerprint'],'replacement_ack':True}
    changed=service.replace_page(page['id'],request)
    assert service.replace_page(page['id'],request)==changed
    assert changed['generation']==page['generation']+1 and not changed['reviewed'] and changed['preparation'] is None
    final=service.detail(task['id'])
    assert final['submissions'][0]['paper_tag']=='001' and final['pages'][0]['slot']==1
    assert len(final['pages'])==len(final['submissions'])==1


def test_replacement_preserves_teacher_state_in_immutable_history(service):
    task,_,runner,pages=prepared(service)
    page=service.review(pages[0]['id'],{'revision':pages[0]['revision'],'question_id':'q1','change':{'status':'correct','raw_english':'teacher correction'}})
    page=service.review(page['id'],{'revision':page['revision'],'action':'confirm'})
    preview=service.replace_page(page['id'],{'preview':True})
    assert preview['reviewed_pages']==1 and preview['teacher_modified_pages']==1
    request={'request_id':uid(),'asset_id':images(service,1)[0],'revision':page['revision'],'fingerprint':preview['fingerprint'],'replacement_ack':True}
    result=service.replace_page(page['id'],request)
    assert not result['overrides'] and not result['reviewed'] and not result['annotations']
    with service.store.connection() as db:
        revision=next(r for r in service.store.list(db,'review_revisions',parent_id=page['id']) if r.get('kind')=='image_replacement')
        assert revision['snapshot']['overrides']==page['overrides'] and revision['snapshot']['reviewed']


def test_replacement_rejects_teacher_change_since_preview(service):
    task,_,runner,pages=prepared(service)
    preview=service.replace_page(pages[0]['id'],{'preview':True})
    current=service.review(pages[0]['id'],{'revision':pages[0]['revision'],'question_id':'q1','change':{'status':'correct'}})
    with pytest.raises(Problem) as error:
        service.replace_page(current['id'],{'request_id':uid(),'asset_id':images(service,1)[0],'revision':current['revision'],
            'fingerprint':preview['fingerprint'],'replacement_ack':True})
    assert error.value.code=='REPLACEMENT_CHANGED'
    assert service.detail(task['id'])['pages'][0]['overrides']==current['overrides']


def test_replacement_requires_explicit_check_and_fences_dependent_results(service):
    task,_,runner,pages=prepared(service,2,True)
    preview=service.replace_page(pages[0]['id'],{'preview':True})
    assert preview['dependent_pages']==1
    request={'request_id':uid(),'asset_id':images(service,1)[0],'revision':pages[0]['revision'],'fingerprint':preview['fingerprint']}
    with pytest.raises(Problem) as error:
        service.replace_page(pages[0]['id'],request)
    assert error.value.code=='REPLACEMENT_ACK'
    result=service.replace_page(pages[0]['id'],{**request,'replacement_ack':True})
    final=service.detail(task['id'])
    assert all(p['state']=='pending' and p['generation']==old['generation']+1 and p['observation_id'] is None for p,old in zip(final['pages'],pages))
    assert final['pages'][1]['image_version_id']==pages[1]['image_version_id']
    assert set(result['replacement_affected_page_ids'])=={p['id'] for p in pages}


def test_captured_page_replacement_remains_withheld_until_checked_and_submitted(service):
    task,groups,_=setup(service,1);runner=Runner(service,FakeProvider())
    session=capture.start(service,task['id'],{'request_id':uid(),'group_id':groups[0]['id'],'cloud_consent':True},config())
    frame=shot(service,session);saved=capture.commit(service,frame['id'],confirmation(frame),runner.enqueue_capture)
    page=service.detail(task['id'])['pages'][0];preview=service.replace_page(page['id'],{'preview':True})
    result=service.replace_page(page['id'],{'request_id':uid(),'asset_id':images(service,1)[0],'revision':page['revision'],
        'fingerprint':preview['fingerprint'],'replacement_ack':True})
    assert result['capture_withheld'] and result['capture_session_id']==session['id']
    assert result['interrupted_work_ids'] and not asyncio.run(runner.tick())
    assert runner.provider.calls==[]
