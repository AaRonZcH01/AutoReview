import asyncio
import json
import pytest
from autoreview.v2.core import Problem, uid
from autoreview.v2.scoring import minor, evaluate, manage, structure, validate
from autoreview.v2.reference_versions import manage as versions
from autoreview.v2.preparation_batch import batch
from autoreview.v2.submission_review import review
from autoreview.v2.contracts import strict_schema, MatchingRule, LocalMatchingRule, Answer
from autoreview.v2.contracts_v5 import ReferenceOutputV5, decode_output
from autoreview.v2.contracts_v7 import ReferenceOutputV7
from autoreview.v2.power import PowerState
from test_workflow import service, setup, intake, FakeProvider, Runner, enqueue, drain


def policy(service, task, points=2):
    manage(service,task['id'],{'action':'toggle','enabled':True})
    draft=manage(service,task['id'])['draft']
    units=[{**u,'max_points_minor':minor(points)} for u in draft['units']]
    result=manage(service,task['id'],{'action':'confirm','revision':draft['revision'],'units':units,'declared_total_minor':minor(points)*len(units)})
    publish={'action':'publish','revision':result['draft']['revision'],'scope':'all'}
    preview=manage(service,task['id'],publish)
    manage(service,task['id'],{**publish,'preview':False,'fingerprint':preview['fingerprint']})
    return manage(service,task['id'])


def test_points_are_exact_and_missing_is_not_zero():
    assert minor('0.29')==29
    for value in (None,'NaN','Infinity',-.1,'1.001'):
        with pytest.raises(Problem): minor(value)


def test_unknown_score_interval_and_reversible_deductions():
    p={'declared_total_minor':300,'units':[{'id':str(i),'question_id':str(i),'rule':'all_correct','max_points_minor':100} for i in range(3)]}
    results=[{'id':str(i),'status':v} for i,v in enumerate(('correct','incorrect','unknown'))]
    a=evaluate(p,results,True)
    assert a['range_minor']==[100,200] and a['final_minor'] is None and a['deducted_minor']==100
    results[1]['status']='correct';results[2]['status']='correct'
    assert evaluate(p,results,True)['final_minor']==300
    results[1]['status']='incorrect'
    assert evaluate(p,results,True)['final_minor']==200
    assert evaluate(p,results,True,photo_pending=True)['pending_minor']==300


def test_matching_wrong_and_missing_deduct_once_per_source():
    p={'declared_total_minor':200,'units':[{'id':s,'question_id':'q','rule':'matching_source','source_id':s,'max_points_minor':100} for s in ('1','2')]}
    a={'id':'q','status':'incorrect','grading_detail':{'correct_edges':[['2','B']],'incorrect_edges':[['1','B']],'missing_edges':[['1','A']]}}
    r=evaluate(p,[a],True)
    assert r['final_minor']==100 and r['deducted_minor']==100
    a['manual_status']=True
    assert evaluate(p,[a],True)['pending_minor']==200
    a['score_unit_status']={'1':'correct','2':'correct'}
    assert evaluate(p,[a],True)['final_minor']==200


def test_empty_editing_not_publish_and_page_scope():
    from autoreview.v2.reference_review import issues
    d={'items':[],'asset_ids':[],'page_count':1,'validation_context':'editing'}
    assert issues(d)==[]
    assert issues({**d,'validation_context':'publish'})[0]['scope']=='task'
    i=issues({**d,'asset_ids':['a'],'parse_attempt_id':'attempt'})[0]
    assert i['scope']=='page' and i['item_id'] is None and i['action']=='view_page'


def test_local_display_id_does_not_mutate_frozen_cloud_schema():
    old=strict_schema(ReferenceOutputV5)
    assert 'display_id' not in old['$defs']['MatchingNode']['properties']
    assert 'scoring' in strict_schema(ReferenceOutputV7)['properties']
    node={'id':'n','group':'left','slot':1,'display_id':'1'}
    answer=Answer.model_validate({'id':'q','slot':1,'number':'1','kind':'matching','matching_rule':{'nodes':[node,dict(node,id='b',group='right',display_id='B')],'allowed_edges':[['n','b']]}})
    assert answer.matching_rule.nodes[0].display_id=='1'


