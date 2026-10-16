"""Safe total candidates use complete disjoint outer scopes, not guessed blanks."""
from copy import deepcopy

import pytest

from autoreview.v2.scoring import candidate


def case():
    answers=[{'id':f'q{i}','slot':1 if i<2 else 2,'section':label,'number':str(i+1),
              'kind':'fill_blank'} for i,label in enumerate(['听力/一','听力/二','笔试/一','笔试/二'])]
    def scope(label,indices,total,**extra):
        return {'section':label,'slot':None,'question_indices':indices,'total':str(total),
                'allocation_basis':'unknown','uncertain':False,'source_text':label+'总分',**extra}
    scoring={'total':None,'items':[], 'sections':[
        scope('听力',[0,1],60),scope('笔试',[2,3],40),
        scope('听力/一',[0],30),scope('听力/二',[1],30),
        scope('笔试/一',[2],10),scope('笔试/二',[3],30)]}
    return answers,scoring


def test_complete_parent_totals_fill_total_while_unknown_small_scores_stay_empty():
    answers,scoring=case(); original=deepcopy((answers,scoring))
    result=candidate(scoring,answers)
    assert result['declared_total_minor']==10000
    assert all(u['max_points_minor'] is None and not u.get('confirmed_hash') for u in result['units'])
    assert not result.get('total_confirmed_hash')
    assert any('顶层分区' in n and '100分' in n for n in result['notes'])
    assert (answers,scoring)==original  # A new candidate never rewrites input/history.


def test_parent_child_totals_are_not_double_counted_and_order_does_not_matter():
    answers,scoring=case()
    scoring['sections'].reverse()
    assert candidate(scoring,answers)['declared_total_minor']==10000


def test_total_candidate_can_be_complete_while_one_small_question_is_not():
    answers,scoring=case()
    scoring['items']=[{'question_index':i,'points':str(points),'uncertain':False}
                      for i,points in enumerate([30,30,10])]
    result=candidate(scoring,answers)
    assert result['declared_total_minor']==10000
    assert [u['max_points_minor'] for u in result['units']]==[3000,3000,1000,None]
    assert all(not u.get('confirmed_hash') for u in result['units'])


def test_leaf_only_totals_are_valid_when_they_cover_every_question_once():
    answers,scoring=case(); scoring['sections']=scoring['sections'][2:]
    assert candidate(scoring,answers)['declared_total_minor']==10000


def test_missing_question_coverage_cannot_invent_the_total():
    answers,scoring=case()
    scoring['sections']=[s for s in scoring['sections'] if s['section'] not in ('笔试','笔试/二')]
    result=candidate(scoring,answers)
    assert result['declared_total_minor'] is None
    assert any('未完整覆盖全卷' in n for n in result['notes'])


@pytest.mark.parametrize('change',['duplicate_parent','duplicate_leaf','partial_parent','cross_section','uncertain_top'])
def test_overlapping_incomplete_or_uncertain_outer_sources_do_not_generate_total(change):
    answers,scoring=case()
    if change=='duplicate_parent': scoring['sections'].append(deepcopy(scoring['sections'][0]))
    elif change=='duplicate_leaf': scoring['sections'].append(deepcopy(scoring['sections'][2]))
    elif change=='partial_parent': scoring['sections'][0]['question_indices']=[0]
    elif change=='cross_section': scoring['sections'][0]['question_indices']=[0,1,2]
    elif change=='uncertain_top': scoring['sections'][0]['uncertain']=True
    assert candidate(scoring,answers)['declared_total_minor'] is None


@pytest.mark.parametrize('change',['child_mismatch','explicit_leaf_mismatch','partial_lower_bound'])
def test_conflicting_known_values_block_total_even_when_some_item_scores_missing(change):
    answers,scoring=case()
    if change=='child_mismatch': scoring['sections'][2]['total']='31'
    elif change=='explicit_leaf_mismatch':
        scoring['items']=[{'question_index':0,'points':'31','uncertain':False}]
    else:
        # One known child already exceeds its parent; another child is absent.
        scoring['sections']=[s for s in scoring['sections'] if s['section']!='听力/二']
        scoring['sections'][2]['total']='61'
    result=candidate(scoring,answers)
    assert result['declared_total_minor'] is None
    assert any('冲突' in n or '不一致' in n for n in result['notes'])


@pytest.mark.parametrize('explicit,conflict',[('100',False),('99',True),('0',True)])
def test_explicit_total_has_priority_and_disagreement_is_reported(explicit,conflict):
    answers,scoring=case(); scoring['total']=explicit
    result=candidate(scoring,answers)
    assert result['declared_total_minor']==int(explicit)*100
    assert any('明确识别的试卷总分已保留' in n for n in result['notes'])==conflict
    assert all(u['max_points_minor'] is None for u in result['units'])


def test_explicit_total_remains_visible_when_child_sources_conflict():
    answers,scoring=case(); scoring['total']='100'; scoring['sections'][2]['total']='31'
    result=candidate(scoring,answers)
    assert result['declared_total_minor']==10000
    assert any('明确识别的试卷总分已保留' in n for n in result['notes'])


def test_excluded_question_is_not_required_for_complete_total_coverage():
    answers,scoring=case(); answers.append({'id':'excluded','slot':3,'section':'附加题','number':'1','excluded':True})
    assert candidate(scoring,answers)['declared_total_minor']==10000


def test_no_section_legacy_complete_item_totals_still_work():
    answers,scoring=case(); scoring['sections']=[]
    scoring['items']=[{'question_index':i,'points':'25','uncertain':False} for i in range(4)]
    assert candidate(scoring,answers)['declared_total_minor']==10000


def test_invalid_explicit_total_is_not_silently_replaced():
    answers,scoring=case(); scoring['total']='100.001'
    assert candidate(scoring,answers)['declared_total_minor'] is None
