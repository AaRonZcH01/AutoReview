"""Concurrency uses local protocol fakes only; no official CLI/model calls."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from autoreview.v2 import provider as module
from autoreview.v2.core import MachineSettings, Problem

PROMPT = {'schema': {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False},
          'template_text': 'offline event isolation', 'schema_version': '8.0'}
CONFIG = {'profile_id': 'fake', 'model': 'offline', 'effort': 'medium', 'service_tier': 'default'}


def event(thread, method, **params):
    return {'method': method, 'params': {'threadId': thread, **params}}


def completed(rpc, thread, *, token=1):
    turn = f'turn-{thread}'
    rpc.publish(event(thread, 'item/completed', turnId=turn,
                      item={'id': 'message', 'type': 'agentMessage', 'text': thread}))
    rpc.publish(event(thread, 'turn/completed', turn={'id': turn, 'status': 'completed'}))
    # Deliberately arrives after completion in the already-received batch.
    rpc.publish(event(thread, 'thread/tokenUsage/updated', turnId=turn,
                      tokenUsage={'last': {'totalTokens': token}}))


class OfflineRpc(module.Rpc):
    def __init__(self, root, *, finish_before_start_response=False):
        super().__init__(root)
        self.disconnected = False
        self.process = SimpleNamespace(returncode=None)
        self.count = 0
        self.started = []
        self.interrupted = []
        self.changed = asyncio.Event()
        self.finish_before_start_response = finish_before_start_response

    async def request(self, method, params=None, timeout=45):
        if method == 'thread/start':
            self.count += 1
            return {'thread': {'id': f'thread-{self.count}'}}
        if method == 'turn/start':
            thread = params['threadId']
            self.started.append(thread)
            self.changed.set()
            if self.finish_before_start_response:
                completed(self, thread, token=17)
                await asyncio.sleep(0)
            return {'turn': {'id': f'turn-{thread}'}}
        if method == 'turn/interrupt':
            self.interrupted.append(params)
            return {}
        raise AssertionError(method)

    async def wait_started(self, count):
        while len(self.started) < count:
            self.changed.clear()
            await asyncio.wait_for(self.changed.wait(), 1)


def setup(tmp_path, monkeypatch, **kwargs):
    rpc = OfflineRpc(tmp_path, **kwargs)
    provider = module.Provider(MachineSettings(tmp_path/'machine'))
    async def client(*args): return rpc
    provider.client = client
    import autoreview.v2.contracts_v5 as decoder
    monkeypatch.setattr(decoder, 'decode_output', lambda purpose, version, raw: {'raw': raw})
    return provider, rpc


def infer(provider, attempt, **callbacks):
    return asyncio.create_task(provider.infer(attempt, 'student', [], PROMPT, {}, CONFIG, **callbacks))


def test_four_concurrent_turns_receive_only_their_own_results_and_usage(tmp_path, monkeypatch):
    async def scenario():
        provider, rpc = setup(tmp_path, monkeypatch)
        jobs = [infer(provider, f'attempt-{i}') for i in range(4)]
        await rpc.wait_started(4)
        for i in range(1000):
            rpc.publish({'method': 'account/updated', 'params': {'index': i}})
        for i in range(200):
            for thread in rpc.started:
                rpc.publish(event(thread, 'item/reasoning/textDelta', turnId=f'turn-{thread}', delta='text'))
        for thread in (rpc.started[3], rpc.started[1], rpc.started[0], rpc.started[2]):
            rpc.publish(event(thread, 'error', turnId='different-turn', willRetry=False, error={'message':'not ours'}))
            completed(rpc, thread, token=int(thread[-1])*11)
        results = await asyncio.wait_for(asyncio.gather(*jobs), 1)
        assert [result['output']['raw'] for result in results] == rpc.started
        assert [result['usage']['totalTokens'] for result in results] == [11,22,33,44]
        assert len(rpc.events) == 64 and all(item['method']=='account/updated' for item in rpc.events)
        assert rpc.subscribers == {} and provider.inboxes == {} and provider.active_turns == {}
        assert not rpc.interrupted
    asyncio.run(scenario())


def test_subscription_catches_completion_before_turn_start_response(tmp_path, monkeypatch):
    async def scenario():
        provider, rpc = setup(tmp_path, monkeypatch, finish_before_start_response=True)
        result = await asyncio.wait_for(infer(provider, 'early'), 1)
        assert result['output']['raw'] == 'thread-1' and result['usage'] == {'totalTokens':17}
        assert rpc.subscribers == {}
    asyncio.run(scenario())


def test_cancel_one_turn_interrupts_and_releases_only_its_subscription(tmp_path, monkeypatch):
    async def scenario():
        provider, rpc = setup(tmp_path, monkeypatch)
        jobs = [infer(provider, f'attempt-{i}') for i in range(4)]
        await rpc.wait_started(4)
        jobs[1].cancel()
        with pytest.raises(asyncio.CancelledError):
            await jobs[1]
        assert rpc.interrupted == [{'threadId':'thread-2','turnId':'turn-thread-2'}]
        assert 'thread-2' not in rpc.subscribers
        completed(rpc, 'thread-2', token=999)  # Late canceled notifications have no consumer.
        for index in (0,2,3):
            completed(rpc, rpc.started[index], token=index)
        results = await asyncio.wait_for(asyncio.gather(jobs[0],jobs[2],jobs[3]), 1)
        assert [result['usage']['totalTokens'] for result in results] == [0,2,3]
        assert provider.active_turns == {} and provider.cancelled == set() and rpc.subscribers == {}
    asyncio.run(scenario())


def test_explicit_interrupt_does_not_wait_for_silent_terminal_event(tmp_path, monkeypatch):
    async def scenario():
        provider, rpc = setup(tmp_path, monkeypatch)
        job = infer(provider,'one')
        await rpc.wait_started(1)
        rpc.publish(event('thread-1','thread/tokenUsage/updated',turnId='turn-thread-1',tokenUsage={'last':{'totalTokens':9}}))
        await provider.interrupt('one')
        with pytest.raises(Problem) as caught:
            await asyncio.wait_for(job,.5)
        assert caught.value.code == 'TURN_INTERRUPTED' and caught.value.details['usage'] == {'totalTokens':9}
        assert rpc.subscribers == {} and provider.active_turns == {} and provider.cancelled == set()
    asyncio.run(scenario())


def test_disconnect_wakes_all_inferences_without_waiting_for_process_returncode(tmp_path, monkeypatch):
    async def scenario():
        provider, rpc = setup(tmp_path, monkeypatch)
        jobs = [infer(provider,f'attempt-{i}') for i in range(3)]
        await rpc.wait_started(3)
        rpc.publish(event('thread-2','thread/tokenUsage/updated',turnId='turn-thread-2',tokenUsage={'last':{'totalTokens':12}}))
        await rpc.disconnected_now()
        results = await asyncio.wait_for(asyncio.gather(*jobs,return_exceptions=True),.5)
        assert rpc.process.returncode is None  # EOF can precede subprocess bookkeeping.
        assert all(isinstance(result,Problem) and result.code=='CODEX_DISCONNECTED' for result in results)
        assert [result.details['usage'] for result in results] == [None,{'totalTokens':12},None]
        assert rpc.subscribers == {} and provider.active_turns == {}
    asyncio.run(scenario())


def test_already_received_completion_and_usage_survive_immediate_eof(tmp_path, monkeypatch):
    async def scenario():
        provider, rpc = setup(tmp_path, monkeypatch)
        job = infer(provider,'one')
        await rpc.wait_started(1)
        completed(rpc,'thread-1',token=23)
        await rpc.disconnected_now()
        result = await asyncio.wait_for(job,.5)
        assert result['usage']=={'totalTokens':23} and result['output']['raw']=='thread-1'
        assert rpc.subscribers == {}
    asyncio.run(scenario())


def test_setup_callback_failure_interrupts_the_started_turn_and_cleans_subscription(tmp_path, monkeypatch):
    async def scenario():
        provider,rpc=setup(tmp_path,monkeypatch)
        def fail(*args): raise RuntimeError('simulated local state failure')
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(infer(provider,'one',on_started=fail),1)
        assert rpc.interrupted == [{'threadId':'thread-1','turnId':'turn-thread-1'}]
        assert rpc.subscribers == {} and provider.active_turns == {} and provider.cancelled == set()
    asyncio.run(scenario())


def test_rpc_reader_routes_notifications_and_wakes_rpc_futures_on_eof(tmp_path):
    async def scenario():
        rpc=module.Rpc(tmp_path)
        stream=asyncio.StreamReader()
        rpc.process=SimpleNamespace(stdout=stream,returncode=None)
        rpc.disconnected=False
        first=rpc.subscribe('a');second=rpc.subscribe('b')
        response=asyncio.get_running_loop().create_future()
        lost=asyncio.get_running_loop().create_future()
        rpc.pending={1:response,2:lost}
        notifications=[event('a','thread/tokenUsage/updated',tokenUsage={'last':{'totalTokens':7}}),
                       event('b','turn/completed',turn={'id':'turn-b','status':'completed'})]
        for item in [{'id':1,'result':{'ok':True}},[],*notifications]:
            stream.feed_data((json.dumps(item)+'\n').encode())
        stream.feed_eof()
        await asyncio.wait_for(rpc.read(),.5)
        assert await response=={'ok':True}
        with pytest.raises(Problem) as caught: await lost
        assert caught.value.code=='CODEX_DISCONNECTED'
        assert await first.receive()==notifications[:1]
        assert await second.receive()==notifications[1:]
        with pytest.raises(Problem): await first.receive()
        assert not rpc.events
    asyncio.run(scenario())
