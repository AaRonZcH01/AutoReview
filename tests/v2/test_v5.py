import io
import json
import zipfile
from pathlib import Path
import pytest
from PIL import Image
from autoreview.v2.core import MachineSettings, Store, Problem, uid
from autoreview.v2.service import Service
from autoreview.v2.reference_review import review, issues
from autoreview.v2.resources import Resources, archive_folder
from autoreview.v2.reference_pack import export_pack, import_pack
from autoreview.v2.libraries import merge_library
from autoreview.v2.grading import grade_observation
from autoreview.v2.contracts import prompt_record, validate_output_schema
from autoreview.v2.contracts_v5 import decode_output
from test_workflow import service, setup, images


def reference(service):
    folder=service.create('folders',{'name':'Unit 2'})
    task=service.create('tasks',{'name':'练习','parent_id':folder['id']})
    assets=images(service,2)
    draft=service.draft(task['id'],{'asset_ids':assets,'page_ids':[uid(),uid()], 'items':[
        {'id':'q1','slot':1,'section':'一','number':'1','english':'dog','answer_zone':{'x1':.1,'y1':.1,'x2':.3,'y2':.3}},
        {'id':'q2','slot':2,'section':'一','number':'1','english':'cat','answer_zone':{'x1':.1,'y1':.1,'x2':.3,'y2':.3}}]})
    return folder,task,draft


def test_page_receipts_are_server_owned_and_selectively_invalidated(service):
    folder,task,draft=reference(service)
    spoof=[{**a,'geometry_checked':True} for a in draft['items']]
    draft=service.draft(task['id'],{'items':spoof,'review_receipts':{'q1':{'fingerprint':'spoof'}}})
    with pytest.raises(Problem): service.confirm_reference(task['id'],draft['revision'])
    for slot in (1,2): draft=review(service,task['id'],{'revision':draft['revision'],'slot':slot})
    assert all(a['geometry_checked'] for a in draft['items'])
    service.confirm_reference(task['id'],draft['revision'])
    draft=service.draft(task['id'])
    assert all(a['geometry_checked'] for a in draft['items'])
    draft['items'][0]['english']='dogs'
    draft=service.draft(task['id'],{'revision':draft['revision'],'items':draft['items']})
    assert [a['geometry_checked'] for a in draft['items']]==[False,True]
    with pytest.raises(Problem): review(service,task['id'],{'revision':draft['revision']-1,'slot':1})


def test_bulk_review_lists_all_structural_blockers(service):
    _,task,d=reference(service)
    items=d['items'];items[1].update(slot=1,number='1',answer_zone=None,uncertain=True)
    d=service.draft(task['id'],{'items':items})
    with pytest.raises(Problem) as error: review(service,task['id'],{'revision':d['revision'],'slot':1})
    codes={i['code'] for i in error.value.details['issues']}
    assert {'REFERENCE_DUPLICATE','REFERENCE_UNCERTAIN','REFERENCE_GEOMETRY'}<=codes
    assert not service.draft(task['id']).get('review_receipts')


def test_choice_semantics_do_not_depend_on_ink_encoding():
    a={'kind':'choice','english':'B','selection_rule':'single','response_encoding':'written_choice','options':[{'id':'A'},{'id':'B'}]}
    assert grade_observation({'raw_english':'','selected_options':['B']},a)[0]=='correct'
    assert grade_observation({'raw_english':'B','written_options':['B']},{**a,'response_encoding':'marked_choice'})[0]=='correct'
    assert grade_observation({'raw_english':'B','written_options':['B'],'selected_options':['A']},a)[0]=='unknown'
    assert grade_observation({'raw_english':'','selected_options':[]},a)[0]=='missing'
    a['answer_spec']={'accepted':['A']}
    assert grade_observation({'raw_english':'B','written_options':['B']},a)[0]=='correct'


def test_new_schema_is_compact_and_old_turns_still_decode():
    for purpose in ('reference','student','writing'): validate_output_schema(prompt_record(purpose)['schema'])
    current=prompt_record('student')
    cell_name=current['schema']['properties']['cells']['items']['$ref'].split('/')[-1]
    assert 'number' not in current['schema']['$defs'][cell_name]['properties']
    old=decode_output('student','4.0',json.dumps({'cells':[{'slot':1,'number':'1','raw_english':'aple','uncertain':False}]}))
    assert old['cells'][0]['raw_english']=='aple'


