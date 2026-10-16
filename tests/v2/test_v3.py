import asyncio
import hashlib
import io
import json
import numpy as np
import pytest
from PIL import Image, ImageDraw
from autoreview.v2.core import MachineSettings, Problem, uid
from autoreview.v2.service import Service
from autoreview.v2.runner import Runner
from autoreview.v2.grading import grade_observation, resolve_page
from autoreview.v2.imaging import render, project, auto_operations
from autoreview.v2.libraries import merge_library, inspect_library
from autoreview.v2.lifetime import UiLease, parent_alive
from test_workflow import service, setup, intake, images, enqueue, FakeProvider, config, drain


def test_preflight_gate_invalidation_and_original_retention(service):
    task, groups, _ = setup(service, 1)
    intake(service, groups[0], 1)
    runner = Runner(service, FakeProvider())
    data = {'request_id':uid(), 'cloud_consent':True,'group_ids':[groups[0]['id']]}
    with pytest.raises(Problem, match='图片准备'):
        runner.create(task['id'],data,config())
    page = service.detail(task['id'])['pages'][0]
    prepared = service.prepare_page(page['id'], {})
    service.prepare_page(page['id'], {'action':'confirm','revision':prepared['revision'],'stamp':prepared['preparation']['stamp']})
    original = service.asset(page['asset_id'], 'original').read_bytes()
    edited = service.prepare_asset(page['asset_id'], {'operation':{'kind':'rotate','angle':90}})
    assert service.asset(edited['id'],'original').read_bytes() == original
    assert (edited['width'],edited['height']) == (180,240)
    current = service.detail(task['id'])['pages'][0]
    service.replace_page(page['id'],{'revision':current['revision'],'asset_id':edited['id']})
    with pytest.raises(Problem, match='图片准备'):
        runner.create(task['id'],data,config())


def test_geometry_composition_roundtrip_and_colour_preservation():
    image=Image.new('RGB',(300,200),'white');ImageDraw.Draw(image).rectangle((80,80,120,120),fill='red')
    out, geometry=render(image,[{'kind':'rotate','angle':90},{'kind':'rotate','angle':-90}])
    assert np.min(np.array(out)[:,:,1]) < 50  # No red removal.
    points=[[.2,.3],[.7,.8],[.3,.9]]
    assert np.allclose(project(project(points,geometry['forward']),geometry['inverse']),points)
    with pytest.raises(Problem):
        render(image,[{'kind':'perspective','points':[[0,0],[1,1],[1,0],[0,1]]}])


def test_automatic_orientation_never_infers_deskew_from_lines(monkeypatch):
    from autoreview.v2 import orientation
    monkeypatch.setattr(orientation, '_collect_evidence', lambda image: ([], 0))
    image=Image.new('RGB',(1000,700),'white');draw=ImageDraw.Draw(image)
    for y in range(100,600,40):draw.line((100,y,900,y+28),fill='black',width=3)
    ops, log=auto_operations(image)
    assert not ops and log[0]['status']=='needs_review'
    out, geometry=render(image,ops)
    assert geometry['forward'] == np.eye(3).tolist()
    assert np.array_equal(np.array(out),np.array(image))
    ops, log=auto_operations(Image.new('RGB',(100,100),'white'))
    assert not ops and all(e.get('reason') for e in log)


def test_typed_answers_and_conflict_do_not_guess():
    answer={'kind':'marked_choice','response_encoding':'marked_choice','english':'B'}
    assert grade_observation({'selected_options':['B'],'raw_english':''},answer)[0]=='correct'
    assert grade_observation({'selected_options':['B'],'written_options':['A']},answer)[0]=='unknown'
    assert grade_observation({'selected_options':[],'visibility':'not_visible'},answer)[0]=='unknown'
    assert grade_observation({'selected_options':[],'raw_english':''},answer)[0]=='missing'
    assert grade_observation({'symbol':'×','raw_english':''},{'english':'×','response_encoding':'symbol'})[0]=='correct'
    assert grade_observation({'rank':'2','raw_english':''},{'english':'2','response_encoding':'rank'})[0]=='correct'
    assert grade_observation({'raw_english':'I has a cat.'},{'english':'I have a cat.'})[0]=='incorrect'


def test_duplicate_number_never_maps_to_wrong_section():
    ref={'items':[{'id':'a','slot':1,'number':'1','section':'I','english':'A'}, {'id':'b','slot':1,'number':'1','section':'II','english':'B'}]}
    result=resolve_page(ref,{'slot':1},{'cells':[{'slot':1,'number':'1','section':'II','raw_english':'B'}]})
    assert [x['status'] for x in result]==['unknown','correct']


def test_reference_geometry_gate_and_parts(service):
    task=service.create('tasks',{'name':'exam'})
    a=images(service,1)[0]
    items=[{'id':'a','slot':1,'section':'I','number':'1','part_id':part,'english':'cat'} for part in ('a','b')]
    items[1]['id']='b'
    draft=service.draft(task['id'],{'asset_ids':[a],'items':items})
    with pytest.raises(Problem,match='完整答题区'):
        service.confirm_reference(task['id'],draft['revision'])
    draft=service.draft(task['id'],{'items':[{**item,'answer_zone':{'x1':.1,'y1':.1+i*.2,'x2':.4,'y2':.2+i*.2},'geometry_checked':True} for i,item in enumerate(items)]})
    from autoreview.v2.reference_review import review
    with pytest.raises(Problem):  # A client boolean is not a teacher review receipt.
        service.confirm_reference(task['id'],draft['revision'])
    draft=review(service,task['id'],{'revision':draft['revision'],'slot':1})
    assert len(service.confirm_reference(task['id'],draft['revision'])['items'])==2


