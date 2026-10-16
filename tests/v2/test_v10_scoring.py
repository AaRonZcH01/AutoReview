"""v1.0 local matching defaults and content-bound scoring undo receipts."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from autoreview.v2.contracts import MatchingEdge, LocalMatchingRule
from autoreview.v2.core import Problem, digest
from autoreview.v2.grading import grade_matching
from autoreview.v2.scoring import (candidate, check, evaluate, manage, seed,
    structure, unit_hash, validate)
from test_workflow import service, setup


def matching(count=3, total=1000):
    sources=[{'id':f'L{i}','group':'left','slot':1,'display_id':str(i+1)} for i in range(count)]
    targets=[{'id':f'R{i}','group':'right','slot':1,'display_id':chr(65+i)} for i in range(count)]
    answer={'id':'matching','kind':'matching','slot':1,'section':'听力/连线','number':'1–3',
        'matching_rule':{'nodes':sources+targets,'allowed_edges':[[f'L{i}',f'R{i}'] for i in range(count)],
            'source_group':'left','directed':False,'cardinality':'one_to_one'}}
    value=candidate({'total':str(total/100),'items':[{'question_index':0,'points':str(total/100),
        'uncertain':False,'source_text':'连线题总分'}]},[answer])
    return answer,value


def result(answer, pairs):
    detail=grade_matching(LocalMatchingRule.model_validate(answer['matching_rule']),
        [MatchingEdge(id=str(i),from_node=a,to_node=b) for i,(a,b) in enumerate(pairs)])
    return {**answer,'status':detail['status'],'grading_detail':detail,'writing_status':'clear'}


def legacy_draft(service, *, explicit=False, published=False):
    answer,_=matching()
    task=service.create('tasks',{'name':'连线分项测试','scoring_enabled':True})
    service.draft(task['id'],{'items':[answer]})
    draft=manage(service,task['id'])['draft']
    whole={'id':'whole-question','question_id':answer['id'],'label':'连线题','rule':'all_correct',
        'max_points_minor':1000,'source':'manual','writing_error_policy':'incorrect'}
    if explicit: whole['rule_selection']='teacher'
    if published: whole['confirmed_hash']=unit_hash(whole)
    draft={**draft,'units':[whole],'declared_total_minor':1000,'structure_hash':structure([answer])}
    if published: draft['total_confirmed_hash']=digest([1000,[(whole['id'],unit_hash(whole))]])
    with service.store.connection(write=True) as db:
        draft=service.store.put(db,'scoring_drafts',draft)
        frozen=None
        if published:
            frozen=service.store.put(db,'scoring_versions',{**draft,'id':'frozen-whole-rule','state':'confirmed','version':1})
            service.store.put(db,'tasks',{**service.store.get(db,'tasks',task['id']),'scoring_version_id':frozen['id']})
    return task,answer,draft,frozen


def test_new_matching_defaults_to_every_source_without_inventing_missing_points():
    answer,_=matching(5)
    units=seed([answer])
    assert len(units)==5 and [u['source_id'] for u in units]==[f'L{i}' for i in range(5)]
    assert all(u['rule']=='matching_source' and u['rule_selection']=='default' for u in units)
    assert all(u['max_points_minor'] is None and not u.get('confirmed_hash') for u in units)


def test_explicit_group_controls_sources_not_node_or_edge_order():
    answer,_=matching()
    answer['matching_rule']['nodes'].reverse()
    assert {u['source_id'] for u in seed([answer])}=={'L0','L1','L2'}


def test_matching_maximum_splits_exactly_with_reviewable_deterministic_remainder():
    answer,value=matching(3,1001)
    assert [u['max_points_minor'] for u in value['units']]==[334,334,333]
    assert sum(u['max_points_minor'] for u in value['units'])==value['declared_total_minor']==1001
    assert all(u['source']=='parsed' and u['source_text']=='连线题总分' and not u.get('confirmed_hash') for u in value['units'])
    assert all(u['matching_remainder_minor']==2 and u['matching_source_count']==3 for u in value['units'])
    assert any('前2项各3.34分' in n and '合计保持10.01分' in n for n in value['notes'])
    assert not validate(value,[answer],False)


def test_single_matching_section_total_is_block_maximum_then_split_by_source():
    answer,_=matching(5)
    value=candidate({'sections':[{'section':answer['section'],'slot':1,'question_indices':[0],
        'total':'10','item_count':5,'allocation_basis':'printed_count','source_text':'连线（5组，共10分）'}]},[answer])
    assert len(value['units'])==5 and [u['max_points_minor'] for u in value['units']]==[200]*5
    assert value['declared_total_minor']==1000 and value['section_candidates'][0]['allocated']
    assert not validate(value,[answer],False)


def test_one_correct_source_earns_its_share_when_other_sources_are_wrong():
    answer,value=matching(3,900)
    graded=result(answer,[('L0','R0'),('L1','R2'),('L2','R1')])
    score=evaluate(value,[graded],True)
    assert [u['earned_minor'] for u in score['units']]==[300,0,0]
    assert score['final_minor']==300 and score['deducted_minor']==600


def test_extra_edge_invalidates_source_even_with_its_correct_pair():
    answer,value=matching(3,900)
    graded=result(answer,[('L0','R0'),('L0','R1'),('L1','R1'),('L2','R2')])
    score=evaluate(value,[graded],True)
    assert [u['earned_minor'] for u in score['units']]==[0,300,300]
    assert score['final_minor']==600


def test_pair_direction_follows_matching_rule_instead_of_source_display_order():
    answer,value=matching(2,400)
    reversed_pairs=[('R0','L0'),('R1','L1')]
    assert evaluate(value,[result(answer,reversed_pairs)],True)['final_minor']==400
    answer['matching_rule']['directed']=True
    assert evaluate(value,[result(answer,reversed_pairs)],True)['final_minor']==0
    pairs=[('L0','R0'),('R0','L0'),('L1','R1')]
    assert evaluate(value,[result(answer,pairs)],True)['final_minor']==200


def test_unknown_edge_never_turns_source_points_into_zero():
    answer,value=matching(2,400)
    detail=grade_matching(LocalMatchingRule.model_validate(answer['matching_rule']),[
        MatchingEdge(id='visible',from_node='L0',to_node='R0'),
        MatchingEdge(id='unknown',from_node='L1',to_node=None,uncertain=True)])
    graded={**answer,'status':detail['status'],'grading_detail':detail,'writing_status':'clear'}
    score=evaluate(value,[graded],True)
    assert score['final_minor'] is None and score['earned_minor']==200 and score['pending_minor']==200 and score['deducted_minor']==0
    assert [u['earned_minor'] for u in score['units']]==[200,None] and score['range_minor']==[200,400]
    # Frozen, unmarked legacy source rules keep their previous all-unknown result.
    for u in value['units']: u.pop('rule_selection')
    assert evaluate(value,[graded],True)['pending_minor']==400


def test_unlocalizable_unknown_edge_keeps_all_source_units_pending():
    answer,value=matching(2,400)
    detail=grade_matching(LocalMatchingRule.model_validate(answer['matching_rule']),[
        MatchingEdge(id='visible',from_node='L0',to_node='R0'),
        MatchingEdge(id='unknown',from_node=None,to_node='R1',uncertain=True)])
    score=evaluate(value,[{**answer,'status':'unknown','grading_detail':detail}],True)
    assert score['pending_minor']==400 and score['deducted_minor']==0 and score['earned_minor']==0


def test_source_override_still_takes_precedence_over_unknown_parent_and_manual_status():
    answer,value=matching(2,400)
    graded={**answer,'status':'unknown','manual_status':True,'grading_detail':{},
        'score_unit_status':{value['units'][0]['id']:'correct',value['units'][1]['id']:'incorrect'}}
    assert evaluate(value,[graded],True)['final_minor']==200


def test_new_marked_source_units_require_complete_declared_group_not_targets():
    answer,value=matching(3,900)
    value['units']=value['units'][:2]
    assert any('每个端点分别赋分' in p['message'] for p in validate(value,[answer],False))
    answer,value=matching(3,900)
    for i,u in enumerate(value['units']): u['source_id']=f'R{i}'
    assert any('不属于此题的源端点组' in p['message'] for p in validate(value,[answer],False))
    # The original unmarked frozen contract remains usable for local replay.
    for u in value['units']: u.pop('rule_selection')
    assert not validate(value,[answer],False)


def test_one_to_many_requires_the_whole_target_set_for_each_source():
    answer,value=matching(2,800)
    answer['matching_rule']['cardinality']='one_to_many'
    answer['matching_rule']['nodes'].append({'id':'R2','group':'right','slot':1,'display_id':'C'})
    answer['matching_rule']['allowed_edges']=[['L0','R0'],['L0','R2'],['L1','R1']]
    graded=result(answer,[('L0','R0'),('L1','R1')])
    assert evaluate(value,[graded],True)['final_minor']==400
    # Even a sparse imported grading detail cannot credit only one of two targets.
    graded['grading_detail']['missing_edges']=[]
    assert evaluate(value,[graded],True)['final_minor']==400
    assert evaluate(value,[result(answer,[('L0','R0'),('L0','R2'),('L1','R1')])],True)['final_minor']==800


def test_sync_unpublished_old_default_preserves_question_total_and_reopens_receipts(service):
    task,answer,draft,_=legacy_draft(service)
    value=manage(service,task['id'],{'action':'sync','revision':draft['revision']})['draft']
    assert len(value['units'])==3 and [u['max_points_minor'] for u in value['units']]==[334,333,333]
    assert sum(u['max_points_minor'] for u in value['units'])==value['declared_total_minor']==1000
    assert all(not u.get('confirmed_hash') for u in value['units']) and not value.get('total_confirmed_hash')
    assert any('尾差' in n for n in value['notes']) and not validate(value,[answer],False)


def test_explicit_teacher_whole_question_opt_out_survives_sync(service):
    task,_,draft,_=legacy_draft(service,explicit=True)
    value=manage(service,task['id'],{'action':'sync','revision':draft['revision']})['draft']
    assert value['units']==draft['units'] and value['units'][0]['rule']=='all_correct'


def test_unsaved_explicit_whole_question_choice_survives_sync(service):
    task,_,draft,_=legacy_draft(service)
    selected=[{**draft['units'][0],'rule_selection':'teacher','max_points_minor':1200}]
    value=manage(service,task['id'],{'action':'sync','revision':draft['revision'],
        'units':selected,'declared_total_minor':1200})['draft']
    assert value['units']==selected and value['declared_total_minor']==1200


def test_existing_published_whole_rule_and_frozen_history_are_never_rewritten(service):
    task,_,draft,frozen=legacy_draft(service,published=True)
    value=manage(service,task['id'],{'action':'sync','revision':draft['revision']})['draft']
    assert value['units']==draft['units'] and value['units'][0]['rule']=='all_correct'
    with service.store.connection() as db:
        assert service.store.get(db,'scoring_versions',frozen['id'])==frozen


def test_synthetic_candidate_preserves_total_after_matching_source_expansion():
    from test_v8_scoring import synthetic_section_case
    case=synthetic_section_case()
    value=candidate(case['scoring'],case['items'],case['attempt_id'])
    assert len(value['units'])==60 and sum(u['max_points_minor'] for u in value['units'])==value['declared_total_minor']==10000
    block=next(a for a in case['items'] if a['kind']=='matching')
    units=[u for u in value['units'] if u['question_id']==block['id']]
    assert len(units)==5 and [u['max_points_minor'] for u in units]==[200]*5
    assert all(u['rule']=='matching_source' and not u.get('confirmed_hash') for u in units)


def test_restoring_exact_total_receipt_restores_published_gate_without_new_version(service):
    task,_,_=setup(service,1)
    manage(service,task['id'],{'action':'toggle','enabled':True})
    draft=manage(service,task['id'])['draft']
    draft=manage(service,task['id'],{'action':'confirm','revision':draft['revision'],
        'units':[{**u,'max_points_minor':10000} for u in draft['units']],'declared_total_minor':10000})['draft']
    published={'action':'publish','revision':draft['revision'],'scope':'future'}
    preview=manage(service,task['id'],published)
    manage(service,task['id'],{**published,'preview':False,'fingerprint':preview['fingerprint']})
    before=manage(service,task['id']); original=deepcopy(before['draft'])
    changed=manage(service,task['id'],{'action':'save','revision':original['revision'],
        'declared_total_minor':10001,'total_confirmed_hash':None})['draft']
    assert not manage(service,task['id'])['ready_to_confirm']
    live=check(service,task['id'],{'units':original['units'],'declared_total_minor':10000,
        'total_confirmed_hash':original['total_confirmed_hash'],'structure_hash':original['structure_hash']})
    assert live['ready_to_confirm'] and live['current_version_id']==before['current_version_id']
    restored=manage(service,task['id'],{'action':'save','revision':changed['revision'],
        'declared_total_minor':10000,'total_confirmed_hash':original['total_confirmed_hash']})['draft']
    after=manage(service,task['id'])
    assert restored['total_confirmed_hash']==original['total_confirmed_hash'] and after['ready_to_confirm']
    assert after['current_version_id']==before['current_version_id'] and len(after['versions'])==len(before['versions'])


def test_stale_or_fabricated_restored_total_receipt_cannot_confirm_changed_content(service):
    task,_,_=setup(service,1)
    draft=manage(service,task['id'])['draft']
    draft=manage(service,task['id'],{'action':'confirm','revision':draft['revision'],
        'units':[{**u,'max_points_minor':100} for u in draft['units']],'declared_total_minor':100})['draft']
    for receipt in (draft['total_confirmed_hash'],'fabricated'):
        with pytest.raises(Problem,match='核对凭据与当前分值内容不一致'):
            manage(service,task['id'],{'action':'save','revision':draft['revision'],
                'declared_total_minor':101,'total_confirmed_hash':receipt})
    assert manage(service,task['id'])['draft']==draft
