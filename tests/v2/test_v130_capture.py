"""Capture intake uses real isolated SQLite/assets; queue callback is local only."""
from concurrent.futures import ThreadPoolExecutor
import pytest
from autoreview.v2 import stream_capture as capture
from autoreview.v2.core import Problem, uid
from test_workflow import service, setup, images, config


def begin(service, n=2, **extra):
    task, groups, ref = setup(service, n)
    data = {'group_id': groups[0]['id'], 'request_id': uid(), 'cloud_consent': True, 'source': 'files', **extra}
    session = capture.start(service, task['id'], data, {**config(), 'parallelism': 2, 'conservative_review': True})
    return task, groups[0], ref, session, data


def shot(service, session, asset=None):
    return capture.stage(service, session['id'], {'capture_id': uid(), 'asset_id': asset or images(service, 1)[0]})


def confirmation(frame, **extra):
    return {'request_id': uid(), 'revision': frame['revision'], 'preparation_stamp': frame['preparation']['stamp'],
            'zones': frame['preparation']['zones'], 'images_prepared': True, 'cloud_consent': True, **extra}


def queued(db, session, pages):
    return {'queued_page_ids': pages, 'waiting_page_ids': []}


def count(service, table):
    with service.store.connection() as db:
        return len(service.store.list(db, table))


def test_capture_draft_and_retake_never_allocate_tag(service):
    task, group, ref, session, data = begin(service)
    first = shot(service, session)
    second = shot(service, session)
    assert first['paper_tag'] == second['paper_tag'] == '001'
    assert first['slot'] == second['slot'] == 1
    assert capture.frame(service, first['id'])['state'] == 'retaken'
    assert count(service, 'pages') == count(service, 'submissions') == 0
    capture.stop(service, session['id'])
    assert capture.read(service, session['id'])['active_frame']['id'] == second['id']
    with service.store.connection() as db:
        assert service.store.get(db, 'groups', group['id'])['next_tag'] == 1


def test_capture_pages_continue_same_paper_and_then_next(service):
    task, group, ref, session, _ = begin(service)
    results = []
    for _ in range(5):
        value = shot(service, session)
        results.append(capture.commit(service, value['id'], confirmation(value), queued))
    assert [(r['paper_tag'], r['slot']) for r in results] == [('001', 1), ('001', 2), ('002', 1), ('002', 2), ('003', 1)]
    assert results[0]['submission_id'] == results[1]['submission_id']
    detail = service.detail(task['id'])
    assert len(detail['pages']) == 5
    assert all(p['preparation']['confirmed'] for p in detail['pages'])
    assert [s['loaded_pages'] for s in detail['submissions']] == [2, 2, 1]


def test_capture_confirm_is_idempotent_even_double_click_new_key(service):
    *_, session, _ = begin(service)
    value = shot(service, session)
    request = confirmation(value)
    result = capture.commit(service, value['id'], request, queued)
    assert capture.commit(service, value['id'], request, queued) == result
    assert capture.commit(service, value['id'], {**request, 'request_id': uid()}, queued) == result
    assert count(service, 'pages') == count(service, 'submissions') == 1
    assert capture.frame(service, value['id'])['commit_result'] == result


def test_capture_parallel_repeat_commit_has_one_effect(service):
    *_, session, _ = begin(service)
    value = shot(service, session)
    request = confirmation(value)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: capture.commit(service, value['id'], request, queued), range(2)))
    assert results[0] == results[1]
    assert count(service, 'pages') == 1


@pytest.mark.parametrize('patch,code', [({'images_prepared': False}, 'IMAGE_PREVIEW_REQUIRED'),
    ({'cloud_consent': False}, 'CLOUD_CONSENT'), ({'revision': -1}, 'REVISION_CONFLICT'),
    ({'preparation_stamp': 'old'}, 'PREPARATION_CHANGED'), ({'zones': {'wrong': None}}, 'REGION_MISMATCH')])
def test_capture_preflight_is_mandatory(service, patch, code):
    *_, session, _ = begin(service)
    value = shot(service, session)
    with pytest.raises(Problem) as error:
        capture.commit(service, value['id'], confirmation(value, **patch), queued)
    assert error.value.code == code
    assert count(service, 'pages') == 0