def test_reference_pack_roundtrip_is_new_unconfirmed_draft(service):
    _,task,d=reference(service)
    for slot in (1,2): d=review(service,task['id'],{'revision':d['revision'],'slot':slot})
    service.confirm_reference(task['id'],d['revision'])
    result=export_pack(service,task['id']);content=Path(result['path']).read_bytes()
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        assert 'previews/page-1.png' in z.namelist()
        assert json.loads(z.read('reference.json'))['student_data_included'] is False
    target=service.create('tasks',{'name':'复用'})
    imported=import_pack(service,target['id'],content)
    assert imported['state']=='draft' and not any(a['geometry_checked'] for a in imported['items'])
    assert {a['id'] for a in imported['items']}.isdisjoint({'q1','q2'})
    assert imported['asset_ids']!=d['asset_ids']
    assert service.asset(imported['asset_ids'][0]).read_bytes()==service.asset(d['asset_ids'][0]).read_bytes()
    with service.store.connection() as db: assert not service.store.list(db,'reference_versions',task_id=target['id'])


def test_reference_pack_rejects_tampering_without_changing_draft(service):
    _,task,d=reference(service);result=export_pack(service,task['id'])
    source=zipfile.ZipFile(result['path']);stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as z:
        for name in source.namelist(): z.writestr(name,b'{}' if name=='reference.json' else source.read(name))
    with pytest.raises(Problem): import_pack(service,task['id'],stream.getvalue())
    assert service.draft(task['id'])['revision']==d['revision']


def test_archive_compresses_is_readable_and_restores(service):
    folder,task,d=reference(service);asset=d['asset_ids'][0]
    before=service.asset(asset).read_bytes()
    job=archive_folder(service,folder['id'])
    assert job['file_count']==6 and job['compressed_bytes']>0
    assert not (service.store.root/f'assets/{asset}/display.png').exists()
    assert service.asset(asset).read_bytes()==before
    with pytest.raises(Problem,match='归档'): service.draft(task['id'],{'items':[]})
    assert service.detail(task['id'])['draft']['items']
    archive_folder(service,folder['id'],True)
    assert (service.store.root/f'assets/{asset}/display.png').read_bytes()==before
    assert not list((service.store.root/'archives').glob('*.zip'))


def test_archived_resources_merge_and_purge_all_copies(service,tmp_path):
    folder,task,d=reference(service);archive_folder(service,folder['id'])
    other_settings=MachineSettings(tmp_path/'other-machine');other_settings.save(library_location=str(tmp_path/'other-library'))
    other=Service(other_settings)
    try:
        merged=merge_library(other,service.store.root)
        assert merged['tasks']==1
        with other.store.connection() as db: imported=other.store.list(db,'assets')
        assert all(other.asset(a['id']).is_file() for a in imported)
    finally: other.close()
    # Move archived folder to trash: expiry or explicit purge removes the cold bytes too.
    with service.store.connection() as db: current=service.store.get(db,'folders',folder['id'])
    service.trash.move('folders',folder['id'],current['revision'])
    entry=service.trash.rows()['items'][0]
    service.trash.purge(entry['id'])
    with service.store.connection() as db: assert not service.store.list(db,'resources')
    assert not list((service.store.root/'archives').glob('*.zip'))


def test_small_local_crop_is_valid_and_preserves_original(service):
    aid=images(service,1)[0];original=service.asset(aid,'original').read_bytes()
    cropped=service.prepare_asset(aid,{'operation':{'kind':'crop','box':{'x1':.1,'y1':.1,'x2':.2,'y2':.2}}})
    assert cropped['width']<240 and cropped['height']<180
    assert service.asset(cropped['id'],'original').read_bytes()==original

