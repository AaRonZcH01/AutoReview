"""v1.1 conflicts, compact feedback, score history and conservative new contracts."""
from copy import deepcopy
import json
import asyncio
import threading
import httpx
import pytest
from autoreview.v2.core import uid,Problem
from autoreview.v2.core import MachineSettings
from autoreview.v2.api import create_app
from autoreview.v2.submission_review import review,state
from autoreview.v2.scoring import manage,gate
from autoreview.v2.grading import grade_observation,resolve_page
from autoreview.v2.scoring import evaluate
from autoreview.v2.contracts import prompt_record
from autoreview.v2.contracts_v5 import decode_output
from autoreview.v2.photo_issues import describe
from autoreview.v2.annotations import marks
from test_v10_scoring import matching,result
from test_workflow import service,setup,intake
from test_v9 import request,assigned,publish


def processed(service, scoring=False):
    task,groups,ref=setup(service,2)
    if scoring:
        request(service,task,'toggle',enabled=True);assigned(service,task)
        for slot in (1,2):request(service,task,'confirm',page_slot=slot)
        request(service,task,'confirm',total_only=True);publish(service,task)
    intake(service,groups[0],2)
    with service.store.connection(write=True) as db:
        sub=service.store.list(db,'submissions',task_id=task['id'])[0]
        policy=service.store.get(db,'tasks',task['id']).get('scoring_version_id')
        if policy:service.store.put(db,'submissions',{**sub,'scoring_version_id':policy})
        for p in service.store.list(db,'pages',parent_id=sub['id']):
            a=next(a for a in ref['items'] if a['slot']==p['slot'])
            box={'x1':.1,'y1':.2,'x2':.3,'y2':.4}
            obs=service.store.put(db,'observations',{'cells':[{'question_id':a['id'],'raw_english':'apple','uncertain':False,'box':box}],
                'photo_issues':['The writing in q1 is faint.'],'structure':[{'id':a['id'],'answer_zone':box}]})
            service.store.put(db,'pages',{**p,'state':'processed','reference_version_id':ref['id'],'observation_id':obs['id'],'photo_status':'clear'})
        service.update_submission(db,sub['id'])
    return task,sub['id']


def change(service,sub_id,**extra):
    current=state(service,sub_id);p=current['pages'][0]
    return review(service,sub_id,{'base_revision':current['submission']['revision'],'page_id':p['id'],
        'revision':p['revision'],'request_id':uid(),**extra})


def test_feedback_contains_current_results_sub_revision_score_and_photo_location(service):
    _,sub=processed(service,True)
    value=change(service,sub,question_id='q1',change={'status':'incorrect'})
    assert value['page']['results'][0]['status']=='incorrect'
    assert len(value['pages'])==2 and value['score_snapshot']['earned_minor']==200
    assert value['page']['photo_details'][0]['located']
    assert '笔迹' in value['page']['photo_details'][0]['message']
    assert value['submission']==state(service,sub)['submission']


def test_rapid_sequential_changes_use_returned_revision_and_idempotent_replay(service):
    _,sub=processed(service)
    first=change(service,sub,question_id='q1',change={'status':'incorrect'})
    p=first['page'];data={'base_revision':first['submission']['revision'],'page_id':p['id'],'revision':p['revision'],'request_id':uid(),'question_id':'q1','change':{'status':'correct'}}
    second=review(service,sub,data);assert second['page']['results'][0]['status']=='correct'
    assert review(service,sub,data)==second
    assert len(second['submission']['review_history'])==2


def test_external_conflict_reports_recovery_and_does_not_overwrite(service):
    _,sub=processed(service);before=state(service,sub);p=before['pages'][0]
    change(service,sub,question_id='q1',change={'status':'incorrect'})
    with pytest.raises(Problem) as error:
        review(service,sub,{'base_revision':before['submission']['revision'],'revision':p['revision'],'page_id':p['id'],'request_id':uid(),'question_id':'q1','change':{'status':'correct'}})
    assert error.value.code=='REVISION_CONFLICT' and error.value.details['reload_path'].endswith('/review-state')
    assert state(service,sub)['pages'][0]['results'][0]['status']=='incorrect'


def test_teacher_transcription_optional_and_undo_removes_modified_marker(service):
    _,sub=processed(service)
    assert state(service,sub)['pages'][0]['results'][0]['teacher_transcription'] is None
    value=change(service,sub,question_id='q1',change={'status':'incorrect'})
    assert value['page']['results'][0]['teacher_override'] and value['page']['results'][0]['teacher_transcription'] is None
    value=change(service,sub,action='undo');assert not value['page']['results'][0]['teacher_override']
    value=change(service,sub,action='redo');assert value['page']['results'][0]['teacher_override']