def test_capture_stop_before_confirm_blocks_intake(service):
    *_, session, _ = begin(service)
    value = shot(service, session)
    capture.stop(service, session['id'])
    with pytest.raises(Problem, match='本轮采集已停止'):
        capture.commit(service, value['id'], confirmation(value), queued)
    assert count(service, 'pages') == 0


def test_capture_stop_after_confirm_preserves_queue_and_receipt(service):
    *_, session, _ = begin(service)
    value = shot(service, session)
    request = confirmation(value)
    result = capture.commit(service, value['id'], request, queued)
    capture.stop(service, session['id'])
    assert capture.commit(service, value['id'], request, queued) == result
    with service.store.connection() as db:
        assert service.store.get(db, 'continuation_groups', session['continuation_group_id'])['user_intent'] == 'continue'
    assert count(service, 'pages') == 1


def test_capture_queue_failure_rolls_back_paper_page_and_tag(service):
    task, group, ref, session, _ = begin(service)
    value = shot(service, session)
    def fail(db, session, pages):
        raise Problem('QUEUE_BUSY', '临时不能入队')
    request = confirmation(value)
    with pytest.raises(Problem, match='临时不能入队'):
        capture.commit(service, value['id'], request, fail)
    assert count(service, 'pages') == count(service, 'submissions') == 0
    with service.store.connection() as db:
        assert service.store.get(db, 'groups', group['id'])['next_tag'] == 1
    assert capture.frame(service, value['id'])['state'] == 'draft'
    assert capture.commit(service, value['id'], request, queued)['paper_tag'] == '001'


def test_capture_stage_retry_same_id_reuses_one_draft(service):
    *_, session, _ = begin(service)
    data = {'capture_id': uid(), 'asset_id': images(service, 1)[0]}
    first = capture.stage(service, session['id'], data)
    assert capture.stage(service, session['id'], data)['id'] == first['id']
    assert count(service, 'capture_frames') == 1


def test_capture_adjustment_invalidates_old_confirmation(service):
    *_, session, _ = begin(service)
    value = shot(service, session)
    old_request = confirmation(value)
    asset_id = images(service, 1)[0]
    request = {'request_id': uid(), 'revision': value['revision'], 'asset_id': asset_id}
    newer = capture.edit(service, value['id'], request)
    assert capture.edit(service, value['id'], request)['revision'] == newer['revision']
    assert newer['preparation']['stamp'] != value['preparation']['stamp']
    with pytest.raises(Problem) as error:
        capture.commit(service, value['id'], old_request, queued)
    assert error.value.code == 'REVISION_CONFLICT'
    assert capture.commit(service, newer['id'], confirmation(newer), queued)['paper_tag'] == '001'


def test_capture_only_requeues_same_submission_candidates(service):
    *_, session, _ = begin(service)
    calls = []
    def waiting(db, session, pages):
        calls.append(pages)
        return {'queued_page_ids': pages if len(pages) == 2 else [], 'waiting_page_ids': [] if len(pages) == 2 else pages}
    results = []
    for _ in range(3):
        value = shot(service, session)
        results.append(capture.commit(service, value['id'], confirmation(value), waiting))
    assert [len(v) for v in calls] == [1, 2, 1]
    assert results[0]['state'] == results[2]['state'] == 'waiting_dependencies'
    assert results[1]['state'] == 'queued'


def test_capture_start_idempotency_and_active_group_guard(service):
    task, group, ref, session, data = begin(service)
    assert capture.start(service, task['id'], data, config())['id'] == session['id']
    with pytest.raises(Problem) as error:
        capture.start(service, task['id'], {**data, 'request_id': uid()}, config())
    assert error.value.code == 'CAPTURE_ACTIVE'


