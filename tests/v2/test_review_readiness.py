"""Writing clearance and identical pending gates for detail/gallery/bulk."""
from copy import deepcopy
import pytest
from autoreview.v2.core import uid,Problem
from autoreview.v2.submission_review import state
from autoreview.v2.batch_review import batch
from autoreview.v2.review_readiness import needs_review
from test_workflow import service
from test_v11_review import processed,change


def pending(service,scoring=True,status='needs_review',raw='apple'):
    task,sub=processed(service,scoring)
    with service.store.connection(write=True) as db:
        page=service.store.list(db,'pages',parent_id=sub)[0]
        old=service.store.get(db,'observations',page['observation_id'])
        copy={k:deepcopy(v) for k,v in old.items() if k not in ('id','revision','created_at','updated_at')}
        copy['cells'][0]['raw_english']=raw
        copy['writing']=[{'question_id':'q1','status':status}]
        obs=service.store.put(db,'observations',copy)
        service.store.put(db,'pages',{**page,'observation_id':obs['id']})
        service.update_submission(db,sub)
    return task,sub


def preview(service,task,sub):
    p=state(service,sub)['pages'][0]
    return batch(service,task['id'],{'preview':True,'action':'confirm','request_id':uid(),'pages':[{'id':p['id'],'revision':p['revision']}]})


@pytest.mark.parametrize('scoring',[False,True])
def test_correct_award_clears_writing_in_same_undoable_transaction(service,scoring):
    task,sub=pending(service,scoring)
    before=state(service,sub)
    if scoring:
        unit=before['score_snapshot']['units'][0]['id']
        edit={'score_unit_status':{unit:'correct'}}
    else:edit={'status':'correct'}
    page=change(service,sub,question_id='q1',change=edit)['page']
    assert page['results'][0]['status']=='correct'
    assert page['results'][0]['writing_status']=='clear'
    assert page['results'][0]['review_pending'] is False
    assert len(preview(service,task,sub)['accepted'])==1
    undone=change(service,sub,action='undo')['page']['results'][0]
    assert undone['status']=='correct' and undone['writing_status']=='pending'
    redone=change(service,sub,action='redo')['page']['results'][0]
    assert redone['status']=='correct' and redone['writing_status']=='clear'
    assert change(service,sub,action='confirm')['page']['reviewed']


@pytest.mark.parametrize('raw',['banana',''])
def test_known_error_or_blank_does_not_require_second_writing_review(service,raw):
    task,sub=pending(service,raw=raw)
    a=state(service,sub)['pages'][0]['results'][0]
    assert a['status'] in ('incorrect','missing') and not needs_review(a)
    assert a['writing_status']=='clear' and a['writing_candidate']['status']=='needs_review'
    assert len(preview(service,task,sub)['accepted'])==1


def test_actual_pending_writing_is_visible_and_rejected_with_exact_reason(service):
    task,sub=pending(service)
    a=state(service,sub)['pages'][0]['results'][0]
    assert a['status']=='correct' and needs_review(a) and a['review_pending']
    assert a['review_pending_reasons']==['书写判定待核实']
    rejected=preview(service,task,sub)['rejected'][0]
    assert '还有1题待核实' in rejected['reasons'][0]
    assert '第1页' in rejected['reasons'][0] and '书写判定待核实' in rejected['reasons'][0]
    with pytest.raises(Problem) as e:change(service,sub,action='confirm')
    assert e.value.code=='UNRESOLVED_REVIEW' and e.value.details['question_ids']==['q1']
    cleared=change(service,sub,question_id='q1',change={'writing_status':'clear'})['page']['results'][0]
    assert cleared['status']=='correct' and not cleared['review_pending']
    assert len(preview(service,task,sub)['accepted'])==1


@pytest.mark.parametrize('scoring',[False,True])
@pytest.mark.parametrize('verdict',['incorrect','missing'])
def test_wrong_or_missing_teacher_verdict_clears_writing(service,scoring,verdict):
    _,sub=pending(service,scoring)
    current=state(service,sub)
    edit={'score_unit_status':{current['score_snapshot']['units'][0]['id']:verdict}} if scoring else {'status':verdict}
    a=change(service,sub,question_id='q1',change=edit)['page']['results'][0]
    assert a['status'] in ('incorrect','missing') and a['writing_status']=='clear' and not a['review_pending']


def test_legacy_teacher_correct_override_reuses_decision_without_rewriting_observation(service):
    _,sub=pending(service)
    with service.store.connection(write=True) as db:
        page=service.store.list(db,'pages',parent_id=sub)[0]
        service.store.put(db,'pages',{**page,'overrides':{'q1':{'status':'correct'}}})
        old=service.store.get(db,'observations',page['observation_id'])
    a=state(service,sub)['pages'][0]['results'][0]
    assert a['writing_status']=='clear' and not a['review_pending']
    with service.store.connection() as db:assert service.store.get(db,'observations',page['observation_id'])==old


def test_explicit_writing_choice_wins_and_layout_edits_do_not_clear_it(service):
    _,sub=pending(service)
    a=change(service,sub,question_id='q1',change={'status':'correct','writing_status':'pending'})['page']['results'][0]
    assert a['status']=='correct' and a['review_pending']
    a=change(service,sub,question_id='q1',change={'box':{'x1':.2,'y1':.2,'x2':.4,'y2':.4}})['page']['results'][0]
    assert a['writing_status']=='pending'
    change(service,sub,question_id='q1',change={'writing_status':'confirmed_error'})
    assert change(service,sub,question_id='q1',change={'status':'correct'})['page']['results'][0]['writing_status']=='clear'


def test_correct_unit_does_not_confirm_other_unknown_units(service):
    _,sub=pending(service)
    current=state(service,sub);oldunit=current['score_snapshot']['units'][0]['id']
    with service.store.connection(write=True) as db:
        owner=service.store.get(db,'submissions',sub)
        policy=service.store.get(db,'scoring_versions',owner['scoring_version_id'])
        base={k:deepcopy(v) for k,v in policy.items() if k not in ('id','revision','created_at','updated_at')}
        base['units'].insert(1,{**base['units'][0],'id':'second-unit','max_points_minor':0})
        version=service.store.put(db,'scoring_versions',base)
        service.store.put(db,'submissions',{**owner,'scoring_version_id':version['id']})
        service.update_submission(db,sub)
    a=change(service,sub,question_id='q1',change={'score_unit_status':{oldunit:'correct'}})['page']['results'][0]
    assert a['writing_status']=='clear' and a['status']=='unknown' and a['review_pending']
    assert a['review_pending_reasons']==['内容判定待核实']
