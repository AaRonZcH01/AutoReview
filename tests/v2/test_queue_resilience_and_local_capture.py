"""Fault injection and local intake; never uses a camera or real provider."""
import asyncio
import json
import pytest
from pydantic import ValidationError
from autoreview.v2.core import Problem, uid
from autoreview.v2.output_safety import decode_response
from autoreview.v2.runner import Runner
from autoreview.v2 import stream_capture as capture
from autoreview.v2.capture_remedy import remedy
from test_workflow import service, setup, config, FakeProvider, drain
from test_v130_capture import begin, shot, confirmation
from test_v13_runner_verification import scene


def raw_cell(box):
    return {'question_id': 'q1', 'raw_english': 'aple', 'uncertain': False,
        'transcription_uncertain': False, 'visibility': 'visible', 'selected_options': [],
        'written_options': [], 'value': None, 'edges': [], 'box': box, 'unknown_reason': None,
        'literal_evidence': 'clear', 'ambiguous_glyphs': [], 'response_evidence': 'clear', 'response_reason': ''}


@pytest.mark.parametrize('box', [
    {'x1': .339, 'x2': .349, 'y1': .66, 'y2': .3575},
    {'x1': .649, 'x2': .659, 'y1': .686, 'y2': .6815},
    {'x1': .4, 'x2': .2, 'y1': .2, 'y2': .5},
    {'x1': -1, 'x2': .2, 'y1': .2, 'y2': .5},
    {'x1': .1, 'x2': .1, 'y1': .2, 'y2': .5},
    {'x1': 'bad', 'x2': .2, 'y1': .2, 'y2': .5},
])
def test_optional_invalid_box_does_not_destroy_content(box):
    raw = json.dumps({'cells': [raw_cell(box)], 'photo_issues': []})
    output, warnings = decode_response('student', '6.0', raw)
    assert output['cells'][0]['raw_english'] == 'aple'
    assert not output['cells'][0]['uncertain'] and output['cells'][0]['box'] is None
    assert warnings[0]['original_box'] == box
    assert json.loads(raw)['cells'][0]['box'] == box


def test_box_recovery_never_relaxes_content_or_required_geometry():
    cell = raw_cell({'x1': .2, 'y1': .8, 'x2': .5, 'y2': .3})
    cell['response_evidence'] = 'invented'
    with pytest.raises(ValidationError):
        decode_response('student', '6.0', json.dumps({'cells': [cell], 'photo_issues': []}))
    with pytest.raises((ValidationError, ValueError)):
        decode_response('student', '6.0', '{')
    with pytest.raises(ValidationError):
        decode_response('reference', '8.0', json.dumps({'items': [{'answer_zone': cell['box']}]}))


class FailingProvider(FakeProvider):
    def __init__(self, codes):
        super().__init__()
        self.codes = iter(codes)
        self.student_calls = 0

    async def infer(self, *args):
        if args[1] == 'student':
            self.student_calls += 1
            code = next(self.codes, None)
            await asyncio.sleep(.01)
            if code:
                raise Problem(code, '合成故障 ' + code)
        return await super().infer(*args)


def batch_scene(service, codes, pages=4, degree=3, writing=True):
    provider = FailingProvider(codes)
    runner, run, task = scene(service, provider, pages=pages, degree=degree, writing=writing)
    with service.store.connection(write=True) as db:
        run = service.store.put(db, 'runs', {**run, 'config': {**run['config'], 'conservative_review': False}})
    return provider, runner, run, task


def test_single_failure_siblings_finish_writing_and_failed_page_retries(service):
    provider, runner, run, task = batch_scene(service, ['OUTPUT_SCHEMA', None, None, None])
    async def go():
        await asyncio.gather(*(runner.tick() for _ in range(3)))
        await drain(runner)
    asyncio.run(go())
    pages = service.detail(task['id'])['pages']
    assert [p['state'] for p in pages].count('processed') == 3
    assert [p['state'] for p in pages].count('failed') == 1
    assert sum(c['purpose'] == 'writing' for c in provider.calls) == 3
    with service.store.connection() as db:
        assert service.store.get(db, 'runs', run['id'])['state'] == 'completed'
    runner.resume(run['id'])
    asyncio.run(drain(runner))
    assert all(p['state'] == 'processed' for p in service.detail(task['id'])['pages'])
    assert provider.student_calls == 5


