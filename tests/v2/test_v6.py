import copy,json
from pathlib import Path
import pytest
from autoreview.v2.core import Problem,uid,Store
from autoreview.v2.reference_review import review,issues,fingerprint,required_slots
from autoreview.v2.reference_pack import export_pack,import_pack
from autoreview.v2.lifetime import UiLease
from test_workflow import service,images
from test_v5 import reference


def test_save_and_review_are_idempotent_with_conflict_protection(service):
    _,t,d=reference(service)
    body={'revision':d['revision'],'items':d['items'],'request_id':uid()}
    first=service.draft(t['id'],body)
    assert service.draft(t['id'],body)==first
    with pytest.raises(Problem): service.draft(t['id'],{**body,'items':[]})
    request={'revision':first['revision'],'slot':1,'request_id':uid()}
    confirmed=review(service,t['id'],request)
    assert review(service,t['id'],request)==confirmed
    assert service.draft(t['id'])['revision']==confirmed['revision']
    with service.store.connection() as db:
        assert all(len(r[0])<300 for r in db.execute('SELECT response FROM idempotency'))


def test_revoke_allowed_even_with_unrelated_validation_error(service):
    _,t,d=reference(service)
    d=review(service,t['id'],{'revision':d['revision'],'slot':1})
    d['items'][0]['uncertain']=True
    d=service.draft(t['id'],{'revision':d['revision'],'items':d['items']})
    d=review(service,t['id'],{'revision':d['revision'],'slot':1,'revoke':True})
    assert not d['items'][0]['geometry_checked']


@pytest.mark.parametrize('kind,encoding',[('choice','mixed'),('letter_choice','written_choice'),('marked_choice','marked_choice')])
def test_legacy_choice_validator_matches_final_gate(service,kind,encoding):
    _,t,d=reference(service)
    d['items'][0].update(kind=kind,response_encoding=encoding,english='A',options=[{'id':'A','box':{'x1':.1,'y1':.1,'x2':.2,'y2':.2}},{'id':'B'}])
    d=service.draft(t['id'],{'items':d['items']})
    error=next(i for i in issues(d) if i['code']=='OPTIONS_INCOMPLETE')
    assert error['field_path']=='options[B].box' and error['region_target']['id']=='B'
    with pytest.raises(Problem) as e: review(service,t['id'],{'revision':d['revision'],'slot':1})
    assert error['id'] in [i['id'] for i in e.value.details['issues']]
    d['items'][0]['options'][1]['box']={'x1':.3,'y1':.1,'x2':.4,'y2':.2}
    d=service.draft(t['id'],{'items':d['items']})
    for slot in (1,2):d=review(service,t['id'],{'revision':d['revision'],'slot':slot})
    assert service.confirm_reference(t['id'],d['revision'])['state']=='confirmed'


def cross_reference(service):
    _,t,d=reference(service)
    a=d['items'][0];a.update(kind='choice',response_encoding='mixed',english='A',options=[{'id':'A','box':a['answer_zone'],'page_id':d['page_ids'][0],'region_id':uid()},{'id':'B','box':a['answer_zone'],'page_id':d['page_ids'][1],'region_id':uid()}])
    return t,service.draft(t['id'],{'items':[a]})


def test_cross_page_option_requires_both_pages_and_selective_revoke(service):
    t,d=cross_reference(service)
    assert required_slots(d['items'][0],d)=={1,2}
    d=review(service,t['id'],{'revision':d['revision'],'slot':1})
    assert d['reviewed_pages']['q1']==[1] and not d['items'][0]['geometry_checked']
    d=review(service,t['id'],{'revision':d['revision'],'page_id':d['page_ids'][1]})
    assert d['items'][0]['geometry_checked']
    d=review(service,t['id'],{'revision':d['revision'],'slot':2,'revoke':True})
    assert d['reviewed_pages']['q1']==[1] and not d['items'][0]['geometry_checked']


