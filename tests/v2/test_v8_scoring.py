import pytest
import asyncio
import json
from copy import deepcopy
from pathlib import Path

from autoreview.v2.core import Problem, digest
from autoreview.v2.scoring import (candidate, manage, structure, validate, evaluate,
    prepare_reference_publication, bind_reference_publication, unit_hash)
from autoreview.v2.contracts_v7 import ScoringCandidate, record
from autoreview.v2.contracts_v5 import decode_output
from test_workflow import service, setup, intake, Runner, FakeProvider, config, drain, images


def items(n=5):
    return [{'id':f'q{i}','number':str(i),'slot':1,'section':'听力/一','kind':'choice'} for i in range(n)]


def section(**extra):
    return {'section':'听力/一','slot':1,'question_indices':list(range(5)),'total':'10',
        'item_count':5,'allocation_basis':'printed_count','source_text':'一、听录音（10分，共5小题）',
        'uncertain':False,**extra}


def test_section_total_allocates_exact_unconfirmed_per_question_points():
    value=candidate({'total':'10','items':[],'notes_zh':[],'sections':[section()]},items())
    assert [u['max_points_minor'] for u in value['units']]==[200]*5
    assert all(u['writing_error_policy']=='incorrect' and not u.get('confirmed_hash') for u in value['units'])
    assert all(u['source']=='section_equal' and u['source_text'] for u in value['units'])
    assert value['section_candidates'][0]['allocated'] and '每题2分' in value['notes'][0]


@pytest.mark.parametrize('change',[
    {'item_count':6}, {'question_indices':[0,1,2]}, {'allocation_basis':'unknown'},
    {'uncertain':True}, {'total':'10.01'}, {'question_indices':[0,1,2,3,3]},
])
def test_ambiguous_or_fractional_section_score_never_invents_allocation(change):
    value=candidate({'items':[],'sections':[section(**change)]},items())
    assert all(u['max_points_minor'] is None for u in value['units'])
    assert not value['section_candidates'][0]['allocated'] and value['notes']


def test_section_allocation_preserves_explicit_points_and_rejects_conflict():
    value=candidate({'total':'10','items':[{'question_index':0,'points':'3','source_text':'第1题3分','uncertain':False}],
        'sections':[section()]},items())
    assert value['units'][0]['max_points_minor']==300
    assert all(u['max_points_minor'] is None for u in value['units'][1:])
    assert '冲突' in value['notes'][0]


def test_duplicate_item_candidates_and_multiblank_or_matching_are_not_guessed():
    score={'items':[{'question_index':0,'points':'2','uncertain':False}]*2}
    assert candidate(score,items())['units'][0]['max_points_minor'] is None
    for answers in ([{**a,'kind':'matching'} if i==0 else a for i,a in enumerate(items())],
                    [{**a,'number':'1'} if i<2 else a for i,a in enumerate(items())]):
        value=candidate({'items':[],'sections':[section()]},answers)
        assert all(u['max_points_minor'] is None for u in value['units'])
        assert '计分单位' in value['notes'][0]


def test_new_score_contract_preserves_section_sources_and_reads_old_v7():
    value=ScoringCandidate.model_validate({'total':None,'items':[],'notes_zh':[]})
    assert value.sections==[]
    assert record()['schema_version']=='8.0'
    decoded=decode_output('reference','7.0','{"items":[],"page_labels":[],"issues":[],"scoring":{"total":null,"items":[],"notes_zh":[]}}')
    assert decoded['scoring']['sections']==[]


def test_confirm_page_scoring_only_confirms_selected_page(service):
    task,_,_=setup(service,2)
    draft=manage(service,task['id'])['draft']
    units=[{**u,'max_points_minor':200} for u in draft['units']]
    result=manage(service,task['id'],{'action':'confirm','revision':draft['revision'],'units':units,
        'declared_total_minor':400,'page_slot':1})['draft']
    assert result['units'][0].get('confirmed_hash')
    assert not result['units'][1].get('confirmed_hash') and not result.get('total_confirmed_hash')
    with pytest.raises(Problem,match='有效页码'):
        manage(service,task['id'],{'action':'confirm','revision':result['revision'],'page_slot':True})


