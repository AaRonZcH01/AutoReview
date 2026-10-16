import asyncio
import json
import zipfile
from pathlib import Path
from fastapi.testclient import TestClient
import pytest
from autoreview.v2.core import MachineSettings, Problem, uid
from autoreview.v2.provider import Provider
from autoreview.v2.runner import Runner
from autoreview.v2.api import create_app
from autoreview.v2.contracts import MatchingRule, MatchingEdge, OUTPUT_MODELS, strict_schema
from autoreview.v2.grading import grade_matching, grade_choice
from test_workflow import service, setup, images, intake, enqueue, FakeProvider, config, drain


def test_reference_parse_is_draft_and_failure_unlocks_edit(service):
    task = service.create('tasks', {'name': 'Reference'})
    group = service.create('groups', {'name': '1班', 'parent_id': task['id']})
    service.draft(task['id'], {'asset_ids': images(service, 1)})
    provider = FakeProvider()
    runner = Runner(service, provider)
    run = enqueue(runner, task, [], purpose='reference')
    asyncio.run(drain(runner))
    assert service.draft(task['id'])['state'] == 'draft'
    with pytest.raises(Problem, match='确认参考答案'):
        intake(service, group, 1)
    provider.failure = Problem('REFERENCE_BAD', 'offline failure')
    run = enqueue(runner, task, [], purpose='reference')
    asyncio.run(runner.tick())
    assert service.draft(task['id'])['state'] == 'draft'
    service.draft(task['id'], {'issues': ['teacher can edit']})


def test_unknown_not_counted_as_student_error(service):
    task, groups, _ = setup(service, 1)
    intake(service, groups[0], 1)
    class Uncertain(FakeProvider):
        async def infer(self, *args):
            result = await super().infer(*args)
            result['output']['cells'][0]['uncertain'] = True
            return result
    runner = Runner(service, Uncertain())
    enqueue(runner, task, groups[:1])
    asyncio.run(drain(runner))
    page = service.detail(task['id'])['pages'][0]
    assert page['results'][0]['status'] == 'unknown'
    with pytest.raises(Problem, match='待核实'):
        service.review(page['id'], {'revision': page['revision'], 'action': 'confirm'})


def test_stop_during_preparation_never_sends_image(service):
    task, groups, _ = setup(service, 1)
    intake(service, groups[0], 1)
    async def scenario():
        class Preparing(FakeProvider):
            async def quota(self, profile):
                self.started.set()
                await self.release.wait()
                return None
        provider = Preparing()
        provider.started, provider.release = asyncio.Event(), asyncio.Event()
        runner = Runner(service, provider)
        run = enqueue(runner, task, groups[:1])
        job = asyncio.create_task(runner.tick())
        await provider.started.wait()
        await runner.stop(run['id'])
        provider.release.set()
        await job
        assert not provider.calls
        with service.store.connection() as db:
            assert service.store.list(db, 'attempts')[0]['state'] == 'interrupted'
    asyncio.run(scenario())


def test_profile_switch_creates_new_frozen_run(service):
    task, groups, _ = setup(service, 1)
    intake(service, groups[0], 2)
    provider = FakeProvider()
    runner = Runner(service, provider)
    run = enqueue(runner, task, groups[:1])
    async def scenario():
        await runner.tick()
        await runner.stop(run['id'])
        second = runner.resume(run['id'], {**config(), 'profile_id': 'second-account'})
        assert second['id'] != run['id']
        await drain(runner)
    asyncio.run(scenario())
    assert [c['config']['profile_id'] for c in provider.calls] == ['fake', 'second-account']
    with service.store.connection() as db:
        assert service.store.get(db, 'runs', run['id'])['config']['profile_id'] == 'fake'


def test_retake_after_completion_keeps_continuation_chain(service):
    task, groups, _ = setup(service, 1)
    intake(service, groups[0], 1)
    runner = Runner(service, FakeProvider())
    first = enqueue(runner, task, groups[:1])
    asyncio.run(drain(runner))
    page = service.detail(task['id'])['pages'][0]
    service.replace_page(page['id'], {'revision': page['revision'], 'asset_id': images(service, 1)[0]})
    second = enqueue(runner, task, groups[:1])
    assert second['parent_id'] == first['parent_id']


def test_fast_uses_catalog_id_not_label(tmp_path):
    class Catalog(Provider):
        async def account(self, profile_id=None):
            return {'account': {'type': 'chatgpt'}, 'models': [{'model': 'test-model', 'inputModalities': ['image'],
                'supportedReasoningEfforts': [{'reasoningEffort': 'medium'}],
                'serviceTiers': [{'id': 'priority', 'name': 'Fast', 'description': 'faster'}]}]}
    provider = Catalog(MachineSettings(tmp_path))
    actual, _ = asyncio.run(provider.validate({**config(), 'speed': 'fast'}))
    assert actual['service_tier'] == 'priority'
    with pytest.raises(Problem):
        asyncio.run(provider.validate({**config(), 'effort': 'unsupported'}))


