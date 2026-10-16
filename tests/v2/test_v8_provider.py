import asyncio
from pathlib import Path
from types import SimpleNamespace
import pytest

from autoreview.v2.core import MachineSettings, Problem
from autoreview.v2 import provider as module


def test_source_cli_prefers_installed_catalogue_and_explicit_override(tmp_path, monkeypatch):
    installed = tmp_path / 'OpenAI/Codex/bin/current/codex.exe'
    installed.parent.mkdir(parents=True)
    installed.write_bytes(b'test executable discovery only')
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    monkeypatch.delenv('AUTOREVIEW_CODEX_EXE', raising=False)
    monkeypatch.setattr(module.sys, 'frozen', False, raising=False)
    assert Path(module.executable()) == installed
    explicit = tmp_path / 'selected.exe'
    explicit.write_bytes(b'explicit discovery only')
    monkeypatch.setenv('AUTOREVIEW_CODEX_EXE', str(explicit))
    assert Path(module.executable()) == explicit


def test_refreshed_catalogue_is_paginated_and_does_not_interrupt_primary(tmp_path, monkeypatch):
    settings = MachineSettings(tmp_path / 'machine')
    settings.save(active_profile_id='profile', profiles=[{'id': 'profile', 'name': 'test'}])
    created = []
    class FakeRpc:
        def __init__(self, home):
            self.home = home
            self.executable_path = 'test-codex.exe'
            self.server_info = {'userAgent': 'codex/0.159.2'}
            self.closed = False
            self.calls = []
            created.append(self)
        async def start(self): pass
        async def close(self): self.closed = True
        async def request(self, method, params=None, timeout=45):
            self.calls.append((method, params))
            if method == 'account/read': return {'account': {'type': 'chatgpt'}}
            if method == 'model/list':
                if not params['cursor']:
                    return {'data': [{'model': 'first-model', 'inputModalities': ['image']}], 'nextCursor': 'next'}
                return {'data': [{'model': 'new-model', 'inputModalities': ['text', 'image']}], 'nextCursor': None}
            raise Problem('NOT_SUPPORTED', 'optional')
    monkeypatch.setattr(module, 'Rpc', FakeRpc)
    async def scenario():
        provider = module.Provider(settings)
        primary = await provider.client()
        result = await provider.account(refresh_models=True)
        assert [m['model'] for m in result['models']] == ['first-model', 'new-model']
        assert result['model_catalogue']['source'] == 'codex_app_server'
        assert result['model_catalogue']['count'] == 2
        assert result['model_catalogue']['access_verified_by_inference'] is False
        assert not primary.closed
        assert created[1].closed
        assert not any(method == 'turn/start' for rpc in created for method, _ in rpc.calls)
        await provider.close()
        assert primary.closed
    asyncio.run(scenario())


def notification(method, **params):
    return {'method': method, 'params': {'threadId': 'thread', **params}}


class EventRpc:
    def __init__(self, tmp_path, events):
        self.home = tmp_path
        self.events = []
        self.upcoming = events
        self.process = SimpleNamespace(returncode=None)
        self.condition = asyncio.Condition()
        self.calls = []
    async def request(self, method, params, timeout=45):
        self.calls.append(method)
        if method == 'thread/start': return {'thread': {'id': 'thread'}}
        if method == 'turn/start':
            self.events.extend(self.upcoming)
            return {'turn': {'id': 'turn'}}
        if method == 'turn/interrupt': return {}
        raise AssertionError(method)


async def infer_events(tmp_path, events, monkeypatch, timeout=.25, purpose='reference', rpc=None, outer_timeout=1):
    settings = MachineSettings(tmp_path / 'machine')
    provider = module.Provider(settings)
    rpc = rpc or EventRpc(tmp_path, events)
    async def client(*args): return rpc
    provider.client = client
    monkeypatch.setattr(module, 'INFERENCE_TIMEOUT_SECONDS', timeout)
    import autoreview.v2.contracts_v5 as decoder
    monkeypatch.setattr(decoder, 'decode_output', lambda purpose, version, raw: {'raw': raw})
    prompt = {'schema': {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False},
              'template_text': 'offline protocol regression', 'schema_version': '8.0'}
    try:
        value = await asyncio.wait_for(provider.infer('attempt', purpose, [], prompt, {},
                      {'profile_id': 'fake', 'model': 'offline', 'effort': 'medium', 'service_tier': 'default'}), outer_timeout)
        return value, rpc, provider
    except Problem as error:
        return error, rpc, provider


