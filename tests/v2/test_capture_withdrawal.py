"""Real isolated queue/numbering transactions. No camera or cloud calls."""
import asyncio
import pytest
from autoreview.v2 import stream_capture as capture
from autoreview.v2.core import Problem, uid
from autoreview.v2.runner import Runner
from test_workflow import service, setup, config, FakeProvider, intake
from test_v130_capture import shot, confirmation


def begin(service, n=1, dependencies=False):
    task, groups, ref = setup(service, n)
    if dependencies:
        draft = service.draft(task['id'], {'items': [{**a, 'kind': 'fill_blank', 'dependencies': [2] if a['slot'] == 1 else []} for a in ref['items']]})
        service.confirm_reference(task['id'], draft['revision'])
    runner = Runner(service, FakeProvider())
    session = capture.start(service, task['id'], {'group_id': groups[0]['id'], 'request_id': uid(), 'cloud_consent': True},
        {**config(), 'parallelism': 3, 'conservative_review': False})
    return task, groups[0], session, runner


def submit(service, session, runner):
    f = shot(service, session)
    request = confirmation(f)
    return capture.commit(service, f['id'], request, runner.enqueue_capture), request


def undo(service, session, result):
    preview = capture.withdraw_last(service, session['id'], {'preview': True, 'frame_id': result['frame_id']})
    data = {'frame_id': result['frame_id'], 'fingerprint': preview['fingerprint'], 'request_id': uid()}
    return capture.withdraw_last(service, session['id'], data), data


def test_pending_withdrawal_releases_number_and_old_commit_is_fenced(service):
    task, group, session, runner = begin(service)
    first, commit_request = submit(service, session, runner)
    result, data = undo(service, session, first)
    assert result['session']['next']['paper_tag'] == '001'
    assert service.detail(task['id'])['pages'] == []
    assert not asyncio.run(runner.tick()) and not runner.provider.calls
    assert capture.withdraw_last(service, session['id'], data) == result
    with pytest.raises(Problem, match='已撤回'):
        capture.commit(service, first['frame_id'], commit_request, runner.enqueue_capture)
    second, _ = submit(service, session, runner)
    assert second['paper_tag'] == '001' and second['page_id'] != first['page_id']
    assert capture.read(service, session['id'])['committed_count'] == 1


def test_completed_withdrawal_retains_attempt_usage_and_reuses_tag(service):
    task, _, session, runner = begin(service)
    first, _ = submit(service, session, runner)
    asyncio.run(runner.tick())
    before = service.detail(task['id'])['attempts']
    undo(service, session, first)
    assert service.detail(task['id'])['attempts'] == before
    assert before[0]['usage']['inputTokens'] == 100
    replacement, _ = submit(service, session, runner)
    assert replacement['paper_tag'] == '001'
    asyncio.run(runner.tick())
    assert [p['id'] for p in service.detail(task['id'])['pages']] == [replacement['page_id']]


def test_second_page_withdrawal_preserves_first_page_and_restores_slot(service):
    task, _, session, runner = begin(service, 2)
    first, _ = submit(service, session, runner)
    asyncio.run(runner.tick())
    second, _ = submit(service, session, runner)
    result, _ = undo(service, session, second)
    assert result['session']['next'] == {'paper_tag': '001', 'slot': 2, 'submission_id': first['submission_id']}
    detail = service.detail(task['id'])
    assert len(detail['pages']) == 1 and detail['pages'][0]['state'] == 'processed'
    assert detail['submissions'][0]['loaded_pages'] == 1
    replacement, _ = submit(service, session, runner)
    assert replacement['submission_id'] == first['submission_id'] and replacement['slot'] == 2


def test_cross_page_dependents_invalidated_and_requeued_with_replacement(service):
    task, _, session, runner = begin(service, 2, True)
    first, _ = submit(service, session, runner)
    second, _ = submit(service, session, runner)
    result, _ = undo(service, session, second)
    assert result['dependent_pages'] == 1
    assert all(w['state'] == 'superseded' for w in service.detail(task['id'])['work_items'])
    replacement, _ = submit(service, session, runner)
    assert set(replacement['queue']['queued_page_ids']) == {first['page_id'], replacement['page_id']}
    assert service.detail(task['id'])['pages'][0]['generation'] == 2