def test_capture_reference_change_does_not_silently_rebind(service):
    task, group, ref, session, _ = begin(service)
    value = shot(service, session)
    with service.store.connection(write=True) as db:
        current = service.store.get(db, 'groups', group['id'])
        service.store.put(db, 'groups', {**current, 'reference_version_id': 'other-version'})
    with pytest.raises(Problem) as error:
        capture.commit(service, value['id'], confirmation(value), queued)
    assert error.value.code == 'CAPTURE_REFERENCE_CHANGED'
    assert count(service, 'pages') == 0


def test_capture_competing_batch_import_requires_new_preview(service):
    task, group, ref, session, _ = begin(service)
    value = shot(service, session)
    service.intake(group['id'], {'asset_ids': images(service, 2), 'request_id': uid()})
    with pytest.raises(Problem) as error:
        capture.commit(service, value['id'], confirmation(value), queued)
    assert error.value.code in ('CAPTURE_TAG_CHANGED', 'CAPTURE_CHANGED')
    assert len(service.detail(task['id'])['pages']) == 2


def test_capture_can_prepare_new_photo_while_other_run_is_active(service):
    task, group, ref, session, _ = begin(service)
    with service.store.connection(write=True) as db:
        service.store.put(db, 'runs', {'parent_id': session['continuation_group_id'], 'task_id': task['id'], 'state': 'running'})
    value = shot(service, session)
    assert value['state'] == 'draft'
    assert capture.commit(service, value['id'], confirmation(value), queued)['state'] == 'queued'


def test_capture_stopped_incomplete_paper_can_be_explicitly_continued(service):
    task, group, ref, session, data = begin(service)
    value = shot(service, session)
    result = capture.commit(service, value['id'], confirmation(value), queued)
    capture.stop(service, session['id'])
    resumed = capture.start(service, task['id'], {**data, 'request_id': uid(), 'submission_id': result['submission_id']}, config())
    next_frame = shot(service, resumed)
    assert next_frame['paper_tag'] == '001' and next_frame['slot'] == 2
    next_result = capture.commit(service, next_frame['id'], confirmation(next_frame), queued)
    assert next_result['next']['paper_tag'] == '002' and next_result['next']['slot'] == 1

def test_real_queue_coalesces_live_capture_and_deduplicates(service):
    from autoreview.v2.runner import Runner
    from test_workflow import FakeProvider
    task, group, ref, session, _ = begin(service)
    runner = Runner(service, FakeProvider())
    first = shot(service, session)
    r1 = capture.commit(service, first['id'], confirmation(first), runner.enqueue_capture)
    second = shot(service, session)
    r2 = capture.commit(service, second['id'], confirmation(second), runner.enqueue_capture)
    assert r1['queue']['runs'][0]['id'] == r2['queue']['runs'][0]['id']
    with service.store.connection() as db:
        work = service.store.list(db, 'work_items', task_id=task['id'])
        assert len(work) == 2
        assert [w['seq'] for w in work] == [1, 2]
        assert all(w['preparation']['confirmed'] for w in work)
        assert runner.enqueue_capture(db, session, [r1['page_id'], r2['page_id']])['queued_page_ids'] == []
    assert capture.commit(service, first['id'], confirmation(first), runner.enqueue_capture) == r1
    assert count(service, 'work_items') == 2
    capture.stop(service, session['id'])
    with service.store.connection() as db:
        assert service.store.get(db, 'continuation_groups', session['continuation_group_id'])['user_intent'] == 'continue'
        assert service.store.get(db, 'runs', r1['queue']['runs'][0]['id'])['state'] == 'queued'


def test_real_queue_waits_for_required_page_then_enqueues_both(service):
    from autoreview.v2.runner import Runner
    from test_workflow import FakeProvider
    task, groups, ref = setup(service, 2)
    draft = service.draft(task['id'], {'items': [{**a, 'kind': 'fill_blank', 'dependencies': [2] if a['slot'] == 1 else []} for a in ref['items']]})
    ref = service.confirm_reference(task['id'], draft['revision'])
    session = capture.start(service, task['id'], {'group_id': groups[0]['id'], 'request_id': uid(), 'cloud_consent': True}, config())
    runner = Runner(service, FakeProvider())
    first = shot(service, session)
    r1 = capture.commit(service, first['id'], confirmation(first), runner.enqueue_capture)
    assert r1['state'] == 'waiting_dependencies' and count(service, 'work_items') == 0
    second = shot(service, session)
    r2 = capture.commit(service, second['id'], confirmation(second), runner.enqueue_capture)
    assert set(r2['queue']['queued_page_ids']) == {r1['page_id'], r2['page_id']}
    with service.store.connection() as db:
        work = service.store.list(db, 'work_items', task_id=task['id'])
        one = next(w for w in work if w['page_id'] == r1['page_id'])
        assert set(one['dependency_versions']) == {r2['page_id']}
        assert len(work) == 2


