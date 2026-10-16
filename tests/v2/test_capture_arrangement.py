"""Stream numbering/roster contracts against isolated SQLite and a local provider."""
import pytest
from autoreview.v2 import stream_capture as capture
from autoreview.v2.core import Problem, uid
from autoreview.v2.runner import Runner
from test_workflow import service, setup, config, FakeProvider, intake
from test_v130_capture import shot, confirmation, queued
from test_capture_withdrawal import undo


def start(service, task, group, **fields):
    return capture.start(service, task['id'], {'request_id': uid(), 'group_id': group['id'],
        'source': 'files', 'cloud_consent': True, **fields}, {**config(), 'parallelism': 3})


def submit(service, session, callback=queued):
    frame = shot(service, session)
    return capture.commit(service, frame['id'], confirmation(frame), callback)


def roster(service, task, group, slot=1, count=3):
    session = start(service, task, group, mode='single_page', slot=slot)
    results = [submit(service, session) for _ in range(count)]
    capture.stop(service, session['id'])
    return session, results


def test_fixed_page_creates_reusable_roster_without_waiting_for_all_photos(service):
    task, groups, _ = setup(service, 3)
    session = start(service, task, groups[0], mode='single_page', slot=2, start_tag='007')
    first = submit(service, session)
    assert first['queue']['queued_page_ids'] == [first['page_id']]
    results = [first, submit(service, session), submit(service, session)]
    assert [(r['paper_tag'], r['slot']) for r in results] == [('007', 2), ('008', 2), ('009', 2)]
    detail = service.detail(task['id'])
    cohort = next(c for c in detail['cohorts'] if c['id'] == session['cohort_id'])
    assert cohort['submission_ids'] == [r['submission_id'] for r in results]
    assert all(s['loaded_pages'] == 1 for s in detail['submissions'])
    assert capture.read(service, session['id'])['next'] == {'paper_tag': '010', 'slot': 2, 'submission_id': None}


@pytest.mark.parametrize('reverse,expected', [(False, ['001', '002', '003']), (True, ['003', '002', '001'])])
def test_supplement_existing_roster_matches_order_and_stops_at_end(service, reverse, expected):
    task, groups, _ = setup(service, 2)
    original, first = roster(service, task, groups[0])
    second = start(service, task, groups[0], mode='single_page', slot=2,
        cohort_id=original['cohort_id'], reverse_cohort=reverse)
    results = [submit(service, second) for _ in range(3)]
    assert [r['paper_tag'] for r in results] == expected
    assert {r['submission_id'] for r in results} == {r['submission_id'] for r in first}
    assert results[-1]['next'] is None
    with pytest.raises(Problem) as exc:
        shot(service, second)
    assert exc.value.code == 'CAPTURE_COHORT_COMPLETE'
    detail = service.detail(task['id'])
    assert len(detail['submissions']) == 3 and len(detail['pages']) == 6
    assert all(s['loaded_pages'] == 2 for s in detail['submissions'])
    assert detail['groups'][0]['next_tag'] == 4


def test_importer_and_capture_use_same_supplement_roster(service):
    task, groups, _ = setup(service, 2)
    imported = intake(service, groups[0], 2, mode='single_page', slot=2)
    session = start(service, task, groups[0], mode='single_page', slot=1,
        cohort_id=imported['cohort']['id'], reverse_cohort=True)
    assert [(r['paper_tag'], r['slot']) for r in [submit(service, session), submit(service, session)]] == [('002', 1), ('001', 1)]
    assert len(service.detail(task['id'])['submissions']) == 2


def test_occupied_slot_is_skipped_without_changing_later_paper(service):
    task, groups, _ = setup(service, 2)
    original, _ = roster(service, task, groups[0])
    supplemental = start(service, task, groups[0], mode='single_page', slot=2, cohort_id=original['cohort_id'])
    one = submit(service, supplemental)
    capture.stop(service, supplemental['id'])
    next_session = start(service, task, groups[0], mode='single_page', slot=2, cohort_id=original['cohort_id'])
    assert next_session['next']['paper_tag'] == '002'
    assert submit(service, next_session)['paper_tag'] == '002'
    assert submit(service, next_session)['paper_tag'] == '003'
    detail = service.detail(task['id'])
    assert next(p for p in detail['pages'] if p['id'] == one['page_id'])['generation'] == 1