def test_backup_restore_checksums_paths_and_no_auto_resume(tmp_path):
    settings = MachineSettings(tmp_path / 'machine')
    settings.save(library_location=str(tmp_path / 'library'), export_directory=str(tmp_path / 'exports'))
    app = create_app(settings, FakeProvider(), nonce='secret', start_worker=False)
    with TestClient(app) as client:
        client.headers['X-AutoReview-Session'] = 'secret'
        task, groups, _ = setup(app.state.service, 1)
        intake(app.state.service, groups[0], 1)
        run = enqueue(app.state.runner, task, groups[:1])
        backup = client.post('/api/library/backup', json={}).json()['path']
        client.post('/api/runs/' + run['id'] + '/stop')
        target = tmp_path / 'restored'
        response = client.post('/api/library/restore', json={'archive': backup, 'path': str(target)})
        assert response.status_code == 200, response.text
        assert not response.json()['auto_resume']
        assert (target / 'assets').is_dir()
        assert client.post('/api/entities/tasks', json={'name': 'must restart'}).status_code == 409
    app = create_app(settings, FakeProvider(), nonce='secret', start_worker=False)
    with TestClient(app):
        with app.state.service.store.connection() as db:
            assert app.state.service.store.list(db, 'runs')[0]['state'] == 'paused'
    settings.save(library_location=str(tmp_path / 'new'))
    app = create_app(settings, FakeProvider(), nonce='secret', start_worker=False)
    malformed = tmp_path / 'bad.zip'
    with zipfile.ZipFile(malformed, 'w') as archive:
        archive.writestr('backup-manifest.json', json.dumps({'checksums': {'../escape.txt': 'x', 'library.json': 'x', 'database/autoreview.sqlite': 'x'}}))
    with TestClient(app) as client:
        client.headers['X-AutoReview-Session'] = 'secret'
        result = client.post('/api/library/restore', json={'archive': str(malformed), 'path': str(tmp_path / 'blocked')})
        assert result.status_code == 422 and result.json()['code'] == 'BACKUP_PATH'
        assert not (tmp_path / 'escape.txt').exists()


def test_matching_directed_and_many_to_many():
    rule = MatchingRule(nodes=[{'id': x, 'group': x[0], 'slot': 1} for x in ('L1', 'L2', 'R1', 'R2')],
                        allowed_edges=[('L1', 'R1'), ('L1', 'R2'), ('L2', 'R2')], cardinality='many_to_many', directed=True)
    edges = [MatchingEdge(id=str(i), from_node=a, to_node=b) for i, (a, b) in enumerate(rule.allowed_edges)]
    assert grade_matching(rule, edges)['status'] == 'correct'
    edges[0] = MatchingEdge(id='0', from_node='R1', to_node='L1')
    assert grade_matching(rule, edges)['status'] == 'incorrect'
    assert grade_choice(['A', 'B'], ['A']) == 'incorrect'
    assert grade_choice([], ['A']) == 'missing'


def test_matching_one_to_many_explicit_source_and_known_extra_is_error():
    rule = MatchingRule(nodes=[{'id': x, 'group': x[0], 'slot': 1} for x in ('R1', 'L1', 'L2', 'R2')],
        allowed_edges=[('L1', 'R1'), ('L1', 'R2')], cardinality='one_to_many', source_group='L')
    edges = [MatchingEdge(id='1', from_node='L1', to_node='R1'), MatchingEdge(id='2', from_node='L1', to_node='R2')]
    assert grade_matching(rule, edges)['status'] == 'correct'
    edges.append(MatchingEdge(id='3', from_node='L2', to_node='R2'))
    assert grade_matching(rule, edges)['status'] == 'incorrect'


def test_strict_output_schemas_and_no_student_answer_leak():
    for model in OUTPUT_MODELS.values():
        schema = strict_schema(model)
        assert schema['additionalProperties'] is False
        assert set(schema['required']) == set(schema['properties'])


def test_photo_issues_independent_of_spelling_and_require_review(service):
    task, groups, _ = setup(service, 1)
    intake(service, groups[0], 1)
    class Photo(FakeProvider):
        async def infer(self, *args):
            result = await super().infer(*args)
            result['output']['photo_issues'] = ['edge glare']
            return result
    runner = Runner(service, Photo())
    enqueue(runner, task, groups[:1])
    asyncio.run(drain(runner))
    page = service.detail(task['id'])['pages'][0]
    assert page['photo_issues'] == ['edge glare']
    with pytest.raises(Problem, match='照片'):
        service.review(page['id'], {'revision': page['revision'], 'action': 'confirm'})
    page = service.review(page['id'], {'revision': page['revision'], 'photo_status': 'accepted'})
    service.review(page['id'], {'revision': page['revision'], 'action': 'confirm'})


