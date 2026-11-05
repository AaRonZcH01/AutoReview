"""Multi-choice formatting, independent evidence, and scoring; no cloud calls."""
from copy import deepcopy

import pytest

from autoreview.v2.choice import option_ids
from autoreview.v2.choice import multiple
from autoreview.v2.grading import grade_observation, resolve_page
from autoreview.v2.reference_review import issues
from autoreview.v2.scoring import evaluate
from autoreview.v2 import verification as v
from test_v13_verification import answer, cell, reread
from test_workflow import service


def multi(**changes):
    return answer(kind='choice', response_encoding='written_choice',
        selection_rule='multiple', english='B,F',
        options=[{'id':c, 'label':c, 'box':{'x1':.1,'y1':.1,'x2':.9,'y2':.9}} for c in 'ABCDEFGHIJ'], **changes)


@pytest.mark.parametrize('text', ['BF','FB','B,F','F，B','B、F','B F','B;F','（BF）','[B,F]'])
def test_written_formats_and_order_share_the_same_complete_selection(text):
    a=multi(); observed=cell(raw=text)
    before=deepcopy(observed)
    assert grade_observation(observed,a)==('correct',{'observed_options':['B','F']})
    assert observed==before  # The literal transcript is never rewritten.


@pytest.mark.parametrize('change', [
    {'selected_options':['B','F']}, {'written_options':['B','F']},
    {'written_options':['BF']}, {'selected_options':['F','B'],'written_options':['BF']},
])
def test_structured_choices_and_literal_transcript_are_compatible(change):
    assert grade_observation(cell(raw='BF',**change),multi())[0]=='correct'


@pytest.mark.parametrize('text', ['B','B,D','B,F,J','X'])
def test_missing_extra_and_wrong_choices_do_not_receive_points(text):
    a=multi();status,_=grade_observation(cell(raw=text),a)
    assert status=='incorrect'
    score=evaluate({'declared_total_minor':200,'units':[{'id':'unit','question_id':'q',
        'rule':'all_correct','max_points_minor':200,'writing_error_policy':'incorrect'}]},
        [{'id':'q','status':status,'writing_status':'clear'}],True)
    assert score['final_minor']==0


@pytest.mark.parametrize('text', ['B/F','B?F','B或F','B,F,','B,,F','B,F because it is right','bf','BFK'])
def test_ambiguous_or_malformed_transcripts_are_not_filtered_into_correct(text):
    assert grade_observation(cell(raw=text),multi())[0]=='unknown'


@pytest.mark.parametrize('change', [
    {'written_options':['B']}, {'selected_options':['F','D']},
    {'written_options':['B','F'],'selected_options':['B']},
    {'written_options':['B','F'],'raw_english':'B,D'},
    {'uncertain':True}, {'transcription_uncertain':True},
    {'literal_evidence':'ambiguous'}, {'response_evidence':'ambiguous'},
    {'ambiguous_glyphs':[{'position':2,'candidates':['F','E']}]}, {'visibility':'not_visible'},
])
def test_uncertainty_or_conflicting_channels_never_receive_automatic_credit(change):
    observed=cell(raw='BF',**{k:val for k,val in change.items() if k!='raw_english'})
    if 'raw_english' in change:observed['raw_english']=change['raw_english']
    assert grade_observation(observed,multi())[0]=='unknown'


def test_blank_and_clear_circled_selections_remain_distinct():
    assert grade_observation(cell(raw=''),multi())[0]=='missing'
    assert grade_observation(cell(raw='',selected_options=['B','F'],literal_evidence='not_applicable'),multi())[0]=='correct'