def test_scoring_gate_and_immutable_rule_history(service):
    task,groups,ref=setup(service,1)
    manage(service,task['id'],{'action':'toggle','enabled':True})
    with pytest.raises(Problem,match='总分'): intake(service,groups[0],1)
    result=policy(service,task)
    intake(service,groups[0],1)
    assert len(result['versions'])==1
    with service.store.connection(write=True) as db:
        with pytest.raises(Problem): service.store.put(db,'scoring_versions',result['versions'][0])


def test_rule_change_invalidates_only_modified_confirmation(service):
    task,_,_=setup(service,2)
    p=policy(service,task);draft=p['draft'];units=draft['units']
    unchanged=units[1]['confirmed_hash'];units[0]['max_points_minor']=150
    r=manage(service,task['id'],{'revision':draft['revision'],'units':units,'declared_total_minor':350})
    assert r['draft']['units'][1]['confirmed_hash']==unchanged
    assert any(i.get('unit_id')==units[0]['id'] for i in r['issues'])
    assert not any(i.get('unit_id')==units[1]['id'] for i in r['issues'])


def test_official_switch_preserves_draft_and_results(service):
    task,groups,first=setup(service,1)
    d=service.draft(task['id'],{'items':[{'id':'q1','number':'1','slot':1,'english':'pear'}]})
    second=service.confirm_reference(task['id'],d['revision'])
    assert service.detail(task['id'])['groups'][0]['reference_version_id']==second['id']
    preview=versions(service,task['id'],first['id'],{'action':'preview'})
    versions(service,task['id'],first['id'],{'action':'activate','fingerprint':preview['fingerprint'],'confirmed':True})
    d=service.draft(task['id'])
    assert d['items'][0]['english']=='apple' and d['replaced_drafts'][-1]['items'][0]['english']=='pear'
    with pytest.raises(Problem): versions(service,task['id'],first['id'],{'action':'delete','confirmed':True})


def test_batch_preview_lists_missing_geometry_without_silent_skips(service):
    task,groups,_=setup(service,1);intake(service,groups[0],2)
    pages=service.detail(task['id'])['pages']
    service.prepare_page(pages[0]['id'],{})
    pages=service.detail(task['id'])['pages']
    preview=batch(service,task['id'],{'pages':[{'id':p['id'],'revision':p['revision']} for p in pages]})
    assert len(preview['eligible'])==1 and len(preview['blocked'])==1
    result=batch(service,task['id'],{'action':'confirm','preview':preview,'request_id':uid()})
    assert len(result['confirmed'])==1 and len(result['blocked'])==1


def test_submission_undo_redo_scores_and_idempotency(service):
    task,groups,_=setup(service,2);policy(service,task);intake(service,groups[0],2)
    runner=Runner(service,FakeProvider());enqueue(runner,task,groups[:1]);asyncio.run(drain(runner))
    detail=service.detail(task['id']);sub=detail['submissions'][0];pages=detail['pages']
    def apply(p,**kw):
        latest=service.detail(task['id']);s=latest['submissions'][0];page=next(v for v in latest['pages'] if v['id']==p['id'])
        return review(service,s['id'],{'base_revision':s['revision'],'page_id':p['id'],'revision':page['revision'],'request_id':uid(),**kw})
    apply(pages[0],question_id='q1',change={'status':'correct'})
    result=apply(pages[1],question_id='q2',change={'status':'correct'})
    with service.store.connection() as db:
        score=service.store.get(db,'score_snapshots',result['submission']['score_snapshot_id'])
        assert score['earned_minor']==400
    result=apply(pages[0],action='undo')
    assert result['page']['id']==pages[1]['id']
    with service.store.connection() as db:
        assert service.store.get(db,'score_snapshots',result['submission']['score_snapshot_id'])['earned_minor']==200
    result=apply(pages[0],action='redo')
    with service.store.connection() as db:
        assert service.store.get(db,'score_snapshots',result['submission']['score_snapshot_id'])['earned_minor']==400


def test_awake_resume_grace_uses_injected_clock_not_physical_sleep():
    clock=[100.0];power=PowerState(lambda:clock[0]);power.event(4)
    assert power.protected()
    power.event(18);assert power.epoch==1 and power.protected()
    clock[0]+=119;assert power.protected()
    clock[0]+=2;assert not power.protected()