def test_real_queue_paused_failure_preserves_draft_and_tag(service):
    from autoreview.v2.runner import Runner
    from test_workflow import FakeProvider
    *_, session, _ = begin(service)
    runner = Runner(service, FakeProvider())
    value = shot(service, session)
    with service.store.connection(write=True) as db:
        chain = service.store.get(db, 'continuation_groups', session['continuation_group_id'])
        service.store.put(db, 'continuation_groups', {**chain, 'user_intent': 'stop'})
    with pytest.raises(Problem) as error:
        capture.commit(service, value['id'], confirmation(value), runner.enqueue_capture)
    assert error.value.code == 'CAPTURE_QUEUE_PAUSED'
    assert count(service, 'pages') == count(service, 'work_items') == 0
    assert capture.frame(service, value['id'])['state'] == 'draft'
    assert capture.read(service, session['id'])['next']['paper_tag'] == '001'


def test_real_queue_fake_provider_processes_into_shared_gallery(service):
    import asyncio
    from autoreview.v2.runner import Runner
    from test_workflow import FakeProvider, drain
    task, groups, _ = setup(service, 2)
    session = capture.start(service, task['id'], {'group_id': groups[0]['id'], 'request_id': uid(), 'cloud_consent': True}, {**config(), 'conservative_review': False})
    provider = FakeProvider()
    runner = Runner(service, provider)
    for _ in range(2):
        value = shot(service, session)
        capture.commit(service, value['id'], confirmation(value), runner.enqueue_capture)
    asyncio.run(drain(runner))
    detail = service.detail(task['id'])
    assert len(detail['pages']) == 2 and len(provider.calls) == 2
    assert all(p['state'] == 'processed' for p in detail['pages'])
    assert all(p['results'] for p in detail['pages'])
    assert 'apple' not in str(provider.calls[0]['variables'])


def test_capture_reopen_recovers_frame_and_source_filename(service):
    from autoreview.v2.service import Service
    *_, session, _ = begin(service)
    first = shot(service, session)
    capture.commit(service, first['id'], confirmation(first), queued)
    second = shot(service, session)
    settings = service.settings
    service.close()
    reopened = Service(settings)
    try:
        value = capture.read(reopened, session['id'])
        assert value['active_frame']['id'] == second['id']
        assert value['committed_files'] == [first['asset']['filename']]
        assert value['next']['slot'] == 2
        assert count(reopened, 'pages') == 1
    finally:
        reopened.close()


@pytest.mark.parametrize('seconds', [0, 3, 9, 60])
def test_capture_countdown_frozen_on_session_not_cloud_config(service, seconds):
    service.settings.save(capture_countdown_seconds=seconds)
    *_, session, _ = begin(service)
    assert session['capture_countdown_seconds'] == seconds
    assert 'capture_countdown_seconds' not in session['config']
    service.settings.save(capture_countdown_seconds=(seconds + 1) % 61)
    assert capture.read(service, session['id'])['capture_countdown_seconds'] == seconds


def test_new_capture_missing_settings_uses_current_default_two(service):
    service.settings.data.pop('capture_countdown_seconds', None)
    *_, session, _ = begin(service)
    assert session['capture_countdown_seconds'] == 2


@pytest.mark.parametrize('seconds', [-1, 61, 1.5, True, '3', None])
def test_capture_start_rejects_invalid_countdown_without_allocating_queue(service, seconds):
    service.settings.data['capture_countdown_seconds'] = seconds
    with pytest.raises(Problem) as error:
        begin(service)
    assert error.value.code == 'CAPTURE_COUNTDOWN_INVALID'
    assert count(service, 'capture_sessions') == count(service, 'continuation_groups') == 0