def test_page_reorder_preserves_review_by_identity(service):
    _,t,d=reference(service)
    for slot in (1,2):d=review(service,t['id'],{'revision':d['revision'],'slot':slot})
    for a in d['items']:a['slot']=3-a['slot']
    d=service.draft(t['id'],{'items':d['items'],'asset_ids':d['asset_ids'][::-1],'page_ids':d['page_ids'][::-1]})
    assert all(a['geometry_checked'] for a in d['items'])
    assert d['reviewed_pages']=={'q1':[2],'q2':[1]}


def test_option_page_swap_invalidates_when_dependency_set_unchanged(service):
    t,d=cross_reference(service)
    for slot in (1,2):d=review(service,t['id'],{'revision':d['revision'],'slot':slot})
    d['items'][0]['options'].reverse()
    for o in d['items'][0]['options']:o['page_id']=next(p for p in d['page_ids'] if p!=o['page_id'])
    d=service.draft(t['id'],{'items':d['items']})
    assert not d['items'][0]['geometry_checked']


def test_cross_page_pack_remaps_region_ids_and_keeps_truth(service):
    t,d=cross_reference(service)
    d=service.draft(t['id'],{'review_provenance':{'baseline':{'q1':json.dumps(d['items'][0],ensure_ascii=False,separators=(',',':'))}}})
    content=Path(export_pack(service,t['id'])['path']).read_bytes()
    target=service.create('tasks',{'name':'重新导入'})
    result=import_pack(service,target['id'],content)
    a=result['items'][0]
    assert a['english']=='A' and not a['geometry_checked']
    assert a['options'][1]['page_id']==result['page_ids'][1]
    assert a['options'][1]['region_id']!=d['items'][0]['options'][1]['region_id']
    assert json.loads(result['review_provenance']['baseline'][a['id']])['options'][1]['page_id']==result['page_ids'][1]


def test_unchanged_detail_does_not_transfer_history(service):
    _,t,d=reference(service)
    d=service.draft(t['id'],{'edit_history':[{'graph':{'items':d['items']}}]*75})
    full=service.detail(t['id']);slim=service.detail(t['id'],d['revision'])
    assert slim['draft_unchanged'] and slim['draft'] is None
    assert len(json.dumps(slim)) < len(json.dumps(full))/2


def test_schema_upgrade_saves_snapshot_and_old_writer_guard(tmp_path):
    store=Store(tmp_path/'library');manifest=store.root/'library.json';store.close()
    old=json.loads(manifest.read_text('utf-8'));old['schema_version']=3;manifest.write_text(json.dumps(old),'utf-8')
    new=Store(tmp_path/'library')
    assert new.manifest['schema_version']==5 and new.manifest['minimum_writer']=='0.7'
    assert list((new.root/'migration-history').glob('schema-3-*/autoreview.sqlite'))
    new.close()


def test_http_activity_survives_lost_socket_until_activity_stops():
    clock=[0.];l=UiLease(clock=lambda:clock[0]);c=l.join('tab1');l.leave(c,'ui_connection_lost')
    clock[0]=100.;l.touch('tab1');assert not l.expired()
    clock[0]=205.;assert not l.expired()
    clock[0]=211.;assert l.expired()


def test_old_connection_close_cannot_remove_reconnected_tab():
    clock=[0.];l=UiLease(clock=lambda:clock[0]);old=l.join('tab');new=l.join('tab');l.leave(old)
    assert not l.expired() and l.recently_active(new)
    l.leave(new);clock[0]=9.;assert l.expired()


def test_history_pruning_preserves_audit_and_freeze_is_idempotent(service):
    _,t,d=reference(service)
    frames=[{'name':'修改'+str(i),'graph':{'items':copy.deepcopy(d['items'])}} for i in range(55)]
    d=service.draft(t['id'],{'edit_history':frames})
    d=service.draft(t['id'],{'edit_history':frames[-50:]})
    assert 'edit_history_archive' not in d
    with service.store.connection() as db: assert len(service.store.get(db,'reference_drafts',t['id'])['edit_history_archive'])==5
    for slot in (1,2):d=review(service,t['id'],{'revision':d['revision'],'slot':slot})
    version=service.confirm_reference(t['id'],d['revision'])
    assert len(version['edit_history_archive'])==5
    assert service.confirm_reference(t['id'],d['revision'])['id']==version['id']