def test_later_confirm_or_batch_import_cannot_be_renumbered(service):
    task, group, session, runner = begin(service)
    first, _ = submit(service, session, runner)
    second, _ = submit(service, session, runner)
    with pytest.raises(Problem) as exc:
        capture.withdraw_last(service, session['id'], {'preview': True, 'frame_id': first['frame_id']})
    assert exc.value.code == 'CAPTURE_WITHDRAW_NOT_LAST'
    intake(service, group, 1, start_tag=3)
    with pytest.raises(Problem) as exc:
        undo(service, session, second)
    assert exc.value.code == 'CAPTURE_WITHDRAW_LATER'
    assert len(service.detail(task['id'])['pages']) == 3


@pytest.mark.parametrize('field,value', [('reviewed', True), ('overrides', {'q1': {'judgment': 'correct'}}), ('history', [{'teacher': 'modified'}])])
def test_teacher_work_refuses_withdrawal_without_side_effects(service, field, value):
    task, _, session, runner = begin(service)
    first, _ = submit(service, session, runner)
    with service.store.connection(write=True) as db:
        page = service.store.get(db, 'pages', first['page_id'])
        service.store.put(db, 'pages', {**page, field: value})
    with pytest.raises(Problem) as exc:
        undo(service, session, first)
    assert exc.value.code == 'CAPTURE_WITHDRAW_REVIEWED'
    assert capture.frame(service, first['frame_id'])['state'] == 'committed'


def test_active_draft_invalidated_only_after_explicit_withdrawal(service):
    _, _, session, runner = begin(service)
    first, _ = submit(service, session, runner)
    draft = shot(service, session)
    preview = capture.withdraw_last(service, session['id'], {'preview': True, 'frame_id': first['frame_id']})
    assert preview['discard_current_draft'] and capture.frame(service, draft['id'])['state'] == 'draft'
    result, _ = undo(service, session, first)
    assert capture.frame(service, draft['id'])['state'] == 'retaken'
    assert capture.read(service, session['id'])['active_frame'] is None
    assert result['session']['next']['paper_tag'] == '001'


def test_stale_preview_refuses_then_duplicate_request_is_atomic(service):
    _, _, session, runner = begin(service)
    first, _ = submit(service, session, runner)
    preview = capture.withdraw_last(service, session['id'], {'preview': True, 'frame_id': first['frame_id']})
    shot(service, session)
    with pytest.raises(Problem) as exc:
        capture.withdraw_last(service, session['id'], {'frame_id': first['frame_id'], 'fingerprint': preview['fingerprint'], 'request_id': uid()})
    assert exc.value.code == 'CAPTURE_WITHDRAW_CHANGED'
    result, data = undo(service, session, first)
    assert capture.withdraw_last(service, session['id'], data) == result


@pytest.mark.parametrize('failure', [False, True])
def test_late_return_cannot_restore_withdrawn_page_or_pause_other_work(service, failure):
    task, _, session, runner = begin(service)
    first, _ = submit(service, session, runner)
    second, _ = submit(service, session, runner)
    class Held(FakeProvider):
        async def infer(self, *args, **kwargs):
            index = len(self.gates)
            gate = asyncio.Event(); self.gates.append(gate)
            await gate.wait()
            if index == 1 and failure:
                raise Problem('TURN_INTERRUPTED', '撤回本地模拟')
            return await super().infer(*args, **kwargs)
    provider = Held(); provider.gates = []; runner.provider = provider
    async def exercise():
        ticks = [asyncio.create_task(runner.tick()) for _ in range(2)]
        for _ in range(100):
            if len(provider.gates) == 2: break
            await asyncio.sleep(.01)
        assert len(provider.gates) == 2
        undo(service, session, second)
        await runner.interrupt_active(page_id=second['page_id'])
        assert len(provider.interruptions) == 1
        provider.gates[1].set(); await ticks[1]
        provider.gates[0].set(); await ticks[0]
    asyncio.run(exercise())
    detail = service.detail(task['id'])
    assert [p['id'] for p in detail['pages']] == [first['page_id']]
    assert detail['pages'][0]['state'] == 'processed'
    assert detail['runs'][0]['state'] != 'paused'
    with service.store.connection() as db:
        assert service.store.get(db, 'pages', second['page_id'])['state'] == 'deleted'
        checkpoints = service.store.list(db, 'checkpoints')
        assert all(not c['eligible'] for c in checkpoints if c['work_item_id'] == detail['work_items'][1]['id'])