def test_single_score_confirmation_does_not_require_other_unfilled_units(service):
    task,_,_=setup(service,2)
    d=manage(service,task['id'])['draft'];units=d['units'];units[0]['max_points_minor']=125
    r=manage(service,task['id'],{'action':'confirm','revision':d['revision'],'units':units,'unit_ids':[units[0]['id']]})['draft']
    assert r['units'][0]['confirmed_hash'] and not r['units'][1].get('confirmed_hash')
    assert not r.get('total_confirmed_hash')


def test_candidate_adoption_is_structure_bound(service):
    task,_,_=setup(service,1);d=manage(service,task['id'])['draft']
    with service.store.connection(write=True) as db:
        d=service.store.put(db,'scoring_drafts',{**d,'machine_candidate':{'units':d['units'],'structure_hash':'obsolete','declared_total_minor':100}})
    with pytest.raises(Problem,match='当前题目结构'):
        manage(service,task['id'],{'action':'save','revision':d['revision'],'adopt_candidate':True})


def test_sequence_coverage_and_writing_policy():
    a={'id':'q','kind':'ordering','english':'2,1,3'}
    d={'units':[{'id':'u','question_id':'q','rule':'sequence_position','position':0,'max_points_minor':100}],'declared_total_minor':100,'structure_hash':structure([a])}
    assert any('每个实际位置' in p['message'] for p in validate(d,[a],False))
    p={'declared_total_minor':100,'units':[{'id':'u','question_id':'q','rule':'all_correct','max_points_minor':100,'writing_error_policy':'incorrect'}]}
    assert evaluate(p,[{'id':'q','status':'incorrect','writing_status':'confirmed_error'}],True)['deducted_minor']==100
    assert evaluate(p,[{'id':'q','status':'correct','writing_status':'confirmed_error'}],True)['deducted_minor']==100


def test_unreferenced_version_recycle_and_restore(service):
    from autoreview.v2.reference_versions import catalogue,sweep
    task,_,ref=setup(service,1)
    with service.store.connection(write=True) as db:
        clone=service.store.put(db,'reference_versions',{**ref,'id':uid(),'version':42})
    preview=versions(service,task['id'],clone['id'],{'action':'delete_preview'})
    assert preview['reference_count']==0
    deleted=versions(service,task['id'],clone['id'],{'action':'delete','confirmed':True})
    assert deleted['delete_after']
    versions(service,task['id'],clone['id'],{'action':'restore'})
    with service.store.connection() as db: assert any(v['id']==clone['id'] for v in catalogue(service.store,db,task['id']))
    versions(service,task['id'],clone['id'],{'action':'delete','confirmed':True})
    with service.store.connection(write=True) as db:
        m=service.store.get(db,'reference_metadata',clone['id']);service.store.put(db,'reference_metadata',{**m,'delete_after':'2000-01-01T00:00:00+00:00'})
    sweep(service)
    with service.store.connection() as db: assert service.store.get(db,'reference_versions',clone['id'],optional=True) is None


def test_reference_pack_keeps_scoring_but_clears_transfer_confirmation(service):
    from pathlib import Path
    from autoreview.v2.reference_pack import export_pack,import_pack
    task,_,_=setup(service,1);policy(service,task)
    bundle=export_pack(service,task['id'])
    target=service.create('tasks',{'name':'导入赋分'})
    target_id=target['task']['id'] if 'task' in target else target['id']
    import_pack(service,target_id,Path(bundle['path']).read_bytes())
    with service.store.connection() as db:
        d=service.store.get(db,'scoring_drafts',target_id)
        assert len(d['units'])==1 and not d['units'][0].get('confirmed_hash')
        assert len(d['imported_history'])==1 and d['imported_history'][0]['declared_total_minor']==200


def test_old_instance_cannot_write_after_reconnect(tmp_path):
    from autoreview.v2.core import MachineSettings
    from autoreview.v2.api import create_app
    from fastapi.testclient import TestClient
    settings=MachineSettings(tmp_path/'machine');settings.save(library_location=str(tmp_path/'library'))
    app=create_app(settings,nonce='test-only',start_worker=False)
    with TestClient(app,headers={'X-AutoReview-Session':'test-only'}) as c:
        state=c.get('/api/state').json()
        r=c.post('/api/settings',json={'onboarding_complete':True},headers={'X-AutoReview-Instance':'obsolete','X-AutoReview-Library':state['library']['library_id']})
        assert r.status_code==409 and r.json()['code']=='CONTEXT_CHANGED'
        assert not c.get('/api/state').json()['settings']['onboarding_complete']