def test_complete_roster_cannot_start_or_allocate_chain(service):
    task, groups, _ = setup(service, 2)
    original, _ = roster(service, task, groups[0])
    before = service.detail(task['id'])
    with pytest.raises(Problem) as exc:
        start(service, task, groups[0], mode='single_page', slot=1, cohort_id=original['cohort_id'])
    assert exc.value.code == 'CAPTURE_COHORT_COMPLETE'
    with service.store.connection() as db:
        assert len(service.store.list(db, 'capture_sessions')) == 1
    assert service.detail(task['id'])['cohorts'] == before['cohorts']


@pytest.mark.parametrize('fields,code', [({'mode':'bad'}, 'INTAKE_MODE'), ({'mode':'single_page','slot':3}, 'PAGE_SLOT'),
    ({'mode':'single_page','slot':True}, 'PAGE_SLOT'), ({'mode':'single_page','submission_id':'bad'}, 'CAPTURE_SUBMISSION_SCOPE')])
def test_invalid_arrangement_has_no_partial_intake(service, fields, code):
    task, groups, _ = setup(service, 2)
    with pytest.raises(Problem) as exc:
        start(service, task, groups[0], **fields)
    assert exc.value.code == code
    detail = service.detail(task['id'])
    assert not detail['cohorts'] and not detail['submissions'] and not detail['pages']


def test_other_group_roster_cannot_be_selected(service):
    task, groups, _ = setup(service, 2)
    original, _ = roster(service, task, groups[0])
    with pytest.raises(Problem) as exc:
        start(service, task, groups[1], mode='single_page', slot=2, cohort_id=original['cohort_id'])
    assert exc.value.code == 'COHORT_SCOPE'


def test_frozen_roster_change_blocks_staged_confirm(service):
    task, groups, _ = setup(service, 2)
    original, _ = roster(service, task, groups[0])
    session = start(service, task, groups[0], mode='single_page', slot=2, cohort_id=original['cohort_id'])
    draft = shot(service, session)
    with service.store.connection(write=True) as db:
        cohort = service.store.get(db, 'cohorts', original['cohort_id'])
        service.store.put(db, 'cohorts', {**cohort, 'submission_ids': list(reversed(cohort['submission_ids']))})
    with pytest.raises(Problem) as exc:
        capture.commit(service, draft['id'], confirmation(draft), queued)
    assert exc.value.code == 'CAPTURE_COHORT_CHANGED'
    assert len(service.detail(task['id'])['pages']) == 3


def test_fixed_page_withdrawal_rolls_back_number_and_roster_then_retake(service):
    task, groups, _ = setup(service, 2)
    session = start(service, task, groups[0], mode='single_page', slot=2)
    first, last = submit(service, session), submit(service, session)
    result, request = undo(service, session, last)
    assert result['session']['next'] == {'paper_tag':'002', 'slot':2, 'submission_id':None}
    assert capture.withdraw_last(service, session['id'], request) == result
    cohort = service.detail(task['id'])['cohorts'][0]
    assert cohort['submission_ids'] == [first['submission_id']]
    replacement = submit(service, session)
    assert replacement['paper_tag'] == '002' and replacement['slot'] == 2
    assert replacement['submission_id'] != last['submission_id']
    assert service.detail(task['id'])['cohorts'][0]['submission_ids'] == [first['submission_id'], replacement['submission_id']]


