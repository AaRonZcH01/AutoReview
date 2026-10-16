import asyncio
import io
import json
import sqlite3
from pathlib import Path
import pytest
from PIL import Image
from autoreview.v2.core import MachineSettings, Store, Problem, uid
from autoreview.v2.service import Service
from autoreview.v2.runner import Runner
from autoreview.v2.reports import statistics, metrics, export_report, export_diagnostics
from autoreview.v2.contracts import MatchingRule, MatchingEdge
from autoreview.v2.grading import grade_matching
from autoreview.v2.telemetry import sanitize
from autoreview.v2.api import create_app
from fastapi.testclient import TestClient


@pytest.fixture
def service(tmp_path):
    settings = MachineSettings(tmp_path / 'machine')
    settings.save(library_location=str(tmp_path / 'library'), export_directory=str(tmp_path / 'exports'))
    result = Service(settings)
    yield result
    result.close()


def setup(service, n=2):
    task = service.create('tasks', {'name': 'Unit 2'})
    groups = [service.create('groups', {'name': name, 'parent_id': task['id']}) for name in ('三1', '三2')]
    draft = service.draft(task['id'], {'page_count': n, 'writing_rules': [], 'items': [
        {'id': f'q{i}', 'number': '1', 'slot': i, 'english': 'apple', 'uncertain': False} for i in range(1, n + 1)]})
    reference = service.confirm_reference(task['id'], draft['revision'])
    return task, groups, reference


def images(service, count):
    stream = io.BytesIO()
    Image.new('RGB', (240, 180), 'white').save(stream, 'PNG')
    return [service.save_asset(stream.getvalue(), f'page{i}.png')['id'] for i in range(count)]


def intake(service, group, count, **extra):
    return service.intake(group['id'], {'asset_ids': images(service, count), 'request_id': uid(), **extra})


class FakeProvider:
    def __init__(self):
        self.calls = []
        self.interruptions = []
        self.started = None
        self.release = None
        self.failure = None

    async def infer(self, attempt_id, purpose, paths, prompt, variables, config, on_started):
        self.calls.append({'purpose': purpose, 'variables': variables, 'config': config})
        on_started('thread-id', 'turn-id')
        if self.started:
            self.started.set()
            await self.release.wait()
        if self.failure:
            raise self.failure
        if purpose == 'reference':
            output = {'items': [{'slot': 1, 'number': '1', 'english': 'apple', 'uncertain': False}], 'page_labels': ['正面'], 'issues': []}
        elif purpose == 'writing':
            output = {'items': []}
        else:
            output = {'cells': [{'question_id': a['id'], 'number': a['number'], 'slot': a['slot'],
                'raw_english': 'aple', 'uncertain': False, 'box': {'x1': .1, 'y1': .2, 'x2': .5, 'y2': .4}}
                for a in variables['structure']], 'photo_issues': []}
        return {'output': output, 'usage': {'inputTokens': 100, 'outputTokens': 20}, 'actual_model': None, 'actual_service_tier': None}

    async def interrupt(self, attempt_id):
        self.interruptions.append(attempt_id)

    async def close(self):
        pass


def config():
    return {'profile_id': 'fake', 'model': 'test-model', 'effort': 'medium', 'speed': 'standard', 'service_tier': 'default'}


def enqueue(runner, task, groups, **extra):
    # Synthetic fixtures explicitly perform the v0.3 teacher preview step.
    selected = {g['id'] for g in groups}
    for page in runner.service.detail(task['id'])['pages']:
        if page['group_id'] in selected:
            prepared = runner.service.prepare_page(page['id'], {'reference_version_id': extra.get('reference_version_id')})
            runner.service.prepare_page(page['id'], {'action': 'confirm', 'revision': prepared['revision'],
                'stamp': prepared['preparation']['stamp'], 'reference_version_id': extra.get('reference_version_id')})
    return runner.create(task['id'], {'request_id': uid(), 'group_ids': [g['id'] for g in groups],
                        'cloud_consent': True, 'images_prepared': True, **extra}, config())['runs'][0]