def test_photo_retake_keeps_machine_content_and_restores_via_undo(service):
    _,sub=processed(service)
    value=change(service,sub,photo_status='retake_required');assert value['page']['photo_status']=='retake_required'
    assert value['page']['results'][0]['status']=='correct'
    with pytest.raises(Problem,match='照片'):change(service,sub,action='confirm',accept_unknown=True)
    value=change(service,sub,action='undo');assert value['page']['photo_status']=='clear'


def test_retake_replacement_clears_old_photo_state_and_keeps_observation_history(service):
    _,sub=processed(service)
    value=change(service,sub,photo_status='retake_required');page=value['page'];old_observation=page['observation_id']
    with service.store.connection() as db:
        replacement=service.store.get(db,'image_versions',page['image_version_id'])['asset_id']
    service.replace_page(page['id'],{'asset_id':replacement,'revision':page['revision']})
    fresh=state(service,sub)['pages'][0]
    assert fresh['photo_status']=='clear' and fresh['observation_id'] is None and fresh['state']=='pending'
    with service.store.connection() as db:
        assert service.store.get(db,'observations',old_observation)['photo_issues']


def test_scoring_unit_edit_and_undo_recompute_points_and_judgment_together(service):
    _,sub=processed(service,True);unit=state(service,sub)['score_snapshot']['units'][0]
    value=change(service,sub,question_id=unit['question_id'],change={'score_unit_status':{unit['id']:'incorrect'}})
    assert value['page']['results'][0]['status']=='incorrect' and value['score_snapshot']['earned_minor']==200
    value=change(service,sub,action='undo');assert value['score_snapshot']['earned_minor']==400
    value=change(service,sub,action='redo');assert value['score_snapshot']['earned_minor']==200


def test_restore_published_requires_confirmation_and_preserves_versions(service):
    task,_=processed(service,True);before=manage(service,task['id']);frozen=deepcopy(before['versions'])
    request(service,task,'save',declared_total_minor=999)
    preview=request(service,task,'restore_published',preview=True)
    with pytest.raises(Problem,match='确认'):request(service,task,'restore_published',preview=False,published_version_id=preview['published_version_id'])
    request(service,task,'restore_published',preview=False,confirm=True,published_version_id=preview['published_version_id'])
    after=manage(service,task['id']);assert after['ready_to_confirm'] and after['draft']['declared_total_minor']==400
    assert after['versions']==frozen
    with service.store.connection() as db:gate(service.store,db,task['id'])


def test_restore_after_unpublish_and_incompatible_structure_is_blocked(service):
    task,_=processed(service,True);request(service,task,'unpublish',confirm=True)
    preview=request(service,task,'restore_published',preview=True)
    request(service,task,'restore_published',preview=False,confirm=True,published_version_id=preview['published_version_id'])
    assert manage(service,task['id'])['ready_to_confirm']
    draft=service.draft(task['id']);service.draft(task['id'],{'revision':draft['revision'],'items':draft['items']+[{'id':'extra','slot':1,'number':'2','english':'cat'}]})
    with pytest.raises(Problem,match='不兼容'):request(service,task,'restore_published',preview=True)


def cell(**extra):
    return {'question_id':'q1','raw_english':'dad','uncertain':False,'transcription_uncertain':False,'visibility':'visible',
        'selected_options':[],'written_options':[],'value':None,'edges':[],'box':None,'unknown_reason':None,
        'literal_evidence':'clear','ambiguous_glyphs':[],**extra}


def test_new_contract_enforces_ambiguous_stroke_unknown_with_chinese_reason():
    value=cell(ambiguous_glyphs=[{'position':2,'candidates':['o','a'],'reason':'右侧附加竖笔不清楚，不能区分o与a','box':None}])
    decoded=decode_output('student','5.2',json.dumps({'cells':[value],'photo_issues':[]}))['cells'][0]
    assert decoded['transcription_uncertain'] and '第2个' in decoded['unknown_reason']
    assert grade_observation(decoded,{'kind':'dictation','response_encoding':'text','english':'dad'})[0]=='unknown'


@pytest.mark.parametrize('text,status',[('dod','incorrect'),('dad','correct')])
def test_clear_literal_nonword_is_not_repaired_or_forced_unknown(text,status):
    value=cell(raw_english=text)
    assert grade_observation(value,{'kind':'dictation','response_encoding':'text','english':'dad'})[0]==status