def test_legacy_import_preserves_original_and_never_starts_queue(service, tmp_path):
    import hashlib
    from autoreview.db import Database
    from autoreview.v2.legacy import import_legacy
    old = Database(tmp_path / 'legacy')
    asset = images(service, 1)[0]
    content = service.asset(asset).read_bytes()
    picture = old.image_dir / 'page.png'
    picture.write_bytes(content)
    with old.connection() as db:
        db.execute("INSERT INTO batches(id,name,class_name,created_at,updated_at,answer_version) VALUES('b','Legacy','Class','2026-01-01','2026-01-01',1)")
        db.execute("INSERT INTO answer_versions VALUES('b',1,?,'2026-01-01')", (json.dumps([{'question_number': 1, 'english': 'apple'}]),))
        db.execute("INSERT INTO submissions(id,batch_id,tag,created_at) VALUES('s','b','017','2026-01-01')")
        db.execute("INSERT INTO pages(id,submission_id,input_seq,file_name,file_path,sha256,width,height,created_at,config_json,review_json,reviewed) VALUES('p','s',1,'page.png',?,?,240,180,'2026-01-01','{}',?,1)",
            (str(picture), hashlib.sha256(content).hexdigest(), json.dumps({'1': {'status': 'incorrect', 'raw_english': 'aple'}})))
    source_before = old.path.read_bytes()
    response = import_legacy(service, str(old.data_dir), 'legacy-request')
    assert old.path.read_bytes() == source_before
    assert import_legacy(service, str(old.data_dir), 'legacy-request') == response
    detail = service.detail(response['task_ids'][0])
    assert detail['submissions'][0]['paper_tag'] == '017'
    assert detail['groups'][0]['next_tag'] == 18
    assert detail['pages'][0]['legacy_results']['teacher']['1']['raw_english'] == 'aple'
    assert detail['draft']['state'] == 'draft'
    assert not detail['runs']
    assert (service.store.root / response['archive_relative_path']).is_file()


def test_backup_chosen_folder_restore_child_preserves_existing_files(tmp_path):
    settings = MachineSettings(tmp_path / 'machine')
    settings.save(library_location=str(tmp_path / 'library'), export_directory=str(tmp_path / 'exports'))
    app = create_app(settings, FakeProvider(), nonce='secret', start_worker=False)
    selected = tmp_path / 'chosen backup folder'
    parent = tmp_path / 'existing folder'
    parent.mkdir()
    sentinel = parent / 'keep.txt'
    sentinel.write_text('existing work')
    with TestClient(app) as client:
        client.headers['X-AutoReview-Session'] = 'secret'
        task, groups, _ = setup(app.state.service, 1)
        intake(app.state.service, groups[0], 1)
        response = client.post('/api/library/backup', json={'directory': str(selected)})
        assert response.status_code == 200, response.text
        backup = response.json()
        assert Path(backup['directory']) == selected
        archive_path = Path(backup['path'])
        assert archive_path.parent == selected
        assert backup['bytes'] == archive_path.stat().st_size
        assert not list(selected.glob('*.partial'))
        with zipfile.ZipFile(archive_path) as archive:
            assert not json.loads(archive.read('backup-manifest.json'))['credentials_included']
        response = client.post('/api/library/restore', json={
            'archive': str(archive_path), 'path': str(parent), 'create_subdirectory': True})
        assert response.status_code == 200, response.text
        restored = Path(response.json()['path'])
        assert restored.parent == parent and restored.name.startswith('AutoReview-Restored-')
        assert (restored / 'database/autoreview.sqlite').is_file()
        assert sentinel.read_text() == 'existing work'
        assert (tmp_path / 'library/database/autoreview.sqlite').is_file()
        assert settings.data['library_location'] == str(restored)
        assert not response.json()['auto_resume']


def test_restore_rejects_installer_and_invalid_zip_with_clear_message(tmp_path):
    settings = MachineSettings(tmp_path / 'machine')
    settings.save(library_location=str(tmp_path / 'library'), export_directory=str(tmp_path / 'exports'))
    app = create_app(settings, FakeProvider(), nonce='secret', start_worker=False)
    with TestClient(app) as client:
        client.headers['X-AutoReview-Session'] = 'secret'
        archive_path = tmp_path / 'installer.zip'
        with zipfile.ZipFile(archive_path, 'w') as archive:
            archive.writestr('AutoReview-Service.exe', 'not a backup')
        response = client.post('/api/library/restore', json={
            'archive': str(archive_path), 'path': str(tmp_path), 'create_subdirectory': True})
        assert response.status_code == 422 and response.json()['code'] == 'BACKUP_FORMAT'
        assert '程序安装包' in response.json()['message']
        archive_path.write_text('not zip')
        response = client.post('/api/library/restore', json={
            'archive': str(archive_path), 'path': str(tmp_path), 'create_subdirectory': True})
        assert response.status_code == 422 and response.json()['code'] == 'BACKUP_FILE'
        assert settings.data['library_location'] == str(tmp_path / 'library')
        assert not list(tmp_path.glob('AutoReview-Restored-*'))