def test_supplement_tail_withdrawal_preserves_original_roster_and_reopens_slot(service):
    task, groups, _ = setup(service, 2)
    original, first = roster(service, task, groups[0], count=2)
    session = start(service, task, groups[0], mode='single_page', slot=2, cohort_id=original['cohort_id'], reverse_cohort=True)
    submit(service, session)
    last = submit(service, session)
    result, _ = undo(service, session, last)
    assert result['session']['next'] == {'paper_tag':'001','slot':2,'submission_id':first[0]['submission_id']}
    assert service.detail(task['id'])['cohorts'][0]['submission_ids'] == [r['submission_id'] for r in first]
    assert submit(service, session)['submission_id'] == first[0]['submission_id']


def test_stop_and_restore_draft_keeps_new_fixed_page_roster(service):
    task, groups, _ = setup(service, 3)
    session = start(service, task, groups[0], mode='single_page', slot=3)
    first = submit(service, session)
    saved = shot(service, session)
    capture.stop(service, session['id'])
    recovered = capture.recover_draft(service, saved['id'], {'request_id':uid(),'revision':saved['revision'],
        'source':'files','cloud_consent':True}, config())
    fresh = recovered['session']
    assert fresh['mode'] == 'single_page' and fresh['slot'] == 3 and fresh['cohort_id'] == session['cohort_id']
    assert (recovered['frame']['paper_tag'], recovered['frame']['slot']) == ('002',3)
    last = capture.commit(service, recovered['frame']['id'], confirmation(recovered['frame']), queued)
    assert service.detail(task['id'])['cohorts'][0]['submission_ids'] == [first['submission_id'],last['submission_id']]


def test_stop_and_restore_supplement_draft_keeps_reverse_order(service):
    task, groups, _ = setup(service, 2)
    original, _ = roster(service, task, groups[0])
    session = start(service, task, groups[0], mode='single_page', slot=2, cohort_id=original['cohort_id'], reverse_cohort=True)
    submit(service, session)
    saved = shot(service, session)
    capture.stop(service, session['id'])
    recovered = capture.recover_draft(service, saved['id'], {'request_id':uid(),'revision':saved['revision'],
        'source':'files','cloud_consent':True}, config())
    assert recovered['session']['reverse_cohort']
    assert (recovered['frame']['paper_tag'],recovered['frame']['slot']) == ('002',2)
    result = capture.commit(service,recovered['frame']['id'],confirmation(recovered['frame']),queued)
    assert result['next']['paper_tag'] == '001'


def test_queue_failure_rolls_back_fixed_page_roster_and_retries_once(service):
    task, groups, _ = setup(service, 2)
    session = start(service, task, groups[0], mode='single_page', slot=2)
    frame = shot(service, session)
    request = confirmation(frame)
    def fail(*args):
        raise Problem('QUEUE_BUSY','本地模拟入队失败')
    with pytest.raises(Problem):
        capture.commit(service,frame['id'],request,fail)
    detail = service.detail(task['id'])
    assert not detail['pages'] and not detail['submissions'] and detail['cohorts'][0]['submission_ids'] == []
    first = capture.commit(service,frame['id'],request,queued)
    assert capture.commit(service,frame['id'],request,queued) == first
    assert capture.commit(service,frame['id'],{**request,'request_id':uid()},queued) == first
    assert capture.read(service,session['id'])['next']['paper_tag'] == '002'


def test_fixed_page_keeps_cross_page_wait_and_ready_pages_enqueue_immediately(service):
    task, groups, ref = setup(service,2)
    draft = service.draft(task['id'],{'items':[{**q,'kind':'fill_blank','dependencies':[2] if q['slot']==1 else []} for q in ref['items']]})
    service.confirm_reference(task['id'],draft['revision'])
    runner = Runner(service,FakeProvider())
    first_session = start(service,task,groups[0],mode='single_page',slot=1)
    first = submit(service,first_session,runner.enqueue_capture)
    assert first['state'] == 'waiting_dependencies'
    capture.stop(service,first_session['id'])
    second_session = start(service,task,groups[0],mode='single_page',slot=2,cohort_id=first_session['cohort_id'])
    second = submit(service,second_session,runner.enqueue_capture)
    assert set(second['queue']['queued_page_ids']) == {first['page_id'],second['page_id']}