def test_capture_replacement_preserves_original_file_cursor(service):
    *_, session, _ = begin(service)
    first = images(service, 1)[0]
    capture.stage(service, session['id'], {'capture_id': uid(), 'asset_id': first, 'source_filename': '001_01.png'})
    replacement = capture.stage(service, session['id'], {'capture_id': uid(), 'asset_id': images(service, 1)[0], 'source_filename': '001_01.png'})
    capture.commit(service, replacement['id'], confirmation(replacement), queued)
    value = capture.read(service, session['id'])
    assert value['committed_files'] == ['001_01.png']
    assert value['next']['slot'] == 2


def stopped_draft(service):
    task, group, ref, session, _ = begin(service)
    value = shot(service, session)
    capture.stop(service, session['id'])
    request = {'request_id': uid(), 'revision': value['revision'], 'cloud_consent': True, 'source': 'files'}
    return task, group, ref, session, value, request


def test_saved_capture_list_and_recovery_copy_only_pixels_with_current_config(service):
    task, group, ref, old_session, old, request = stopped_draft(service)
    with service.store.connection(write=True) as db:
        original = service.store.get(db, 'capture_frames', old['id'])
        changed = service.store.put(db, 'capture_frames', {**original, 'preparation': {**original['preparation'],
            'confirmed': True, 'zones': {'q1': {'x1': .7, 'y1': .7, 'x2': .8, 'y2': .8}}}})
    request['revision'] = changed['revision']
    service.settings.save(capture_countdown_seconds=9)
    current_config = {**config(), 'parallelism': 4, 'conservative_review': False}
    saved = capture.list_sessions(service, task['id'])[0]['saved_frame']
    assert saved['id'] == old['id'] and saved['paper_tag'] == '001' and saved['slot'] == 1
    recovered = capture.recover_draft(service, old['id'], request, current_config)
    assert recovered['session']['id'] != old_session['id']
    assert recovered['session']['config'] == current_config and recovered['session']['capture_countdown_seconds'] == 9
    assert recovered['frame']['asset_id'] == old['asset_id'] and recovered['frame']['id'] != old['id']
    assert recovered['frame']['preparation']['confirmed'] is False and recovered['frame']['preparation']['zones'] == {'q1': None}
    assert count(service, 'pages') == count(service, 'runs') == count(service, 'work_items') == 0
    assert capture.frame(service, old['id'])['state'] == 'recovered'
    assert capture.read(service, old_session['id'])['state'] == 'stopped'
    assert not any(s.get('saved_frame') for s in capture.list_sessions(service, task['id']) if s['id'] == old_session['id'])


def test_saved_capture_recovery_is_idempotent_and_old_confirmation_rejected(service):
    *_, session, old, request = stopped_draft(service)
    recovered = capture.recover_draft(service, old['id'], request, config())
    again = capture.recover_draft(service, old['id'], request, config())
    assert again['frame']['id'] == recovered['frame']['id'] and count(service, 'capture_sessions') == 2
    with pytest.raises(Problem) as error:
        capture.commit(service, old['id'], confirmation(old), queued)
    assert error.value.code == 'CAPTURE_STOPPED'
    with pytest.raises(Problem) as error:
        capture.recover_draft(service, old['id'], {**request, 'request_id': uid()}, config())
    assert error.value.code == 'CAPTURE_DRAFT_UNAVAILABLE'


def test_saved_capture_uses_latest_reference_for_unallocated_paper(service):
    task, group, ref, session, old, request = stopped_draft(service)
    draft = service.draft(task['id'], {'items': [{**a, 'english': 'pear'} for a in ref['items']]})
    latest = service.confirm_reference(task['id'], draft['revision'])
    recovered = capture.recover_draft(service, old['id'], request, config())
    assert recovered['session']['reference_version_id'] == latest['id']
    assert recovered['frame']['reference_version_id'] == latest['id']
    assert recovered['frame']['preparation']['reference_version_id'] == latest['id']