def test_three_equal_errors_stop_before_fourth_dispatch_and_remain_stopped_after_restart(service):
    provider, runner, run, task = batch_scene(service, ['OUTPUT_SCHEMA'] * 5, pages=5, degree=1)
    asyncio.run(drain(runner))
    assert provider.student_calls == 3
    with service.store.connection() as db:
        stopped = service.store.get(db, 'runs', run['id'])
        assert stopped['state'] == 'paused' and stopped['error_code'] == 'REPEATED_PAGE_ERROR'
        assert stopped['generation'] == 2
        assert service.store.get(db, 'continuation_groups', run['parent_id'])['user_intent'] == 'stop'
    runner.recover(allow=True)
    assert not asyncio.run(runner.tick())
    assert len(service.detail(task['id'])['pages']) == 5


@pytest.mark.parametrize('codes', [
    ['OUTPUT_SCHEMA', 'OUTPUT_SCHEMA', None, 'OUTPUT_SCHEMA', 'OUTPUT_SCHEMA'],
    ['OUTPUT_SCHEMA', 'TURN_TIMEOUT', 'OUTPUT_SCHEMA', 'TURN_TIMEOUT', 'OUTPUT_SCHEMA'],
])
def test_success_or_different_error_breaks_consecutive_streak(service, codes):
    provider, runner, run, task = batch_scene(service, codes, pages=5, degree=1)
    asyncio.run(drain(runner))
    assert provider.student_calls == 5
    with service.store.connection() as db:
        assert service.store.get(db, 'runs', run['id'])['state'] == 'completed'


def test_three_errors_interrupt_inflight_and_discard_late_result(service):
    class Inflight(FailingProvider):
        def __init__(self):
            super().__init__([])
            self.count = 0
            self.started_all = asyncio.Event()
            self.release = asyncio.Event()
        async def infer(self, *args):
            self.count += 1
            index = self.count
            if index == 4:
                self.started_all.set()
                await self.release.wait()
                # Ignore interrupt deliberately to test the durable write fence.
                return {'output': {'cells': [], 'photo_issues': []}}
            await self.started_all.wait()
            raise Problem('OUTPUT_SCHEMA', '合成格式错误')
        async def interrupt(self, attempt_id):
            self.interruptions.append(attempt_id)
            self.release.set()
    async def go():
        provider = Inflight()
        runner, run, task = scene(service, provider, pages=5, degree=4)
        await asyncio.wait_for(asyncio.gather(*(runner.tick() for _ in range(4))), 10)
        assert provider.interruptions and provider.count == 4
        assert not await runner.tick()
        with service.store.connection() as db:
            assert not service.store.list(db, 'observations')
            assert not any(c['eligible'] for c in service.store.list(db, 'checkpoints'))
            assert service.store.get(db, 'runs', run['id'])['error_code'] == 'REPEATED_PAGE_ERROR'
    asyncio.run(go())


def test_writing_failure_retry_reuses_paid_first_pass(service):
    class WritingFails(FakeProvider):
        fail = True
        async def infer(self, *args):
            result = await super().infer(*args)
            if args[1] == 'writing' and self.fail:
                self.fail = False
                raise Problem('OUTPUT_SCHEMA', '书写返回错误')
            return result
    provider = WritingFails()
    runner, run, task = scene(service, provider, writing=True)
    with service.store.connection(write=True) as db:
        service.store.put(db, 'runs', {**run, 'config': {**run['config'], 'conservative_review': False}})
    asyncio.run(drain(runner))
    runner.resume(run['id'])
    asyncio.run(drain(runner))
    assert [c['purpose'] for c in provider.calls] == ['student', 'writing', 'writing']
    assert service.detail(task['id'])['pages'][0]['state'] == 'processed'


def test_old_failed_coordinate_response_recovers_without_new_student_call(service):
    class OldDecoder(FakeProvider):
        async def infer(self, *args):
            if args[1] == 'student':
                self.calls.append({'purpose': 'student'})
                raw = json.dumps({'cells': [raw_cell({'x1': .1, 'x2': .2, 'y1': .8, 'y2': .3})], 'photo_issues': []})
                raise Problem('OUTPUT_SCHEMA', '旧版严格校验拒绝', raw_output=raw, usage={'inputTokens': 100})
            return await super().infer(*args)
    provider = OldDecoder()
    runner, run, task = scene(service, provider, writing=True)
    with service.store.connection(write=True) as db:
        service.store.put(db, 'runs', {**run, 'config': {**run['config'], 'conservative_review': False}})
    asyncio.run(drain(runner))
    runner.resume(run['id'])
    asyncio.run(drain(runner))
    page = service.detail(task['id'])['pages'][0]
    assert page['state'] == 'processed'
    assert [c['purpose'] for c in provider.calls] == ['student', 'writing']
    with service.store.connection() as db:
        obs = service.store.get(db, 'observations', page['observation_id'])
        assert obs['geometry_warnings'][0]['question_id'] == 'q'
        assert obs['cells'][0]['box'] == page['preparation']['zones']['q']
        assert service.store.list(db, 'attempts')[0]['state'] == 'failed'


