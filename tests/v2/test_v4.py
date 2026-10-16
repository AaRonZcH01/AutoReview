import asyncio
from datetime import datetime, timedelta, timezone
import pytest
from autoreview.v2.core import Problem, uid
from autoreview.v2.contracts import OUTPUT_MODELS, strict_schema, validate_output_schema
from autoreview.v2.runner import Runner
from autoreview.v2.reports import metrics
from test_workflow import service, setup, images, intake, FakeProvider, enqueue, drain


def test_wire_schemas_have_supported_tuple_arrays():
    for model in OUTPUT_MODELS.values():
        schema = strict_schema(model)
        validate_output_schema(schema)
        assert 'prefixItems' not in str(schema)
    pair = strict_schema(OUTPUT_MODELS['reference'])['$defs']['MatchingRule']['properties']['allowed_edges']['items']
    assert pair == {'type': 'array', 'items': {'type': 'string'}, 'minItems': 2, 'maxItems': 2}
    with pytest.raises(Problem, match='格式'):
        validate_output_schema({'type': 'array', 'prefixItems': [{'type': 'string'}]})


def test_local_staging_without_reference_and_revision_fence(service):
    task = service.create('tasks', {'name': '待确认'})
    group = service.create('groups', {'name': '1班', 'parent_id': task['id']})
    assets = images(service, 2)
    draft = service.intake_draft(task['id'], {'revision': 0, 'assets': [{'id': i} for i in assets], 'form': {'group_id': group['id']}})
    assert [a['id'] for a in service.intake_draft(task['id'])['assets']] == assets
    assert not service.detail(task['id'])['pages']
    with pytest.raises(Problem, match='其他窗口'):
        service.intake_draft(task['id'], {'revision': 0, 'assets': []})
    assert draft['revision'] == 1


def test_preview_fingerprint_blocks_changed_allocation(service):
    task, groups, _ = setup(service)
    data = {'asset_ids': images(service, 2), 'request_id': uid()}
    preview = service.intake(groups[0]['id'], {**data, 'preview': True})
    intake(service, groups[0], 2)
    with pytest.raises(Problem, match='编排已变化'):
        service.intake(groups[0]['id'], {**data, 'fingerprint': preview['fingerprint']})
    assert len(service.detail(task['id'])['submissions']) == 1


def test_preset_addition_is_atomic_idempotent_and_local(service):
    task = service.create('tasks', {'name': '班级'})
    preset = service.create('class_presets', {'name': '1班'})
    request = {'preset_ids': [preset['id']], 'custom': '2班', 'save_preset': True, 'request_id': uid()}
    first = service.add_groups(task['id'], request)
    assert service.add_groups(task['id'], request) == first
    assert len(first['groups']) == 2
    group = first['groups'][0]
    service.edit('groups', group['id'], {'name': '临时名称'})
    assert next(p for p in service.snapshot()['class_presets'] if p['id'] == preset['id'])['name'] == '1班'


def test_folder_trash_cascade_restore_independent_child(service):
    folder = service.create('folders', {'name': 'Unit'})
    a = service.create('tasks', {'name': 'A', 'parent_id': folder['id']})
    b = service.create('tasks', {'name': 'B', 'parent_id': folder['id']})
    independent = service.edit('tasks', b['id'], {'state': 'deleted'})
    whole = service.edit('folders', folder['id'], {'state': 'deleted'})
    assert whole['counts']['tasks'] == 1
    with pytest.raises(Problem, match='独立'):
        service.trash.preview(whole['id'])
    service.trash.restore(whole['id'])
    assert service.detail(a['id'])['task']['state'] == 'active'
    assert service.detail(b['id'])['task']['state'] == 'deleted'
    service.trash.restore(independent['id'])
    assert len(service.snapshot()['tasks']) == 2