def test_old_contract_decodes_without_new_fields_and_new_prompt_has_no_reference_answers():
    old=cell();old.pop('literal_evidence');old.pop('ambiguous_glyphs')
    assert decode_output('student','5.1',json.dumps({'cells':[old],'photo_issues':[]}))['cells'][0]==old
    record=prompt_record('student');assert record['schema_version']=='6.0' and 'distinguishing strokes' in record['template_text']
    assert 'ambiguous_glyphs' in record['schema']['$defs']['StudentCellV60']['properties']


def test_unknown_photo_location_never_guesses_printed_number():
    issue=describe(['The writing in q24 is faint.'],[{'id':'a','number':'24','box':{'x1':0,'y1':0,'x2':1,'y2':1}}],[])[0]
    assert not issue['located'] and issue['question_ids']==[] and '笔迹' in issue['message']


def source_geometry(answer):
    return {'nodes':[{'id':f'L{i}','label':str(i+1),'box':{'x1':.1,'x2':.2,'y1':.1+i*.2,'y2':.2+i*.2}} for i in range(3)]}


def test_matching_marks_only_wrong_and_unknown_sources_in_visual_order():
    answer,policy=matching(3);value=result(answer,[('L0','R0'),('L1','R2')])
    value['grading_detail']['unknown_edges']=[{'from_node':'L2','to_node':None,'endpoint_candidates':['L2']}]
    value['grading_detail']['missing_edges']=[]
    value['grading_detail']['unresolved_expected_edges']=[['L2','R2']]
    value['observed_structure']=source_geometry(answer)
    output=marks(value,policy)
    assert [(m['source_id'],m['status']) for m in output]==[('L1','incorrect'),('L2','unknown')]
    assert all(m['box']['x2']==.2 for m in output)


def test_matching_teacher_unit_correction_removes_only_that_source_circle():
    answer,policy=matching(3);value=result(answer,[('L0','R1'),('L1','R0'),('L2','R2')])
    value['observed_structure']=source_geometry(answer);unit=next(u for u in policy['units'] if u['source_id']=='L0')
    value['score_unit_status']={unit['id']:'correct'};value['manual_status']=True
    assert [(m['source_id'],m['status']) for m in marks(value,policy)]==[('L1','incorrect')]


@pytest.mark.parametrize('status',['incorrect','unknown'])
@pytest.mark.parametrize('legacy',[False,True])
def test_matching_partial_unit_review_keeps_other_correct_pairs_and_points(status,legacy):
    answer,policy=matching(3);unit=policy['units'][0]
    if legacy:
        for u in policy['units']:u.pop('rule_selection',None)
    observation={'cells':[{'question_id':answer['id'],'raw_english':'','uncertain':False,'visibility':'visible','edges':[
        {'id':str(i),'from_node':f'L{i}','to_node':f'R{i}'} for i in range(3)]}],'structure':[{'id':answer['id'],**source_geometry(answer)}]}
    page={'slot':1,'overrides':{answer['id']:{'status':status,'score_unit_status':{unit['id']:status}}}}
    value=resolve_page({'items':[answer]},page,observation)[0]
    assert not value['manual_status']
    score=evaluate(policy,[value])
    assert [u['status'] for u in score['units']]==[status,'correct','correct']
    assert score['earned_minor']==sum(u['max_points_minor'] for u in policy['units'][1:])


def test_matching_marks_never_use_reference_endpoint_coordinates():
    answer,policy=matching(3);value=result(answer,[('L0','R1')]);answer['matching_rule']['nodes'][0]['box']={'x1':0,'y1':0,'x2':1,'y2':1}
    assert marks(value,policy)==[]


def test_conflict_diagnostic_io_keeps_event_loop_available(tmp_path):
    settings=MachineSettings(tmp_path/'machine');settings.save(library_location=str(tmp_path/'library'))
    app=create_app(settings,nonce='test-only',start_worker=False)
    started=threading.Event();release=threading.Event()
    def delayed_record(*args,**kwargs):
        started.set();assert release.wait(3)
    app.state.service.diagnostics.record=delayed_record
    @app.get('/api/test-conflict')
    async def conflict():raise Problem('REVISION_CONFLICT','测试修订冲突',409)
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://testserver',headers={'x-autoreview-session':'test-only'}) as client:
            pending=asyncio.create_task(client.get('/api/test-conflict'))
            try:
                assert await asyncio.to_thread(started.wait,1)
                response=await asyncio.wait_for(client.get('/api/state'),1)
                assert response.status_code==200
            finally:release.set()
            assert (await pending).status_code==409
    try:asyncio.run(exercise())
    finally:app.state.service.close()