def test_terminal_error_notification_fails_immediately_and_preserves_batch_usage(tmp_path, monkeypatch):
    known = {'inputTokens': 13, 'outputTokens': 2, 'totalTokens': 15}
    events = [notification('error', turnId='turn', willRetry=False,
                    error={'message': 'Unauthorized Bearer private-token teacher@example.com C:\\private\\paper.png',
                           'codexErrorInfo': 'usageLimitExceeded', 'accessToken': 'never-record'}),
              notification('thread/tokenUsage/updated', turnId='turn', tokenUsage={'last': known})]
    error, rpc, provider = asyncio.run(infer_events(tmp_path, events, monkeypatch))
    assert isinstance(error, Problem) and error.code == 'USAGE_LIMIT'
    assert error.details['usage'] == known
    assert error.details['provider_error']['willRetry'] is False
    assert error.details['provider_error']['accessToken'] == '[redacted]'
    message = error.details['provider_error']['message']
    assert 'private-token' not in message and 'teacher@example.com' not in message and 'C:\\private' not in message
    assert rpc.calls == ['thread/start', 'turn/start']
    assert provider.active_turns == {}


def test_retryable_error_keeps_same_turn_and_ignores_other_turn_completion(tmp_path, monkeypatch):
    events = [notification('error', turnId='different', willRetry=False, error={'message': 'not ours'}),
              notification('error', turnId='turn', willRetry=True, error={'message': 'temporarily disconnected'}),
              notification('turn/completed', turn={'id': 'different', 'status': 'failed', 'error': {'message': 'wrong turn'}}),
              notification('item/completed', turnId='turn', item={'id': 'message', 'type': 'agentMessage', 'text': '{}'}),
              notification('turn/completed', turn={'id': 'turn', 'status': 'completed'})]
    result, rpc, provider = asyncio.run(infer_events(tmp_path, events, monkeypatch))
    assert result['output'] == {'raw': '{}'}
    assert result['usage'] is None
    assert rpc.calls == ['thread/start', 'turn/start']
    assert provider.active_turns == {}


def test_timeout_retains_retryable_error_and_missing_usage_stays_unknown(tmp_path, monkeypatch):
    events = [notification('error', turnId='turn', willRetry=True,
              error={'message': 'stream disconnected token=private', 'codexErrorInfo': {'responseStreamDisconnected': {'httpStatusCode': 502}}})]
    error, rpc, provider = asyncio.run(infer_events(tmp_path, events, monkeypatch, timeout=.01))
    assert error.code == 'TURN_TIMEOUT'
    assert error.details['usage'] is None
    assert error.details['provider_error']['willRetry'] is True
    assert 'private' not in error.details['provider_error']['message']
    assert rpc.calls == ['thread/start', 'turn/start', 'turn/interrupt']
    assert provider.active_turns == {}


def test_completed_failure_uses_sanitized_terminal_error(tmp_path, monkeypatch):
    events = [notification('error', turnId='turn', willRetry=True, error={'message': 'prior transport recovery'}),
              notification('turn/completed', turn={'id': 'turn', 'status': 'failed',
                           'error': {'message': 'invalid JSON schema', 'password': 'never-record'}})]
    error, rpc, provider = asyncio.run(infer_events(tmp_path, events, monkeypatch))
    assert error.code == 'REQUEST_SCHEMA_INVALID'
    assert error.details['provider_error']['password'] == '[redacted]'
    assert provider.active_turns == {}


class TimedEventRpc(EventRpc):
    """Deliver real asynchronous notifications against the event loop clock."""
    def __init__(self, tmp_path, schedule):
        super().__init__(tmp_path, [])
        self.schedule = schedule
        self.producer = None

    async def request(self, method, params, timeout=45):
        result = await super().request(method, params, timeout)
        if method == 'turn/start':
            self.producer = asyncio.create_task(self.produce())
        return result

    async def produce(self):
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        for delay, events in self.schedule:
            await asyncio.sleep(max(0, started_at + delay - loop.time()))
            async with self.condition:
                self.events.extend(events)
                self.condition.notify_all()


def completed_events():
    return [notification('item/completed', turnId='turn',
                         item={'id': 'message', 'type': 'agentMessage', 'text': '{}'}),
            notification('turn/completed', turn={'id': 'turn', 'status': 'completed'})]