def test_purge_shared_original_and_cost_history(service):
    task, groups, _ = setup(service, n=1)
    shared = images(service, 1)[0]
    for group in groups:
        service.intake(group['id'], {'asset_ids': [shared], 'request_id': uid()})
    runner = Runner(service, FakeProvider())
    enqueue(runner, task, groups)
    asyncio.run(drain(runner))
    before = metrics(service)['summary']['input_tokens']
    path = service.asset(shared, 'original')
    entry = service.edit('groups', groups[0]['id'], {'state': 'deleted'})
    service.trash.purge(entry['id'])
    assert path.exists()
    assert len(service.detail(task['id'])['pages']) == 1
    assert metrics(service)['summary']['input_tokens'] == before
    entry = service.edit('tasks', task['id'], {'state': 'deleted'})
    service.trash.purge(entry['id'])
    assert not path.exists()
    assert not service.snapshot()['tasks']
    assert metrics(service)['summary']['input_tokens'] == before
    with service.store.connection() as db:
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []


def test_expiry_and_legacy_grace(service):
    task = service.create('tasks', {'name': '旧记录'})
    with service.store.connection(write=True) as db:
        service.store.put(db, 'tasks', {**task, 'state': 'deleted'})
    service.trash.adopt_legacy()
    entry = service.trash.rows()['items'][0]
    assert entry['legacy_grace']
    assert datetime.fromisoformat(entry['expires_at']) > datetime.now(timezone.utc) + timedelta(days=29)
    service.trash.sweep()
    assert service.trash.rows()['items']
    with service.store.connection(write=True) as db:
        service.store.put(db, 'trash_entries', {**entry, 'expires_at': (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()})
    service.trash.sweep()
    assert not service.trash.rows()['items']


def test_deleted_task_cannot_resume_or_publish(service):
    task, groups, _ = setup(service, n=1)
    intake(service, groups[0], 1)
    runner = Runner(service, FakeProvider())
    run = enqueue(runner, task, groups[:1])
    asyncio.run(runner.stop(run['id']))
    service.edit('tasks', task['id'], {'state': 'deleted'})
    with pytest.raises(Problem, match='回收站'):
        runner.resume(run['id'])
    with service.store.connection() as db:
        item = service.store.list(db, 'work_items', parent_id=run['id'])[0]
        assert not runner.valid(db, run, item)


def test_clear_all_orders_independent_descendants_and_shared_assets(service):
    folder = service.create('folders', {'name': '上级'})
    task = service.create('tasks', {'name': '子任务', 'parent_id': folder['id']})
    child = service.trash.move('tasks', task['id'])
    parent = service.trash.move('folders', folder['id'])
    preview = service.trash.preview_many([parent['id'], child['id']])
    assert [e['id'] for e in preview['items']] == [child['id'], parent['id']]
    service.trash.purge_many([parent['id'], child['id']])
    assert not service.trash.rows()['items']
    with service.store.connection() as db:
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []


def test_purge_retry_preserves_outside_files(service, monkeypatch, tmp_path):
    from pathlib import Path
    task, groups, _ = setup(service, n=1)
    asset = images(service, 1)[0]
    service.intake(groups[0]['id'], {'asset_ids': [asset], 'request_id': uid()})
    image = service.asset(asset, 'original')
    outside = tmp_path / 'original-outside.png'
    outside.write_bytes(image.read_bytes())
    real_unlink = Path.unlink
    def locked(path, *args, **kwargs):
        if path == image:
            raise PermissionError('test locked file')
        return real_unlink(path, *args, **kwargs)
    entry = service.trash.move('tasks', task['id'])
    monkeypatch.setattr(Path, 'unlink', locked)
    service.trash.purge(entry['id'])
    assert service.trash.rows()['items'][0]['state'] == 'purging'
    assert image.exists() and outside.exists()
    monkeypatch.setattr(Path, 'unlink', real_unlink)
    service.trash.finish_jobs()
    assert not image.exists() and outside.exists()
    assert not service.trash.rows()['items']


def test_provider_preflight_and_sanitized_schema_failure(service):
    from autoreview.v2.provider import Provider
    provider = Provider(service.settings)
    async def forbidden_client(profile):
        raise AssertionError('invalid schema must not reach CLI')
    provider.client = forbidden_client
    with pytest.raises(Problem) as invalid:
        asyncio.run(provider.infer('invalid', 'reference', [], {'schema': {'type': 'array'}}, {}, {}))
    assert invalid.value.code == 'REQUEST_SCHEMA_INVALID'
    class Client:
        home = service.settings.home
        events = []
        condition = asyncio.Condition()
        process = type('Process', (), {'returncode': None})()
        async def request(self, method, params, **kw):
            if method == 'thread/start':
                return {'thread': {'id': 'thread'}}
            if method == 'turn/start':
                self.events = [{'method': 'turn/completed', 'params': {'threadId': 'thread', 'turnId': 'turn',
                    'turn': {'status': 'failed', 'error': {'message': 'Invalid schema: array missing items'}}}}]
                return {'turn': {'id': 'turn'}}
            raise AssertionError(method)
    async def fake_client(profile):
        return Client()
    provider.client = fake_client
    prompt = {'schema': strict_schema(OUTPUT_MODELS['reference']), 'template_text': 'test'}
    with pytest.raises(Problem) as failed:
        asyncio.run(provider.infer('test', 'reference', [], prompt, {}, {
            'profile_id': 'offline', 'model': 'offline', 'effort': 'high', 'service_tier': None}))
    assert failed.value.code == 'REQUEST_SCHEMA_INVALID'
    assert 'missing items' in failed.value.details['provider_error']['message']
    assert failed.value.details['usage'] is None


def test_purge_unused_derived_image_family_but_protect_shared_derivative(service):
    task, groups, _ = setup(service, n=1)
    asset = images(service, 1)[0]
    derived = service.prepare_asset(asset, {'operation': {'kind': 'rotate', 'angle': 90}})['id']
    service.intake(groups[0]['id'], {'asset_ids': [asset], 'request_id': uid()})
    service.intake(groups[1]['id'], {'asset_ids': [derived], 'request_id': uid()})
    original = service.asset(asset, 'original')
    rendered = service.asset(derived)
    first = service.trash.move('groups', groups[0]['id'])
    service.trash.purge(first['id'])
    assert original.exists() and rendered.exists()
    second = service.trash.move('groups', groups[1]['id'])
    service.trash.purge(second['id'])
    assert not original.exists() and not rendered.exists()


@pytest.mark.parametrize('reason,expected', [('ui_closed','paused'),('ui_heartbeat_timeout','queued')])
def test_ui_exit_reason_controls_recovery(service, reason, expected):
    task, groups, _ = setup(service, n=1)
    intake(service, groups[0], 1)
    runner = Runner(service, FakeProvider())
    run = enqueue(runner, task, groups[:1])
    asyncio.run(runner.stop(run['id'], reason=reason, interrupt=False))
    runner.recover()
    with service.store.connection() as db:
        row = service.store.get(db, 'runs', run['id'])
        assert row['state'] == expected and row['stop_reason'] == reason


def test_reference_interrupt_recovers_frozen_revision_and_preserves_edits(service):
    from test_workflow import config
    task = service.create('tasks', {'name': '范本恢复'})
    draft = service.draft(task['id'], {'asset_ids': images(service, 1)})
    runner = Runner(service, FakeProvider())
    run = runner.create(task['id'], {'request_id':uid(),'purpose':'reference','cloud_consent':True,
        'images_prepared':True,'draft_revision':draft['revision']}, config())['runs'][0]
    asyncio.run(runner.stop(run['id'], reason='ui_connection_lost', interrupt=False))
    runner.recover()
    with service.store.connection() as db:
        row = service.store.get(db,'runs',run['id'])
        current = service.store.get(db,'reference_drafts',task['id'])
        assert row['state'] == 'queued' and current['state'] == 'parsing'
        assert row['draft_revision'] == current['revision']
    asyncio.run(runner.stop(run['id'], reason='ui_connection_lost', interrupt=False))
    service.draft(task['id'], {'items':[{'id':'manual','slot':1,'number':'1','english':'teacher'}]})
    runner.recover()
    with service.store.connection() as db:
        assert service.store.get(db,'runs',run['id'])['state'] == 'paused'
    assert service.draft(task['id'])['items'][0]['english']=='teacher'
