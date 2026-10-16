"""Capture evidence in trash, cold resources and merged old/new libraries."""
import hashlib
import sqlite3

import pytest

from autoreview.v2 import stream_capture as capture
from autoreview.v2.core import MachineSettings, Problem
from autoreview.v2.libraries import merge_library
from autoreview.v2.reference_versions import reference_counts
from autoreview.v2.resources import archive_folder
from autoreview.v2.service import Service
from test_workflow import service, images
from test_v130_capture import begin, shot, confirmation, queued


def checksum(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize('kind', ['groups', 'tasks', 'folders'])
def test_live_capture_prevents_moving_its_scope_to_trash(service, kind):
    task, group, _, session, _ = begin(service)
    value = shot(service, session)
    folder = service.create('folders', {'name':'test archive'})
    service.edit('tasks', task['id'], {'parent_id':folder['id']})
    target = {'groups':group['id'], 'tasks':task['id'], 'folders':folder['id']}[kind]
    with pytest.raises(Problem) as caught:
        service.trash.move(kind, target)
    assert caught.value.code == 'CAPTURE_ACTIVE'
    assert capture.frame(service, value['id'])['state'] == 'draft'
    capture.stop(service, session['id'])
    entry = service.trash.move(kind, target)
    service.trash.restore(entry['id'])
    assert capture.read(service,session['id'])['state'] == 'stopped'
    assert capture.read(service,session['id'])['active_frame']['id'] == value['id']


def test_other_group_can_be_removed_while_capture_remains_active(service):
    task, group, _, session, _ = begin(service)
    other = service.create('groups', {'name':'unrelated class', 'parent_id':task['id']})
    entry = service.trash.move('groups',other['id'])
    service.trash.purge(entry['id'])
    assert capture.read(service,session['id'])['state'] == 'active'
    assert shot(service,session)['state'] == 'draft'


def test_cold_capture_draft_and_shared_asset_survive_other_task_purge(service):
    first, _, ref, first_session, _ = begin(service)
    second, _, _, second_session, _ = begin(service)
    shared = images(service,1)[0]
    first_frame = shot(service,first_session,shared)
    second_frame = shot(service,second_session,shared)
    original_hash = checksum(service.asset(shared,'original'))
    folder = service.create('folders',{'name':'cold capture'})
    service.edit('tasks',first['id'],{'parent_id':folder['id']})
    with pytest.raises(Problem) as caught:
        archive_folder(service,folder['id'])
    assert caught.value.code == 'CAPTURE_ACTIVE'
    capture.stop(service,first_session['id'])
    archive_folder(service,folder['id'])
    assert checksum(service.asset(shared,'original')) == original_hash
    assert capture.read(service,first_session['id'])['active_frame']['asset_id'] == shared
    # A draft holds both its source pixels and its frozen answer reference.
    with service.store.connection() as db:
        counts = reference_counts(service.store,db,ref['id'])
        assert counts['capture_sessions'] == counts['capture_frames'] == 1
    removed = service.trash.move('tasks', first['id'])
    service.trash.purge(removed['id'])
    with service.store.connection() as db:
        assert service.store.get(db,'capture_frames',first_frame['id'],optional=True) is None
        assert service.store.get(db,'capture_sessions',first_session['id'],optional=True) is None
        assert service.store.get(db,'capture_frames',second_frame['id'])['asset_id'] == shared
        assert service.store.get(db,'assets',shared)
    assert checksum(service.asset(shared,'original')) == original_hash
    assert capture.commit(service,second_frame['id'],confirmation(second_frame),queued)['state']=='queued'


def test_cold_capture_only_asset_is_reclaimed_with_its_group(service):
    task, group, _, session, _ = begin(service)
    value = shot(service,session)
    exclusive = value['asset_id']
    capture.stop(service,session['id'])
    folder = service.create('folders',{'name':'exclusive cold capture'})
    service.edit('tasks',task['id'],{'parent_id':folder['id']})
    archive_folder(service,folder['id'])
    entry=service.trash.move('groups',group['id'])
    assert service.trash.preview(entry['id'])['files'] >= 3
    service.trash.purge(entry['id'])
    with service.store.connection() as db:
        assert service.store.get(db,'assets',exclusive,optional=True) is None
        assert service.store.get(db,'capture_frames',value['id'],optional=True) is None
        assert service.store.get(db,'capture_sessions',session['id'],optional=True) is None
        assert not service.store.list(db,'resources')
        assert not db.execute('PRAGMA foreign_key_check').fetchall()


def test_merging_capture_preserves_draft_but_requires_new_local_session(service,tmp_path):
    settings=MachineSettings(tmp_path/'source-machine')
    settings.save(library_location=str(tmp_path/'source-library'))
    source=Service(settings)
    try:
        task,group,_,session,_=begin(source)
        value=shot(source,session)
        original_hash=checksum(source.asset(value['asset_id'],'original'))
        before=checksum(source.store.path)
        result=merge_library(service,source.store.root)
        assert not result['auto_resume']
        assert checksum(source.store.path)==before
        assert capture.read(source,session['id'])['state']=='active'
        with service.store.connection() as db:
            imported=service.store.list(db,'capture_sessions')[0]
            imported_frame=service.store.list(db,'capture_frames')[0]
            chain=service.store.get(db,'continuation_groups',imported['continuation_group_id'])
            assert imported['state']=='stopped' and imported['stop_reason']=='library_import'
            assert chain['state']=='paused' and chain['user_intent']=='stop'
            assert imported['active_frame_id']==imported_frame['id'] and imported_frame['state']=='draft'
            assert imported['group_id'] != group['id'] and imported['task_id'] != task['id']
            assert not db.execute('PRAGMA foreign_key_check').fetchall()
        assert checksum(service.asset(imported_frame['asset_id'],'original'))==original_hash
        with pytest.raises(Problem) as caught:
            capture.commit(service,imported_frame['id'],confirmation(imported_frame),queued)
        assert caught.value.code=='CAPTURE_STOPPED'
        assert merge_library(service,source.store.root)['already_imported']
    finally:
        source.close()


def test_library_without_capture_tables_still_merges(service,tmp_path):
    settings=MachineSettings(tmp_path/'old-machine')
    settings.save(library_location=str(tmp_path/'old-library'))
    source=Service(settings)
    source.create('tasks',{'name':'pre-capture version'})
    path=source.store.path; root=source.store.root
    source.close()
    with sqlite3.connect(path) as db:
        db.execute('DROP TABLE capture_frames')
        db.execute('DROP TABLE capture_sessions')
    before=checksum(path)
    assert merge_library(service,root)['source_preserved']
    assert checksum(path)==before
    with service.store.connection() as db:
        assert not service.store.list(db,'capture_sessions')
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