def test_publish_scoring_before_answers_and_bind_atomically(service):
    task=service.create('tasks',{'name':'先分值后答案','scoring_enabled':True})
    ref_draft=service.draft(task['id'],{'items':[{'id':'q','number':'1','slot':1,'english':'cat'}]})
    draft=manage(service,task['id'])['draft']
    draft=manage(service,task['id'],{'action':'confirm','revision':draft['revision'],
        'units':[{**u,'max_points_minor':100} for u in draft['units']],'declared_total_minor':100})['draft']
    data={'action':'publish','revision':draft['revision'],'scope':'future'}
    preview=manage(service,task['id'],data)
    manage(service,task['id'],{**data,'preview':False,'fingerprint':preview['fingerprint']})
    state=manage(service,task['id'])
    assert state['ready_to_confirm'] and state['current_version_id']
    with service.store.connection(write=True) as db:
        current_task=service.store.get(db,'tasks',task['id'])
        prepared=prepare_reference_publication(service,db,current_task,ref_draft['items'])
        assert prepared['reference_version_id'] is None
        ref=service.store.put(db,'reference_versions',{'task_id':task['id'],'parent_id':task['id'],
            'items':ref_draft['items'],'page_count':1})
        version_id=bind_reference_publication(service,db,current_task,ref)
        bound=service.store.get(db,'scoring_versions',version_id)
        assert bound['reference_version_id']==ref['id'] and bound['bound_from_id']==prepared['id']
        assert service.store.get(db,'scoring_versions',prepared['id'])['reference_version_id'] is None
    draft=state['draft']
    manage(service,task['id'],{'revision':draft['revision']+1,'units':[{**u,'max_points_minor':200} for u in draft['units']],
        'declared_total_minor':200})
    assert not manage(service,task['id'])['ready_to_confirm']
    with service.store.connection() as db:
        with pytest.raises(Problem,match='草稿已修改'):
            prepare_reference_publication(service,db,service.store.get(db,'tasks',task['id']),ref_draft['items'])


def test_new_writing_default_deducts_but_explicit_ignore_remains(service):
    task,_,_=setup(service,1);draft=manage(service,task['id'])['draft']
    assert draft['units'][0]['writing_error_policy']=='incorrect'
    unit={**draft['units'][0],'max_points_minor':100}
    result={'id':unit['question_id'],'status':'correct','writing_status':'confirmed_error'}
    assert evaluate({'units':[unit],'declared_total_minor':100},[result],True)['final_minor']==0
    unit['writing_error_policy']='ignore'
    assert evaluate({'units':[unit],'declared_total_minor':100},[result],True)['final_minor']==100


def test_answer_confirmation_gate_binds_prepublished_score_to_formal_version(service):
    task=service.create('tasks',{'name':'正式流程','scoring_enabled':True})
    group=service.create('groups',{'name':'三1','parent_id':task['id']})
    reference=service.draft(task['id'],{'items':[{'id':'q','number':'1','slot':1,'english':'cat'}]})
    with pytest.raises(Problem,match='发布'):
        service.confirm_reference(task['id'],reference['revision'])
    draft=manage(service,task['id'])['draft']
    draft=manage(service,task['id'],{'action':'confirm','revision':draft['revision'],
        'units':[{**u,'max_points_minor':100} for u in draft['units']],'declared_total_minor':100})['draft']
    data={'action':'publish','revision':draft['revision'],'scope':'future'}
    preview=manage(service,task['id'],data)
    manage(service,task['id'],{**data,'preview':False,'fingerprint':preview['fingerprint']})
    formal=service.confirm_reference(task['id'],reference['revision'])
    state=manage(service,task['id'])
    assert next(p for p in state['versions'] if p['id']==state['published_version_id'])['reference_version_id']==formal['id']
    assert state['ready_to_confirm']
    intake(service,group,1)
    changed=service.draft(task['id'],{'revision':service.draft(task['id'])['revision'],
        'items':[{'id':'q','number':'2','slot':1,'english':'cat'}]})
    with pytest.raises(Problem,match='发布'):
        service.confirm_reference(task['id'],changed['revision'])


def test_initial_reference_runner_expands_section_candidates_without_cloud(service):
    class SectionProvider(FakeProvider):
        async def infer(self,*args,**kwargs):
            result=await super().infer(*args,**kwargs)
            if result['output'].get('items'):
                result['output']['items']=[{**a,'english':'A'} for a in items()]
                for a in result['output']['items']: a.pop('id')
                result['output']['scoring']={'total':'10','items':[],'sections':[section()],'notes_zh':[]}
            return result
    task=service.create('tasks',{'name':'解析候选','scoring_enabled':True})
    service.draft(task['id'],{'asset_ids':images(service,1)})
    runner=Runner(service,SectionProvider())
    runner.create(task['id'],{'purpose':'reference','request_id':'scoring-candidate-test',
        'cloud_consent':True,'images_prepared':True},config())
    asyncio.run(drain(runner))
    draft=manage(service,task['id'])['draft']
    assert len(draft['units'])==5 and all(u['max_points_minor']==200 for u in draft['units'])
    assert draft['section_candidates'][0]['allocated']
    assert not manage(service,task['id'])['ready_to_confirm']