def test_local_capture_has_no_queue_and_batch_uses_later_configuration(service):
    task, group, ref, session, _ = begin(service, delivery_mode='local', cloud_consent=False)
    assert not session['config'] and not session['account'] and not session['cloud_consent_at']
    def forbidden(*args):
        raise AssertionError('Local commit must not call queue callback')
    for _ in range(4):
        frame = shot(service, session)
        data = confirmation(frame, cloud_consent=False)
        receipt = capture.commit(service, frame['id'], data, forbidden)
        assert receipt['state'] == 'saved_local'
        assert capture.commit(service, frame['id'], data, forbidden) == receipt
    with service.store.connection() as db:
        assert not service.store.list(db, 'runs') and not service.store.list(db, 'attempts')
    capture.stop(service, session['id'])
    runner = Runner(service, FakeProvider())
    result = runner.create(task['id'], {'request_id': uid(), 'group_ids': [group['id']], 'cloud_consent': True},
        {**config(), 'parallelism': 7, 'conservative_review': False})
    assert result['runs'][0]['config']['parallelism'] == 7
    assert result['runs'][0].get('source') != 'stream_capture'
    asyncio.run(drain(runner))
    assert all(p['state'] == 'processed' for p in service.detail(task['id'])['pages'])


def test_local_mode_stays_local_on_recovery_remedy_and_tail_rollback(service):
    task, group, ref, session, _ = begin(service, delivery_mode='local', cloud_consent=False)
    runner = Runner(service, FakeProvider())
    frame = shot(service, session)
    receipt = capture.commit(service, frame['id'], confirmation(frame, cloud_consent=False), runner.enqueue_capture)
    page_id = receipt['page_id']
    for action in ('hold', 'resume'):
        plan = remedy(service, runner, page_id, {'preview': True, 'action': action})
        remedy(service, runner, page_id, {'request_id': uid(), 'action': action, 'fingerprint': plan['fingerprint']})
    draft = shot(service, session)
    capture.stop(service, session['id'])
    saved = capture.frame(service, draft['id'])
    recovered = capture.recover_draft(service, saved['id'], {'request_id': uid(), 'revision': saved['revision'],
        'delivery_mode': 'local', 'cloud_consent': False}, {})
    assert recovered['session']['delivery_mode'] == 'local'
    second = capture.commit(service, recovered['frame']['id'], confirmation(recovered['frame'], cloud_consent=False), runner.enqueue_capture)
    plan = capture.withdraw_last(service, recovered['session']['id'], {'preview': True, 'frame_id': second['frame_id']})
    undone = capture.withdraw_last(service, recovered['session']['id'], {'request_id': uid(), 'frame_id': second['frame_id'], 'fingerprint': plan['fingerprint']})
    assert undone['session']['next']['slot'] == 2 and undone['session']['next']['paper_tag'] == '001'
    with service.store.connection() as db:
        assert not service.store.list(db, 'attempts') and not service.store.list(db, 'runs')


def test_stream_failure_streak_spans_idle_runs_and_retains_unconfirmed_photo(service):
    task, group, ref, session, _ = begin(service, n=1)
    provider = FailingProvider(['OUTPUT_SCHEMA'] * 4)
    runner = Runner(service, provider)
    for i in range(3):
        frame = shot(service, session)
        capture.commit(service, frame['id'], confirmation(frame), runner.enqueue_capture)
        if i == 2:
            draft = shot(service, session)
        asyncio.run(drain(runner))
    stopped = capture.read(service, session['id'])
    assert stopped['state'] == 'stopped' and stopped['active_frame']['id'] == draft['id']
    assert '3 次' in stopped['stop_reason'] and provider.student_calls == 3
    assert stopped['committed_count'] == 3


def test_later_immediate_session_does_not_auto_submit_previously_local_page(service):
    task, group, ref, session, _ = begin(service, delivery_mode='local', cloud_consent=False)
    runner = Runner(service, FakeProvider())
    frame = shot(service, session)
    local = capture.commit(service, frame['id'], confirmation(frame, cloud_consent=False), runner.enqueue_capture)
    capture.stop(service, session['id'])
    immediate = capture.start(service, task['id'], {'request_id': uid(), 'group_id': group['id'],
        'submission_id': local['submission_id'], 'cloud_consent': True}, config())
    frame = shot(service, immediate)
    receipt = capture.commit(service, frame['id'], confirmation(frame), runner.enqueue_capture)
    assert receipt['queue']['queued_page_ids'] == [receipt['page_id']]
    asyncio.run(drain(runner))
    with service.store.connection() as db:
        assert service.store.get(db, 'pages', local['page_id'])['state'] == 'pending'
        assert all(a['page_id'] != local['page_id'] for a in service.store.list(db, 'attempts'))