async def drain(runner, limit=30):
    for _ in range(limit):
        if not await runner.tick():
            return
    pytest.fail('queue did not settle')


def test_reference_gate_and_immutable_versions(service):
    task = service.create('tasks', {'name': 'Gate'})
    group = service.create('groups', {'name': '三1', 'parent_id': task['id']})
    with pytest.raises(Problem, match='确认参考答案'):
        intake(service, group, 1)
    draft = service.draft(task['id'], {'items': [{'id': 'q', 'number': '1', 'slot': 1, 'english': 'apple', 'uncertain': True}]})
    with pytest.raises(Problem, match='不确定'):
        service.confirm_reference(task['id'], draft['revision'])
    draft = service.draft(task['id'], {'items': [{**draft['items'][0], 'uncertain': False}]})
    version = service.confirm_reference(task['id'], draft['revision'])
    with service.store.connection(write=True) as db:
        with pytest.raises(Problem, match='历史版本'):
            service.store.put(db, 'reference_versions', {**version, 'items': []})
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('UPDATE reference_versions SET state=? WHERE id=?', ('draft', version['id']))


def test_numbering_preview_idempotency_highwater(service):
    task, groups, _ = setup(service)
    request = {'asset_ids': images(service, 5), 'request_id': uid(), 'mode': 'whole_submission'}
    preview = service.intake(groups[0]['id'], {**request, 'preview': True})
    assert [(e['paper_tag'], e['slot']) for e in preview['manifest']] == [('001', 1), ('001', 2), ('002', 1), ('002', 2), ('003', 1)]
    assert preview['incomplete']
    committed = service.intake(groups[0]['id'], request)
    assert service.intake(groups[0]['id'], request) == committed
    assert len(service.detail(task['id'])['pages']) == 5
    next_import = intake(service, groups[0], 2)
    assert next_import['manifest'][0]['paper_tag'] == '004'


def test_single_side_reverse_missing_positions(service):
    task, groups, _ = setup(service)
    first = intake(service, groups[0], 3, mode='single_page', slot=1)
    second = intake(service, groups[0], 2, mode='single_page', slot=2,
                    cohort_id=first['cohort']['id'], reverse_cohort=True, cohort_positions=[0, 2])
    assert [e['paper_tag'] for e in second['manifest']] == ['003', '001']
    runner = Runner(service, FakeProvider())
    with pytest.raises(Problem, match='全部页面'):
        enqueue(runner, task, groups[:1])
    run = enqueue(runner, task, groups[:1], slot=1)
    asyncio.run(drain(runner))
    assert service.detail(task['id'])['runs'][0]['state'] == 'completed'
    assert all(s['state'] != 'processed' for s in service.detail(task['id'])['submissions'])


def test_review_undo_statistics_and_exports(service):
    task, groups, _ = setup(service, 1)
    intake(service, groups[0], 2)
    provider = FakeProvider()
    runner = Runner(service, provider)
    enqueue(runner, task, groups[:1])
    asyncio.run(drain(runner))
    assert 'english' not in json.dumps(provider.calls[0]['variables'])
    pages = service.detail(task['id'])['pages']
    assert pages[0]['results'][0]['machine_raw_english'] == 'aple'
    page = service.review(pages[0]['id'], {'revision': pages[0]['revision'], 'question_id': 'q1',
        'change': {'raw_english': 'apple', 'status': 'correct'}, 'category': 'transcription'})
    page = service.review(page['id'], {'revision': page['revision'], 'action': 'undo'})
    with service.store.connection() as db:
        assert service.results(db, page)[0]['status'] == 'incorrect'
    page = service.review(page['id'], {'revision': page['revision'], 'action': 'redo'})
    page = service.review(page['id'], {'revision': page['revision'], 'action': 'confirm'})
    other = pages[1]
    service.review(other['id'], {'revision': other['revision'], 'action': 'confirm'})
    stats = statistics(service, task['id'])
    assert stats['known'] == 2 and stats['errors'] == 1 and stats['error_rate'] == .5
    for kind in ('csv', 'xlsx', 'json', 'pdf', 'images'):
        record = export_report(service, task['id'], {'format': kind})
        assert Path(record['output_path']).stat().st_size > 20
    log = Path(export_diagnostics(service, task['id'])['path']).read_text('utf-8')
    assert 'aple' not in log and 'apple' not in log
    assert metrics(service, task['id'])['summary']['input_tokens'] == 200