def test_cross_page_input_frozen_and_answers_absent(service):
    task,groups,ref=setup(service,2)
    draft=service.draft(task['id'],{'items':[{**a,'dependencies':[2] if a['slot']==1 else [],'kind':'fill_blank'} for a in ref['items']]})
    new=service.confirm_reference(task['id'],draft['revision'])
    intake(service,groups[0],2)
    provider=FakeProvider();runner=Runner(service,provider)
    run=enqueue(runner,task,groups[:1],reference_version_id=new['id'],regrade_all=True)
    asyncio.run(drain(runner))
    assert provider.calls[0]['variables']['image_page_order']==[1,2]
    serialized=json.dumps(provider.calls[0]['variables'])
    assert 'apple' not in serialized and 'allowed_edges' not in serialized
    assert len(provider.calls)==2


def test_merge_preserves_reviews_evidence_and_deduplicates(service,tmp_path):
    settings=MachineSettings(tmp_path/'other-machine');settings.save(library_location=str(tmp_path/'other-library'))
    source=Service(settings)
    try:
        task,groups,_=setup(source,1);intake(source,groups[0],1)
        runner=Runner(source,FakeProvider());enqueue(runner,task,groups[:1]);asyncio.run(drain(runner))
        page=source.detail(task['id'])['pages'][0]
        source.review(page['id'],{'revision':page['revision'],'question_id':'q1','change':{'status':'correct'},'category':'transcription'})
        before=hashlib.sha256(source.store.path.read_bytes()).hexdigest()
        result=merge_library(service,source.store.root)
        assert result['source_preserved']
        assert hashlib.sha256(source.store.path.read_bytes()).hexdigest()==before
        assert merge_library(service,source.store.root)['already_imported']
        with service.store.connection() as db:
            pages=service.store.list(db,'pages'); assert len(pages)==1
            assert service.results(db,pages[0])[0]['status']=='correct'
            assets=service.store.list(db,'assets')
            assert all(service.asset(a['id']).is_file() for a in assets)
            assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
    finally:source.close()


def test_ui_lease_refresh_and_multiple_windows():
    clock=[0.];lease=UiLease(clock=lambda:clock[0],startup=30,grace=8)
    assert not lease.expired()
    a,b=lease.join(),lease.join();lease.leave(a);clock[0]=100;assert not lease.expired()
    lease.leave(b);clock[0]=105;assert not lease.expired()
    c=lease.join();clock[0]=200;assert not lease.expired()
    lease.leave(c);clock[0]=209;assert lease.expired()
    import os
    assert parent_alive(os.getpid()) and not parent_alive(99999999)


def test_crop_retry_keeps_other_observations_and_manual_results(service):
    task,groups,ref=setup(service,1)
    first={**ref['items'][0],'answer_zone':dict(x1=.1,y1=.1,x2=.4,y2=.3)}
    second={**first,'id':'q2','number':'2','answer_zone':dict(x1=.5,y1=.1,x2=.9,y2=.3)}
    draft=service.draft(task['id'],{'items':[first,second]})
    new=service.confirm_reference(task['id'],draft['revision'])
    intake(service,groups[0],1);provider=FakeProvider();runner=Runner(service,provider)
    enqueue(runner,task,groups[:1],reference_version_id=new['id'],regrade_all=True);asyncio.run(drain(runner))
    page=service.detail(task['id'])['pages'][0]
    service.review(page['id'],{'revision':page['revision'],'question_id':'q2','change':{'status':'correct'}})
    page=service.detail(task['id'])['pages'][0]
    runner.retry_region(page['id'],{'revision':page['revision'],'request_id':uid(),'cloud_consent':True,'question_ids':[first['id']]},config())
    asyncio.run(drain(runner))
    assert len(provider.calls[-1]['variables']['structure'])==1
    assert provider.calls[-1]['variables']['crop_manifest']['question_ids']==[first['id']]
    page=service.detail(task['id'])['pages'][0]
    assert next(a for a in page['results'] if a['id']=='q2')['status']=='correct'
    assert len(page['results'])==2


def test_batch_rotation_undo_is_atomic_and_stales_preflight(service):
    task,groups,_=setup(service,1);intake(service,groups[0],2)
    pages=service.detail(task['id'])['pages']
    op=service.batch_images({'pages':[{'id':p['id'],'revision':p['revision']} for p in pages],'angle':-90})
    rotated=service.detail(task['id'])['pages']
    assert all(p['preparation'] is None for p in rotated)
    service.batch_images({'undo_id':op['id']})
    undone=service.detail(task['id'])['pages']
    assert [p['asset_id'] for p in undone]==[p['asset_id'] for p in pages]
    assert all(p['generation']>old['generation'] for p,old in zip(undone,pages))
    with pytest.raises(Problem):service.batch_images({'undo_id':op['id']})