def test_compact_splitting_uses_printed_options_never_the_correct_answer():
    a=multi();a['english']='A,C'
    assert grade_observation(cell(raw='BF'),a)==('incorrect',{'observed_options':['B','F']})
    a['options']=[];a['english']='B,F'
    assert grade_observation(cell(raw='BF'),a)[0]=='unknown'
    assert option_ids('12',[{'id':'1'},{'id':'2'}])==['12']
    assert option_ids('AB',[{'id':'AB'},{'id':'C'}])==['AB']
    assert option_ids('ABC',[{'id':'AB'},{'id':'C'}]) is None


@pytest.mark.parametrize('text', ['B,F','BF','F、B'])
def test_reference_formats_validate_against_the_entire_option_bank(text):
    a=multi();a['english']=text
    assert not [p for p in issues({'items':[a],'asset_ids':[]},include_review=False) if p['code'].startswith('CHOICE')]


@pytest.mark.parametrize('text', ['B/F','B?F','B,F,','B,B','BX','B,Z'])
def test_invalid_reference_does_not_pass_confirmation_or_silently_deduplicate(text):
    a=multi();a['english']=text
    assert any(p['code']=='CHOICE_ANSWER' for p in issues({'items':[a],'asset_ids':[]},include_review=False))
    assert grade_observation(cell(raw='BF'),a)[0]=='unknown'


def test_multiple_interpretation_does_not_change_single_choice_or_text_answers():
    a=multi();a.update(selection_rule='single',english='B')
    assert grade_observation(cell(raw='BF'),a)[0]=='incorrect'
    assert grade_observation(cell(raw='BF'),answer(english='BF'))[0]=='correct'


@pytest.mark.parametrize('kind', ['dictation','copying','fill_blank','sentence','ordering','matching','true_false'])
def test_non_choice_kind_never_enables_compact_splitting_even_with_stale_flags(kind):
    assert not multiple({'kind':kind,'selection_rule':'multiple','answer_spec':{'multiple':True}})


@pytest.mark.parametrize('selection', ['single','unknown',None])
def test_canonical_choice_requires_an_explicit_multiple_setting(selection):
    a=multi();a.update(selection_rule=selection,answer_spec={'multiple':True})
    assert not multiple(a)
    status,detail=grade_observation(cell(raw='BF'),a)
    assert status=='incorrect' and detail['observed_options']==['BF']


def test_independent_reread_agrees_on_complete_sets_with_shared_response_form():
    a=multi();first=cell(raw='BF',written_options=['B','F']);second=cell(raw='F，B',written_options=['F','B'])
    frozen=deepcopy([first,second])
    observed=reread(a,first,second)
    evidence=observed['verification']['q']
    assert evidence['status']=='verified' and evidence['form_differences']
    assert [first,second]==frozen and observed['cells'][0]==first
    assert resolve_page({'items':[a]}, {'slot':1}, observed)[0]['status']=='correct'
    assert v._agreement(first,second,{**a,'english':'UNRELATED'},policy_version=v.POLICY_VERSION)[0]
    assert not v._agreement(first,second,a,policy_version=v.PREVIOUS_POLICY_VERSION)[0]
    assert not v._agreement(first,second,a,policy_version=v.LEGACY_POLICY_VERSION)[0]


@pytest.mark.parametrize('change', [
    {'raw_english':'B','written_options':['B']},
    {'raw_english':'BD','written_options':['B','D']},
    {'raw_english':'BF','written_options':['B','D']},
    {'raw_english':'B?F','written_options':['B','F']},
    {'uncertain':True}, {'response_evidence':'ambiguous'},
])
def test_reread_disagreement_and_uncertainty_remain_pending(change):
    a=multi();first=cell(raw='BF',written_options=['B','F']);second={**first,**change}
    observed=reread(a,first,second)
    assert observed['verification']['q']['status']=='pending'
    assert resolve_page({'items':[a]},{'slot':1},observed)[0]['status']=='unknown'


def test_raw_only_agreement_and_substituted_response_form_do_not_release_correct():
    a=multi()
    assert not v._agreement(cell(raw='BF'),cell(raw='B,F'),a,policy_version=v.POLICY_VERSION)[0]
    assert not v._agreement(cell(raw='',selected_options=['B','F']),cell(raw='BF',written_options=['B','F']),a,policy_version=v.POLICY_VERSION)[0]