def test_legacy_draft_missing_writing_default_requires_reconfirmation_not_history_mutation(service):
    task,_,reference=setup(service,2)
    seeded=manage(service,task['id'])['draft']
    units=[{**u,'max_points_minor':200} for u in seeded['units']]
    units[0].pop('writing_error_policy')
    units[1]['writing_error_policy']='ignore'
    units=[{**u,'confirmed_hash':unit_hash(u)} for u in units]
    legacy={**seeded,'units':units,'declared_total_minor':400,
        'total_confirmed_hash':digest([400,[(u['id'],unit_hash(u)) for u in units]])}
    with service.store.connection(write=True) as db:
        old=service.store.put(db,'scoring_versions',{**legacy,'id':'frozen-legacy-score',
            'reference_version_id':reference['id'],'state':'confirmed','version':1})
        service.store.put(db,'scoring_drafts',legacy)
        current=service.store.get(db,'tasks',task['id'])
        service.store.put(db,'tasks',{**current,'scoring_enabled':True,'scoring_version_id':old['id']})
    state=manage(service,task['id']);draft=state['draft']
    assert draft['units'][0]['writing_error_policy']=='incorrect'
    assert draft['units'][0]['confirmed_hash'] is None and draft['total_confirmed_hash'] is None
    assert draft['units'][1]['writing_error_policy']=='ignore' and draft['units'][1]['confirmed_hash']==units[1]['confirmed_hash']
    assert not state['ready_to_confirm']
    with service.store.connection() as db:
        frozen=service.store.get(db,'scoring_versions',old['id'])
        assert frozen==old and 'writing_error_policy' not in frozen['units'][0]
        with pytest.raises(Problem,match='草稿已修改'):
            prepare_reference_publication(service,db,service.store.get(db,'tasks',task['id']),reference['items'])
    # Sending the old apparent confirmation cannot bypass default normalization.
    with pytest.raises(Problem,match='尚未全部确认'):
        manage(service,task['id'],{'action':'publish','revision':draft['revision'],'units':units,
            'declared_total_minor':400,'scope':'future'})
    page=manage(service,task['id'],{'action':'confirm','revision':draft['revision'],'page_slot':2})['draft']
    assert page['units'][0]['confirmed_hash'] is None and page['units'][1]['confirmed_hash']
    assert not manage(service,task['id'])['ready_to_confirm']
    confirmed=manage(service,task['id'],{'action':'confirm','revision':page['revision']})['draft']
    data={'action':'publish','revision':confirmed['revision'],'scope':'future'}
    preview=manage(service,task['id'],data)
    manage(service,task['id'],{**data,'preview':False,'fingerprint':preview['fingerprint']})
    state=manage(service,task['id']);assert state['ready_to_confirm']
    new=next(v for v in state['versions'] if v['id']==state['current_version_id'])
    results=[{'id':u['question_id'],'status':'correct','writing_status':'confirmed_error'} for u in units]
    assert evaluate(old,results,True)['final_minor']==400
    assert evaluate(new,results,True)['final_minor']==200
    with service.store.connection() as db:
        assert service.store.get(db,'scoring_versions',old['id'])==old


def synthetic_section_case():
    """Generated hierarchy fixture; contains no captured run or exam records."""
    rows, sections = [], []
    for slot, parent, totals in [(1, '听力', [10]*6), (2, '笔试', [5,5,5,5,10,10])]:
        parent_start = len(rows)
        for number, points in enumerate(totals):
            label = parent + '/' + '一二三四五六'[number]
            start = len(rows)
            count = 1 if slot == 1 and number == 3 else 5
            for item in range(count):
                row = {'id': f'synthetic-question-{len(rows)+1}', 'kind':'choice',
                       'slot':slot, 'section':label, 'number':str(item+1)}
                if count == 1:
                    row['kind'] = 'matching'
                    nodes = [{'id':f'{group}{i}', 'group':group, 'label':str(i), 'slot':1,
                              'box':{'x1':x, 'y1':.1+i*.1, 'x2':x+.08, 'y2':.15+i*.1}}
                             for group,x in [('left',.1),('right',.7)] for i in range(5)]
                    row['matching_rule'] = {'nodes':nodes, 'allowed_edges':[[f'left{i}',f'right{i}'] for i in range(5)],
                        'cardinality':'one_to_one', 'directed':False, 'grading_unit':'edge', 'source_group':'left'}
                rows.append(row)
            sections.append({'section':label,'slot':slot,'question_indices':list(range(start,len(rows))),
                'total':str(points),'item_count':None,'allocation_basis':'unknown' if count==1 else 'structured_units',
                'source_text':f'合成小节 {number+1}（{points}分）','uncertain':False})
        sections.append({'section':parent,'slot':slot,'question_indices':list(range(parent_start,len(rows))),
            'total':str(sum(totals)),'item_count':None,'allocation_basis':'unknown',
            'source_text':f'合成汇总（{sum(totals)}分）','uncertain':False})
    return {'attempt_id':'synthetic-attempt','items':rows,'scoring':{'total':None,'sections':sections,
        'notes_zh':[], 'items':[{'question_index':15,'points':'10','source_text':'合成连线题10分','uncertain':False}]}}