def test_cross_page_review_requires_each_dependency_and_blank_page_declaration(service):
    _,task,d=reference(service)
    d['items']=d['items'][:1];d['items'][0]['dependencies']=[2]
    d=service.draft(task['id'],{'items':d['items']})
    d=review(service,task['id'],{'revision':d['revision'],'slot':1})
    assert not d['items'][0]['geometry_checked']
    d=review(service,task['id'],{'revision':d['revision'],'slot':2})
    assert d['items'][0]['geometry_checked']
    d['items'][0]['dependencies']=[]
    d=service.draft(task['id'],{'items':d['items']})
    assert any(v['code']=='PAGE_UNCHECKED' for v in issues(d))
    d=review(service,task['id'],{'revision':d['revision'],'slot':2,'no_answers':True})
    assert not any(v['code']=='PAGE_UNCHECKED' for v in issues(d))
    d=service.draft(task['id'],{'asset_ids':[d['asset_ids'][0],images(service,1)[0]]})
    assert any(v['code']=='PAGE_UNCHECKED' for v in issues(d))


def test_pack_preview_is_readonly(service):
    _,task,d=reference(service)
    content=Path(export_pack(service,task['id'])['path']).read_bytes()
    preview=import_pack(service,task['id'],content,True)
    assert preview['page_count']==2 and preview['item_count']==2
    assert service.draft(task['id'])['revision']==d['revision']


def test_cold_archive_survives_backup_restore(tmp_path):
    from fastapi.testclient import TestClient
    from autoreview.v2.api import create_app
    from test_workflow import FakeProvider
    settings=MachineSettings(tmp_path/'machine');settings.save(library_location=str(tmp_path/'library'),export_directory=str(tmp_path/'exports'))
    app=create_app(settings,FakeProvider(),nonce='test',start_worker=False)
    with TestClient(app) as client:
        client.headers['X-AutoReview-Session']='test'
        s=app.state.service;folder,task,d=reference(s);aid=d['asset_ids'][0];before=s.asset(aid).read_bytes()
        archive_folder(s,folder['id'])
        response=client.post('/api/library/backup',json={});assert response.status_code==200,response.text
        target=tmp_path/'restored'
        response=client.post('/api/library/restore',json={'archive':response.json()['path'],'path':str(target)})
        assert response.status_code==200,response.text
    restored=Service(settings)
    try:
        assert restored.asset(aid).read_bytes()==before
        with restored.store.connection() as db: assert restored.store.get(db,'folders',folder['id'])['state']=='archived'
        archive_folder(restored,folder['id'],True)
        assert restored.asset(aid).read_bytes()==before
    finally:restored.close()

def test_outside_projection_requires_explicit_page_review(service,monkeypatch):
    from test_workflow import intake
    _,task,d=reference(service)
    for slot in (1,2):d=review(service,task['id'],{'revision':d['revision'],'slot':slot})
    service.confirm_reference(task['id'],d['revision'])
    group=service.create('groups',{'name':'一班','parent_id':task['id']})
    intake(service,group,2)
    monkeypatch.setattr('autoreview.v2.imaging.align',lambda *a,**k:{'matrix':[[1,0,-.2],[0,1,0],[0,0,1]],'status':'aligned','reason':'test'})
    p=service.prepare_page(service.detail(task['id'])['pages'][0]['id'],{})
    assert p['preparation']['zones']['q1']['x1']==0
    assert p['preparation']['boundary_warnings'][0]['projected_box']['x1']<0
    data={'action':'confirm','revision':p['revision'],'stamp':p['preparation']['stamp']}
    with pytest.raises(Problem,match='边缘'):service.prepare_page(p['id'],data)
    assert service.prepare_page(p['id'],{**data,'boundary_ack':True})['preparation']['confirmed']

def test_missing_response_annotation_uses_confirmed_student_geometry():
    from autoreview.v2.grading import resolve_page
    from autoreview.v2.contracts import Answer
    a=Answer.model_validate({'id':'q','slot':1,'number':'1','english':'dog','box':{'x1':.1,'y1':.1,'x2':.2,'y2':.2}}).model_dump()
    student_box={'x1':.5,'y1':.5,'x2':.7,'y2':.7}
    page={'slot':1,'preparation':{'zones':{'q':student_box}}}
    observation={'cells':[{'question_id':'q','raw_english':'','uncertain':False,'box':None}]}
    r=resolve_page({'items':[a],'asset_ids':['template']},page,observation)[0]
    assert r['status']=='missing' and r['raw_english']=='' and r['box']==student_box