def test_teacher_transcript_replaces_old_conflicting_machine_options():
    a=multi();original=cell(raw='BD',written_options=['B','D'],uncertain=True)
    before=deepcopy(original)
    page={'slot':1,'overrides':{'q':{'raw_english':'BF'}}}
    assert resolve_page({'items':[a]},page,{'cells':[original]})[0]['status']=='correct'
    page['overrides']['q']['raw_english']='B?F'
    assert resolve_page({'items':[a]},page,{'cells':[original]})[0]['status']=='unknown'
    assert original==before


def test_multichoice_reference_to_runner_scoring_review_and_undo(service):
    import asyncio
    from autoreview.v2.core import uid
    from autoreview.v2.runner import Runner
    from autoreview.v2.scoring import manage
    from autoreview.v2.submission_review import review, state
    from test_workflow import FakeProvider, intake, enqueue

    task=service.create('tasks',{'name':'多选完整本地链路'})
    a=multi();a.update(answer_zone={'x1':.1,'y1':.1,'x2':.9,'y2':.9},geometry_checked=True)
    draft=service.draft(task['id'],{'items':[a],'writing_rules':[]})
    service.confirm_reference(task['id'],draft['revision'])
    manage(service,task['id'],{'action':'toggle','enabled':True})
    score=manage(service,task['id'])['draft']
    score=manage(service,task['id'],{'action':'confirm','revision':score['revision'],
        'units':[{**u,'max_points_minor':200} for u in score['units']],'declared_total_minor':200})['draft']
    request={'action':'publish','revision':score['revision'],'scope':'future'}
    preview=manage(service,task['id'],request)
    manage(service,task['id'],{**request,'preview':False,'fingerprint':preview['fingerprint']})
    group=service.create('groups',{'name':'本地测试','parent_id':task['id']})
    intake(service,group,1)

    class Provider(FakeProvider):
        async def infer(self,*args):
            value=await super().infer(*args)
            for c in value['output'].get('cells',[]):
                c.update(raw_english='F，B' if args[1]=='student_verify' else 'BF',
                    written_options=['F','B'] if args[1]=='student_verify' else ['B','F'],
                    selected_options=[],literal_evidence='clear',ambiguous_glyphs=[],
                    response_evidence='clear',response_reason='',transcription_uncertain=False)
            return value

    provider=Provider();runner=Runner(service,provider);run=enqueue(runner,task,[group])
    with service.store.connection(write=True) as db:
        service.store.put(db,'runs',{**run,'config':{**run['config'],'conservative_review':True}})
    asyncio.run(runner.tick())
    detail=service.detail(task['id']);p=detail['pages'][0];sub=p['parent_id']
    assert p['state']=='processed' and p['results'][0]['status']=='correct'
    assert p['results'][0]['verification']['status']=='verified'
    assert [c['purpose'] for c in provider.calls]==['student','student_verify']
    assert state(service,sub)['score_snapshot']['earned_minor']==200

    def edit(**data):
        current=state(service,sub);page=current['pages'][0]
        return review(service,sub,{'base_revision':current['submission']['revision'],
            'page_id':page['id'],'revision':page['revision'],'request_id':uid(),**data})

    correction=cell(raw='BD',selected_options=['BD']);correction.pop('value')
    changed=edit(question_id='q',change={'observation':correction})
    assert changed['page']['results'][0]['status']=='incorrect' and changed['score_snapshot']['earned_minor']==0
    restored=edit(action='undo')
    assert restored['page']['results'][0]['status']=='correct' and restored['score_snapshot']['earned_minor']==200
    redone=edit(action='redo')
    assert redone['page']['results'][0]['status']=='incorrect' and redone['score_snapshot']['earned_minor']==0
