import asyncio
import pytest
from autoreview.v2.core import Problem,uid
from test_workflow import service,images,intake,config
from test_v141_defaults_replacement import prepared


def replaced(service,page):
    plan=service.replace_page(page['id'],{'preview':True})
    page=service.replace_page(page['id'],{'request_id':uid(),'asset_id':images(service,1)[0],
        'revision':page['revision'],'fingerprint':plan['fingerprint'],'replacement_ack':True})
    prep=service.prepare_page(page['id'],{})
    return service.prepare_page(page['id'],{'action':'confirm','boundary_ack':True,'stamp':prep['preparation']['stamp'],'revision':prep['revision']})


def test_replacement_handoff_only_enqueues_selected_page_and_is_idempotent(service):
    task,group,runner,pages=prepared(service,2)
    page=replaced(service,pages[0])
    intake(service,group,1,start_tag='002')  # An unrelated unfinished copy must neither block nor be uploaded.
    request={'request_id':uid(),'purpose':'student','group_ids':[group['id']],
             'page_ids':[page['id']],'cloud_consent':True}
    result=runner.create(task['id'],request,{**config(),'conservative_review':False})
    assert runner.create(task['id'],request,{**config(),'conservative_review':False})==result
    with service.store.connection() as db:
        work=service.store.list(db,'work_items',parent_id=result['runs'][0]['id'])
        assert [w['page_id'] for w in work]==[page['id']]
    while asyncio.run(runner.tick()):pass
    current=service.detail(task['id'])
    assert current['pages'][0]['state']=='processed'
    assert current['pages'][1]['generation']==pages[1]['generation']
    assert current['pages'][2]['state']=='pending'


def test_replacement_handoff_rejects_regular_page_scope(service):
    task,group,runner,pages=prepared(service,2)
    with pytest.raises(Problem) as error:
        runner.create(task['id'],{'request_id':uid(),'group_ids':[group['id']],
            'page_ids':[pages[0]['id']],'cloud_consent':True},config())
    assert error.value.code=='PAGE_SCOPE'


def test_replacement_handoff_still_requires_preparation(service):
    task,group,runner,pages=prepared(service,2)
    plan=service.replace_page(pages[0]['id'],{'preview':True})
    page=service.replace_page(pages[0]['id'],{'asset_id':images(service,1)[0],'revision':plan['revision']})
    with pytest.raises(Problem) as error:
        runner.create(task['id'],{'request_id':uid(),'group_ids':[group['id']],
            'page_ids':[page['id']],'cloud_consent':True},config())
    assert error.value.code=='IMAGE_PREVIEW_REQUIRED'


def test_replaced_page_can_be_checked_while_other_pages_are_queued(service):
    task,group,runner,pages=prepared(service,2)
    # A normal second pass owns both pages; replacing one supersedes its input.
    runner.create(task['id'],{'request_id':uid(),'group_ids':[group['id']],
        'regrade_all':True,'cloud_consent':True},config())
    page=replaced(service,service.detail(task['id'])['pages'][0])
    assert page['preparation']['confirmed']
    other=service.detail(task['id'])['pages'][1]
    with pytest.raises(Problem) as error:
        service.prepare_page(other['id'],{})
    assert error.value.code=='RUN_ACTIVE'