async def timed_inference(tmp_path, monkeypatch, schedule, purpose='reference', idle=.4, total=.9):
    monkeypatch.setattr(module, 'REFERENCE_MAX_TIMEOUT_SECONDS', total)
    monkeypatch.setattr(module, 'INFERENCE_RESCHEDULE_INTERVAL_SECONDS', .01)
    rpc = TimedEventRpc(tmp_path, schedule)
    started_at = asyncio.get_running_loop().time()
    try:
        result, rpc, provider = await infer_events(tmp_path, [], monkeypatch, timeout=idle,
                            purpose=purpose, rpc=rpc, outer_timeout=2)
        return result, rpc, provider, asyncio.get_running_loop().time() - started_at
    finally:
        if rpc.producer:
            rpc.producer.cancel()
            await asyncio.gather(rpc.producer, return_exceptions=True)


@pytest.mark.parametrize('method', ['item/agentMessage/delta', 'item/reasoning/textDelta',
                                    'item/reasoning/summaryTextDelta'])
def test_reference_real_text_activity_finishes_after_initial_deadline(tmp_path, monkeypatch, method):
    schedule = [(.2, [notification(method, turnId='turn', itemId='message', delta='actual text')]),
                (.5, completed_events())]
    result, rpc, provider, elapsed = asyncio.run(timed_inference(tmp_path, monkeypatch, schedule))
    assert result['output'] == {'raw': '{}'}
    assert elapsed >= .4  # Completion genuinely crosses the original idle limit.
    assert rpc.calls == ['thread/start', 'turn/start']
    assert provider.active_turns == {}


def test_reference_empty_deltas_and_notifications_do_not_extend_deadline(tmp_path, monkeypatch):
    schedule = [(.1, [notification('item/agentMessage/delta', turnId='turn', delta='')]),
                (.2, [notification('item/reasoning/textDelta', turnId='turn', delta='  \n'),
                      notification('error', turnId='turn', willRetry=True, error={'message': 'retrying'})]),
                (.3, [notification('item/reasoning/summaryTextDelta', turnId='turn', delta=None),
                      notification('model/rerouted', turnId='turn', toModel='offline'),
                      notification('item/agentMessage/delta', turnId='different', delta='another turn')]),
                (.5, completed_events())]
    error, rpc, provider, elapsed = asyncio.run(timed_inference(tmp_path, monkeypatch, schedule))
    assert error.code == 'TURN_TIMEOUT'
    assert .38 <= elapsed < .7
    assert error.details['usage'] is None
    assert error.details['provider_error']['willRetry'] is True
    assert rpc.calls == ['thread/start', 'turn/start', 'turn/interrupt']
    assert provider.active_turns == {}


def test_reference_continuous_real_activity_still_hits_total_cap(tmp_path, monkeypatch):
    known = {'inputTokens': 3, 'outputTokens': 4, 'totalTokens': 7}
    schedule = [(.05, [notification('thread/tokenUsage/updated', turnId='turn', tokenUsage={'last': known}),
                       notification('error', turnId='turn', willRetry=True, error={'message': 'retrying'})])]
    schedule.extend((index / 10, [notification('item/agentMessage/delta', turnId='turn', delta='text')])
                    for index in range(1, 9))
    schedule.append((.85, completed_events()))
    error, rpc, provider, elapsed = asyncio.run(timed_inference(tmp_path, monkeypatch, schedule, idle=.25, total=.7))
    assert error.code == 'TURN_TIMEOUT'
    assert .68 <= elapsed < 1
    assert error.details['usage'] == known
    assert error.details['provider_error']['willRetry'] is True
    assert rpc.calls == ['thread/start', 'turn/start', 'turn/interrupt']
    assert provider.active_turns == {}


def test_student_real_text_activity_does_not_extend_deadline(tmp_path, monkeypatch):
    schedule = [(.2, [notification('item/agentMessage/delta', turnId='turn', delta='actual text')]),
                (.5, completed_events())]
    error, rpc, provider, elapsed = asyncio.run(timed_inference(tmp_path, monkeypatch, schedule, purpose='student'))
    assert error.code == 'TURN_TIMEOUT'
    assert .38 <= elapsed < .7
    assert rpc.calls == ['thread/start', 'turn/start', 'turn/interrupt']
    assert provider.active_turns == {}