def test_stop_late_result_and_restart_does_not_resume(service):
    task, groups, _ = setup(service, 1)
    intake(service, groups[0], 2)
    async def scenario():
        provider = FakeProvider()
        provider.started, provider.release = asyncio.Event(), asyncio.Event()
        runner = Runner(service, provider)
        run = enqueue(runner, task, groups[:1])
        pending = asyncio.create_task(runner.tick())
        await provider.started.wait()
        await runner.stop(run['id'])
        provider.release.set()
        await pending
        assert provider.interruptions
        assert all(p['state'] == 'pending' for p in service.detail(task['id'])['pages'])
        runner.recover()
        assert not await runner.tick()
        runner.resume(run['id'])
        await drain(runner)
        assert len(provider.calls) == 3  # stopped late checkpoint cannot become a success
    asyncio.run(scenario())


def test_replace_running_fences_old_result_and_preserves_order(service):
    task, groups, _ = setup(service, 1)
    imported = intake(service, groups[0], 2)
    async def scenario():
        provider = FakeProvider()
        provider.started, provider.release = asyncio.Event(), asyncio.Event()
        runner = Runner(service, provider)
        enqueue(runner, task, groups[:1])
        pending = asyncio.create_task(runner.tick())
        await provider.started.wait()
        page = service.detail(task['id'])['pages'][0]
        replacement = service.replace_page(page['id'], {'revision': page['revision'], 'asset_id': images(service, 1)[0]})
        assert replacement['seq'] == page['seq']
        provider.release.set()
        await pending
        assert service.detail(task['id'])['pages'][0]['state'] == 'pending'
        await drain(runner)
        assert len(provider.calls) == 2  # Replacement cannot bypass a fresh preview.
        enqueue(runner, task, groups[:1])
        await drain(runner)
        assert len(provider.calls) == 3
        assert all(p['state'] == 'processed' for p in service.detail(task['id'])['pages'])
    asyncio.run(scenario())


def test_checkpoint_crash_adoption_without_second_call(service):
    task, groups, _ = setup(service, 1)
    intake(service, groups[0], 1)
    provider = FakeProvider()
    runner = Runner(service, provider)
    run = enqueue(runner, task, groups[:1])
    # Crash after durable model checkpoint and before page application.
    real_put = service.store.put
    def crash(db, table, data, **kwargs):
        if table == 'observations':
            raise KeyboardInterrupt('simulated process loss')
        return real_put(db, table, data, **kwargs)
    service.store.put = crash
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(runner.tick())
    service.store.put = real_put
    runner.recover()
    asyncio.run(drain(runner))
    assert len(provider.calls) == 1
    with service.store.connection() as db:
        assert len(service.store.list(db, 'recovery_adoptions')) == 1
    assert service.detail(task['id'])['pages'][0]['state'] == 'processed'