@pytest.mark.parametrize('patch,code', [({'cloud_consent': False}, 'CLOUD_CONSENT'), ({'revision': -1}, 'REVISION_CONFLICT')])
def test_saved_capture_recovery_guards(service, patch, code):
    *_, old, request = stopped_draft(service)
    with pytest.raises(Problem) as error:
        capture.recover_draft(service, old['id'], {**request, **patch}, config())
    assert error.value.code == code and count(service, 'capture_sessions') == 1


def test_saved_capture_rejects_changed_asset_file(service):
    *_, old, request = stopped_draft(service)
    with service.store.connection() as db:
        asset = service.store.get(db, 'assets', old['asset_id'])
    (service.store.root / asset['display_path']).write_bytes(b'changed fixture')
    with pytest.raises(Problem) as error:
        capture.recover_draft(service, old['id'], request, config())
    assert error.value.code == 'CAPTURE_ASSET_CHANGED' and count(service, 'capture_sessions') == 1


def test_saved_capture_rejects_changed_continuation_page_version(service):
    *_, session, _ = begin(service)
    first = shot(service, session)
    receipt = capture.commit(service, first['id'], confirmation(first), queued)
    old = shot(service, session)
    capture.stop(service, session['id'])
    with service.store.connection(write=True) as db:
        page = service.store.get(db, 'pages', receipt['page_id'])
        service.store.put(db, 'pages', {**page, 'image_version_id': 'changed-version'})
    with pytest.raises(Problem) as error:
        capture.recover_draft(service, old['id'], {'request_id': uid(), 'revision': old['revision'], 'cloud_consent': True}, config())
    assert error.value.code == 'CAPTURE_DEPENDENCY_CHANGED' and count(service, 'capture_sessions') == 1


def test_saved_capture_continues_same_incomplete_paper_without_restarting_old_queue(service):
    *_, session, _ = begin(service)
    first = shot(service, session)
    receipt = capture.commit(service, first['id'], confirmation(first), queued)
    old = shot(service, session)
    capture.stop(service, session['id'])
    recovered = capture.recover_draft(service, old['id'], {'request_id': uid(), 'revision': old['revision'], 'cloud_consent': True}, config())
    assert recovered['frame']['slot'] == 2 and recovered['frame']['submission_id'] == receipt['submission_id']
    assert recovered['session']['continuation_group_id'] != session['continuation_group_id']
    assert count(service, 'pages') == 1 and count(service, 'runs') == 0


def test_saved_capture_taken_paper_rolls_back_recovery(service):
    task, group, ref, session, old, request = stopped_draft(service)
    service.intake(group['id'], {'asset_ids': images(service, 2), 'request_id': uid()})
    with pytest.raises(Problem) as error:
        capture.recover_draft(service, old['id'], request, config())
    assert error.value.code == 'PAPER_TAG' and count(service, 'capture_sessions') == 1
    assert capture.frame(service, old['id'])['state'] == 'draft'


def test_saved_capture_shutdown_during_preparation_creates_no_new_session(service):
    *_, old, request = stopped_draft(service)
    def closing():
        raise Problem('SHUTTING_DOWN', '正在退出', 409)
    with pytest.raises(Problem) as error:
        capture.recover_draft(service, old['id'], request, config(), write_guard=closing)
    assert error.value.code == 'SHUTTING_DOWN' and count(service, 'capture_sessions') == 1
    assert capture.frame(service, old['id'])['state'] == 'draft'


def test_discard_saved_capture_hides_only_draft_and_keeps_asset(service):
    task, _, _, session, old, request = stopped_draft(service)
    result = capture.discard_draft(service, old['id'], request)
    assert capture.discard_draft(service, old['id'], request) == result
    assert result['images_preserved'] and capture.frame(service, old['id'])['state'] == 'discarded'
    assert count(service, 'assets') == 1 and count(service, 'pages') == 0
    assert capture.read(service, session['id'])['active_frame'] is None
    assert not any(s.get('saved_frame') for s in capture.list_sessions(service, task['id']))
