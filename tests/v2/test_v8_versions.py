import pytest

from autoreview.v2.core import Problem, uid
from autoreview.v2.reference_versions import manage, catalogue
from autoreview.v2.trash import Trash
from test_workflow import service, setup


def clone(service, reference, number=42):
    with service.store.connection(write=True) as db:
        return service.store.put(db, 'reference_versions', {**reference, 'id': uid(), 'version': number})


def test_version_rename_keeps_snapshot_immutable_and_current_is_protected(service):
    task, _, reference = setup(service, 1)
    manage(service, task['id'], reference['id'], {'action': 'rename', 'name': '教师核对版'})
    version = service.detail(task['id'])['answer_versions'][0]
    assert version['name'] == '教师核对版' and version['items'] == reference['items']
    preview = manage(service, task['id'], reference['id'], {'action': 'delete_preview'})
    assert '正式版本' in preview['blocked_reason']
    with pytest.raises(Problem, match='正式版本'):
        manage(service, task['id'], reference['id'], {'action': 'delete', 'confirmed': True})
    purge = manage(service, task['id'], reference['id'], {'action': 'purge_preview'})
    assert not purge['can_purge']


def test_version_recycle_restore_and_explicit_purge_require_current_preview(service):
    task, _, reference = setup(service, 1)
    old = clone(service, reference)
    manage(service, task['id'], old['id'], {'action': 'delete', 'confirmed': True})
    assert service.detail(task['id'])['hidden_answer_versions'][0]['id'] == old['id']
    manage(service, task['id'], old['id'], {'action': 'restore'})
    assert any(v['id'] == old['id'] for v in service.detail(task['id'])['answer_versions'])
    manage(service, task['id'], old['id'], {'action': 'delete', 'confirmed': True})
    preview = manage(service, task['id'], old['id'], {'action': 'purge_preview'})
    assert preview['can_purge'] and preview['reference_count'] == 0
    with pytest.raises(Problem, match='明确确认'):
        manage(service, task['id'], old['id'], {'action': 'purge', 'fingerprint': preview['fingerprint']})
    manage(service, task['id'], old['id'], {'action': 'rename', 'name': '更名后的旧版'})
    with pytest.raises(Problem, match='已经变化'):
        manage(service, task['id'], old['id'], {'action': 'purge', 'confirmed': True, 'fingerprint': preview['fingerprint']})
    fresh = manage(service, task['id'], old['id'], {'action': 'purge_preview'})
    result = manage(service, task['id'], old['id'], {'action': 'purge', 'confirmed': True, 'fingerprint': fresh['fingerprint']})
    assert result['purged'] == old['id']
    with service.store.connection() as db:
        assert service.store.get(db, 'reference_versions', old['id'], optional=True) is None
        assert service.store.get(db, 'reference_versions', reference['id'])['items'] == reference['items']
        assert service.store.get(db, 'reference_metadata', old['id'])['state'] == 'purged'


def test_new_reference_after_purge_preview_cannot_be_lost(service):
    task, _, reference = setup(service, 1)
    old = clone(service, reference)
    manage(service, task['id'], old['id'], {'action': 'delete', 'confirmed': True})
    preview = manage(service, task['id'], old['id'], {'action': 'purge_preview'})
    with service.store.connection(write=True) as db:
        service.store.put(db, 'scoring_versions', {'task_id': task['id'], 'parent_id': task['id'], 'reference_version_id': old['id'], 'version': 1})
    with pytest.raises(Problem, match='已经变化'):
        manage(service, task['id'], old['id'], {'action': 'purge', 'confirmed': True, 'fingerprint': preview['fingerprint']})
    fresh = manage(service, task['id'], old['id'], {'action': 'purge_preview'})
    assert not fresh['can_purge'] and fresh['reference_counts']['scoring_versions'] == 1
    with pytest.raises(Problem, match='历史依据'):
        manage(service, task['id'], old['id'], {'action': 'purge', 'confirmed': True, 'fingerprint': fresh['fingerprint']})
    with service.store.connection() as db:
        assert service.store.get(db, 'reference_versions', old['id'])


def test_hidden_version_activation_restores_catalogue_and_preserves_outgoing_draft(service):
    task, _, reference = setup(service, 1)
    old = clone(service, reference)
    manage(service, task['id'], old['id'], {'action': 'delete', 'confirmed': True})
    preview = manage(service, task['id'], old['id'], {'action': 'preview'})
    manage(service, task['id'], old['id'], {'action': 'activate', 'confirmed': True, 'fingerprint': preview['fingerprint']})
    detail = service.detail(task['id'])
    assert detail['task']['reference_version_id'] == old['id']
    assert detail['hidden_answer_versions'] == []
    assert detail['draft']['replaced_drafts']
    assert next(v for v in detail['answer_versions'] if v['id'] == old['id'])['official']


def test_main_and_version_recycle_use_deletion_time_descending(service):
    trash = Trash(service)
    entries = []
    for i in range(3):
        folder = service.create('folders', {'name': str(i)})
        entry = trash.move('folders', folder['id'])
        with service.store.connection(write=True) as db:
            entries.append(service.store.put(db, 'trash_entries', {**entry, 'deleted_at': f'2026-09-{28+i}T01:00:00+00:00', 'seq': 10-i}))
    assert [e['id'] for e in trash.rows()['items']] == [e['id'] for e in reversed(entries)]
    task, _, reference = setup(service, 1)
    old_versions = []
    for i in range(2):
        old = clone(service, reference, 40+i)
        manage(service, task['id'], old['id'], {'action': 'delete', 'confirmed': True})
        old_versions.append(old)
    with service.store.connection() as db:
        assert [v['id'] for v in catalogue(service.store, db, task['id'], hidden=True)] == [v['id'] for v in reversed(old_versions)]


def test_real_publication_provenance_does_not_permanently_retain_unused_previous_version(service):
    task, _, first = setup(service, 1)
    draft = service.draft(task['id'], {'items': [{'id': 'q1', 'number': '1', 'slot': 1, 'english': 'pear'}]})
    second = service.confirm_reference(task['id'], draft['revision'])
    # A legacy frozen snapshot may include the prior draft's confirmation pointer.
    with service.store.connection(write=True) as db:
        service.store.put(db, 'reference_versions', {**second, 'id': uid(), 'version': 90, 'confirmed_version_id': first['id']})
    removed = manage(service, task['id'], first['id'], {'action': 'delete', 'confirmed': True})
    assert removed['reference_count'] == 0
    preview = manage(service, task['id'], first['id'], {'action': 'purge_preview'})
    assert preview['can_purge']
    manage(service, task['id'], first['id'], {'action': 'purge', 'confirmed': True, 'fingerprint': preview['fingerprint']})
    assert service.detail(task['id'])['task']['reference_version_id'] == second['id']