def test_local_regrade_no_cloud_and_old_class_version_preserved(service):
    task, groups, old = setup(service, 1)
    for group in groups:
        intake(service, group, 1)
    provider = FakeProvider()
    runner = Runner(service, provider)
    enqueue(runner, task, groups)
    asyncio.run(drain(runner))
    draft = service.draft(task['id'], {'items': [{'id': 'q1', 'slot': 1, 'number': '1', 'english': 'aple'}],
        'writing_rules': [{'id': 'hooked-y', 'description': 'hooked y'}]})
    new = service.confirm_reference(task['id'], draft['revision'])
    enqueue(runner, task, groups[:1], reference_version_id=new['id'], regrade_all=True, reuse_transcription=True)
    asyncio.run(drain(runner))
    assert len(provider.calls) == 2
    detail = service.detail(task['id'])
    assert detail['groups'][1]['reference_version_id'] == new['id']  # v0.7 defaults follow task official version; prior page snapshots stay frozen
    assert detail['pages'][0]['results'][0]['status'] == 'correct'
    assert detail['pages'][0]['results'][0]['writing_status'] == 'pending'


def test_failure_usage_not_lost(service):
    task, groups, _ = setup(service, 1)
    intake(service, groups[0], 1)
    provider = FakeProvider()
    provider.failure = Problem('TEST_FAILURE', 'mock failure', usage={'inputTokens': 15})
    runner = Runner(service, provider)
    enqueue(runner, task, groups[:1])
    asyncio.run(runner.tick())
    result = metrics(service, task['id'])
    assert result['summary']['failed_count'] == 1
    assert result['summary']['input_tokens'] == 15
    assert result['summary']['output_tokens'] is None
    assert service.diagnostics.rows()[0]['error_code'] == 'TEST_FAILURE'


def test_matching_crossings_unknown_erase_and_cardinality():
    rule = MatchingRule(nodes=[{'id': x, 'group': x[0], 'slot': 1} for x in ('L1', 'L2', 'R1', 'R2')],
                        allowed_edges=[('L1', 'R2'), ('L2', 'R1')])
    edge = lambda n, a, b, **kw: MatchingEdge(id=n, from_node=a, to_node=b, **kw)
    good = [edge('1', 'L1', 'R2'), edge('2', 'L2', 'R1')]
    assert grade_matching(rule, good)['status'] == 'correct'
    assert grade_matching(rule, good + [edge('3', 'L1', 'R1', erased=True)])['status'] == 'correct'
    ambiguous = grade_matching(rule, [good[0], edge('2', 'L2', None, uncertain=True)])
    assert ambiguous['status'] == 'unknown' and ambiguous['missing_edges'] == []
    assert grade_matching(rule, good + [edge('3', 'L1', 'R1')])['cardinality_conflicts']
    reverse = [edge('1', 'R2', 'L1'), edge('2', 'R1', 'L2')]
    assert grade_matching(rule, reverse)['status'] == 'correct'


def test_log_redaction():
    result = json.dumps(sanitize({'email': 'teacher@example.com', 'authorization': 'Bearer abc123',
        'raw_english': 'private spelling', 'message': 'https://auth.openai.com/?code=abc teacher@example.com C:\\Users\\Name\\secret.png'}))
    for value in ('teacher@example.com', 'abc123', 'private spelling', 'code=abc', 'Name'):
        assert value not in result


def test_http_nonce_origin_backup_and_dynamic_library(tmp_path):
    settings = MachineSettings(tmp_path / 'machine')
    settings.save(library_location=str(tmp_path / 'library'), export_directory=str(tmp_path / 'exports'))
    app = create_app(settings, FakeProvider(), nonce='test-secret', start_worker=False)
    with TestClient(app) as client:
        assert client.get('/api/state').status_code == 401
        client.headers['X-AutoReview-Session'] = 'test-secret'
        assert client.get('/api/state').status_code == 200
        assert client.post('/api/settings', json={}, headers={'Origin': 'https://evil.test'}).status_code == 403
        assert client.post('/api/entities/tasks', json={'name': 'HTTP test'}).status_code == 200
        result = client.post('/api/library/backup', json={})
        assert result.status_code == 200
        assert Path(result.json()['path']).exists()