def test_synthetic_section_parents_do_not_block_leaf_allocations():
    case=synthetic_section_case()
    value=candidate(case['scoring'],case['items'],case['attempt_id'])
    assert len(value['units'])==60 and all(u['max_points_minor'] is not None for u in value['units'])
    assert value['declared_total_minor']==10000==sum(u['max_points_minor'] for u in value['units'])
    assert all(not u.get('confirmed_hash') and u['writing_error_policy']=='incorrect' for u in value['units'])
    matching=[u for u in value['units'] if u['question_id']==case['items'][15]['id']]
    assert len(matching)==5 and all(u['rule']=='matching_source' for u in matching)
    assert sum(u['max_points_minor'] for u in matching)==1000 and all(u['source']=='parsed' for u in matching)
    parents=[s for s in value['section_candidates'] if s['summary']]
    leaves=[s for s in value['section_candidates'] if not s['summary']]
    assert {s['section']:s['total_minor'] for s in parents}=={'听力':6000,'笔试':4000}
    assert all(not s['allocated'] and not s['message'] and s['source_text'] for s in parents)
    assert len(leaves)==12 and all(s['allocated'] and not s['message'] for s in leaves)
    assert all(u['source_text'] for u in value['units'])
    reordered=deepcopy(case['scoring']);reordered['sections'].reverse()
    other=candidate(reordered,case['items'])
    assert [(u['question_id'],u['max_points_minor'],u['source']) for u in other['units']]==[(u['question_id'],u['max_points_minor'],u['source']) for u in value['units']]


@pytest.mark.parametrize('changed_section,total',[('听力','59'),('听力/一','9')])
def test_parent_child_total_conflicts_block_automatic_allocation(changed_section,total):
    case=synthetic_section_case()
    for s in case['scoring']['sections']:
        if s['section']==changed_section:s['total']=total
    value=candidate(case['scoring'],case['items'])
    assert value['declared_total_minor'] is None
    first_page={a['id'] for a in case['items'][:26]}; matching_id=case['items'][15]['id']
    assert all(u['max_points_minor'] is None for u in value['units'] if u['question_id'] in first_page and u['question_id']!=matching_id)
    assert sum(u['max_points_minor'] for u in value['units'] if u['question_id']==matching_id)==1000  # Preserve directly observed question total across source units.
    assert all(u['max_points_minor'] is not None for u in value['units'] if u['question_id'] not in first_page)
    assert any('父级汇总分与下级分值合计不一致' in n for n in value['notes'])
    assert all(not s['allocated'] for s in value['section_candidates'] if s['section'].startswith('听力'))


@pytest.mark.parametrize('other',[
    {'section':'听力/一','question_indices':list(range(5)),'slot':1},  # Same-range duplicate.
    {'section':'听力/二','question_indices':[4],'slot':1},  # Unrelated overlap.
    {'section':'听力','question_indices':list(range(5)),'slot':1},  # Parent with equal range, not strict containment.
    {'section':'听力','question_indices':list(range(5)),'slot':2},  # Incompatible page scope.
])
def test_nonhierarchical_overlaps_are_still_blocked(other):
    value=candidate({'items':[],'sections':[section(),section(**other)]},items())
    assert all(u['max_points_minor'] is None for u in value['units'])
    assert any('重复候选' in n for n in value['notes'])


def test_partial_hierarchy_conflict_with_explicit_item_withdraws_derived_points():
    answers=[{'id':'q1','number':'1','slot':1,'section':'A/x','kind':'choice'},
        {'id':'q2','number':'2','slot':1,'section':'A/x','kind':'choice'},
        {'id':'q3','number':'3','slot':1,'section':'A/y','kind':'choice'}]
    rules={'total':None,'items':[{'question_index':2,'points':'9','source_text':'第3题9分','uncertain':False}],
        'sections':[{'section':'A','slot':1,'question_indices':[0,1,2],'total':'10','item_count':None,
                     'allocation_basis':'unknown','source_text':'A共10分','uncertain':False},
                    {'section':'A/x','slot':1,'question_indices':[0,1],'total':'4','item_count':2,
                     'allocation_basis':'printed_count','source_text':'A/x共4分','uncertain':False}]}
    value=candidate(rules,answers)
    assert [u['max_points_minor'] for u in value['units']]==[None,None,900]
    assert value['declared_total_minor'] is None
    assert all(not s['allocated'] for s in value['section_candidates'])
    assert all('父级汇总分与下级分值合计不一致' in s['message'] for s in value['section_candidates'])
    assert value['units'][2]['source']=='parsed' and value['units'][2]['source_text']=='第3题9分'
    assert all(not u.get('confirmed_hash') for u in value['units'])