def test_recompute_failure_rolls_back_every_submission_and_can_retry(service,monkeypatch):
    task,groups,_=setup(service,1);policy(service,task);intake(service,groups[0],3)
    runner=Runner(service,FakeProvider());enqueue(runner,task,groups[:1]);asyncio.run(drain(runner))
    before=service.detail(task['id']);old=manage(service,task['id']);d=old['draft']
    d=manage(service,task['id'],{'action':'confirm','revision':d['revision'],'units':[{**u,'max_points_minor':300} for u in d['units']],'declared_total_minor':300})['draft']
    data={'action':'publish','revision':d['revision'],'scope':'all'};preview=manage(service,task['id'],data)
    original=service.update_submission;calls=[]
    def fail(db,key):
        calls.append(key)
        if len(calls)==2: raise RuntimeError('injected transaction failure')
        return original(db,key)
    monkeypatch.setattr(service,'update_submission',fail)
    with pytest.raises(RuntimeError): manage(service,task['id'],{**data,'preview':False,'fingerprint':preview['fingerprint']})
    after=service.detail(task['id'])
    assert after['submissions']==before['submissions'] and after['score_snapshots']==before['score_snapshots']
    assert len(manage(service,task['id'])['versions'])==len(old['versions'])
    monkeypatch.setattr(service,'update_submission',original)
    manage(service,task['id'],{**data,'preview':False,'fingerprint':preview['fingerprint']})
    assert len(manage(service,task['id'])['versions'])==len(old['versions'])+1


def test_score_bounds_and_page_order_are_invariant():
    import random
    random.seed(7)
    for _ in range(200):
        units=[{'id':str(i),'question_id':str(i),'rule':'all_correct','max_points_minor':random.randrange(0,999)} for i in range(60)]
        p={'units':units,'declared_total_minor':sum(u['max_points_minor'] for u in units)}
        results=[{'id':u['question_id'],'status':random.choice(['correct','incorrect','unknown','missing'])} for u in units]
        value=evaluate(p,results,True)
        assert 0<=value['range_minor'][0]<=value['range_minor'][1]<=p['declared_total_minor']
        random.shuffle(results)
        assert evaluate(p,results,True)==value


def test_multi_blank_parent_unit_covers_children_without_double_deduction():
    items=[{'id':q,'kind':'fill_blank','slot':1,'section':'一','number':'1','part_id':q} for q in ('a','b')]
    unit={'id':'u','question_id':'a','question_ids':['a','b'],'rule':'all_correct','max_points_minor':500}
    p={'units':[unit],'declared_total_minor':500,'structure_hash':structure(items)}
    assert not validate(p,items,False)
    assert evaluate(p,[{'id':'a','status':'incorrect'},{'id':'b','status':'incorrect'}],True)['deducted_minor']==500
    assert evaluate(p,[{'id':'a','status':'correct'},{'id':'b','status':'correct'}],True)['final_minor']==500
    assert evaluate(p,[{'id':'a','status':'correct'},{'id':'b','status':'unknown'}],True)['final_minor'] is None
    p['units'].append({'id':'extra','question_id':'b','rule':'all_correct','max_points_minor':100})
    assert any('重复计分' in i['message'] for i in validate(p,items,False))


def test_shared_options_validation_and_local_contract():
    from autoreview.v2.reference_review import issues
    a={'id':'a','number':'1','kind':'choice','slot':1,'english':'A','response_encoding':'written_choice','option_group_id':'group', 'options':[{'id':'A','label':'cat'},{'id':'B','label':'dog'}]}
    b={**a,'id':'b','number':'2'}
    d={'items':[a,b],'asset_ids':[],'page_count':1}
    assert not any(i['code']=='SHARED_OPTIONS_CONFLICT' for i in issues(d))
    assert Answer.model_validate(a).option_group_id=='group'
    b['options']=[{'id':'A','label':'bird'},{'id':'B','label':'dog'}]
    assert any(i['code']=='SHARED_OPTIONS_CONFLICT' for i in issues(d))
