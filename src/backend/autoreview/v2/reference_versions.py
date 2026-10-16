"""Immutable answer content, editable catalogue, explicit official activation."""
from .core import require, now, digest, TABLES
from datetime import datetime, timedelta
import json


def reference_counts(store, db, version_id):
    def contains(value):
        if isinstance(value,dict): return any(contains(v) for v in value.values())
        if isinstance(value,list): return any(contains(v) for v in value)
        return value==version_id
    def effective(table, row):
        # Older snapshots copied the working draft's previous confirmation pointer.
        # It is publication provenance, not a dependency on another answer snapshot.
        return {k: v for k, v in row.items() if k != 'confirmed_version_id'} if table == 'reference_versions' else row
    return {table: count for table in TABLES if table != 'reference_metadata'
            if (count := sum(contains(effective(table, row)) for row in store.list(db, table)
                if not (table == 'reference_versions' and row['id'] == version_id)))}


def references(store, db, version_id):
    return sum(reference_counts(store, db, version_id).values())


def purge_snapshot(store, db, task, version, metadata):
    counts = reference_counts(store, db, version['id'])
    official = version['id'] == task.get('reference_version_id')
    hidden = bool(metadata.get('hidden'))
    blocked_reason = ('当前正式版本不能彻底删除，请先切换正式版本。' if official else
                      '请先将版本移入版本回收站。' if not hidden else
                      '该版本仍被答案草稿、计分规则或批改记录引用，必须保留作为历史依据。' if counts else '')
    snapshot = {'version_id': version['id'], 'version_revision': version['revision'],
                'task_revision': task['revision'], 'metadata_revision': metadata.get('revision'),
                'reference_count': sum(counts.values()), 'reference_counts': counts,
                'blocked_reason': blocked_reason, 'can_purge': not bool(blocked_reason)}
    return {**snapshot, 'fingerprint': digest(snapshot)}


def sweep(service):
    store=service.store
    with store.connection(write=True,purge=True) as db:
        for m in store.list(db,'reference_metadata'):
            if m.get('delete_after') and m['delete_after']<=now() and not references(store,db,m['id']):
                db.execute('DELETE FROM reference_versions WHERE id=?',(m['id'],))
                store.put(db,'reference_metadata',{**m,'state':'purged','delete_after':None})
                # Assets are shared resources. Their normal reference-aware cleanup owns deletion.


def catalogue(store, db, task_id, hidden=False):
    task = store.get(db, 'tasks', task_id)
    metadata = {m['id']: m for m in store.list(db, 'reference_metadata', task_id=task_id)}
    rows = [{**r, 'name': metadata.get(r['id'], {}).get('name', '答案 v' + str(r['version'])),
             'official': r['id'] == task.get('reference_version_id'), 'read_only': True,
             'delete_after':metadata.get(r['id'],{}).get('delete_after'),
             'hidden_at':metadata.get(r['id'],{}).get('hidden_at'),
             'hidden':bool(metadata.get(r['id'],{}).get('hidden'))}
            for r in store.list(db, 'reference_versions', task_id=task_id)
            if bool(metadata.get(r['id'], {}).get('hidden'))==hidden]
    return sorted(rows, key=lambda r: (r.get('hidden_at') if hidden else r['created_at']) or r['created_at'], reverse=True)


def manage(service, task_id, version_id, data):
    store = service.store
    with store.connection(write=True, purge=data.get('action') == 'purge') as db:
        service.require_available(db, 'tasks', task_id)
        task = store.get(db, 'tasks', task_id)
        version = store.get(db, 'reference_versions', version_id)
        require(version['task_id'] == task_id, 'REFERENCE_SCOPE', '答案版本不属于当前任务。')
        draft = store.get(db, 'reference_drafts', task_id)
        action = data.get('action')
        metadata = store.get(db, 'reference_metadata', version_id, optional=True) or {'id': version_id, 'task_id': task_id, 'parent_id': task_id}
        if action == 'rename':
            name = str(data.get('name', '')).strip()
            require(0 < len(name) <= 100, 'NAME_REQUIRED', '版本名称为1至100字。')
            return store.put(db, 'reference_metadata', {**metadata, 'name': name})
        if action == 'delete':
            require(version_id != task.get('reference_version_id'), 'OFFICIAL_VERSION', '不能删除当前正式版本，请先切换正式版本。')
            require(data.get('confirmed'), 'CONFIRM_REQUIRED', '请确认从版本列表移除；原始历史及已有批改引用保留。')
            count=references(store,db,version_id)
            return store.put(db, 'reference_metadata', {**metadata, 'hidden': True, 'hidden_at': now(),
                'reference_count':count,'delete_after':None if count else (datetime.fromisoformat(now())+timedelta(days=30)).isoformat()})
        if action=='delete_preview':
            count=references(store,db,version_id)
            return {'reference_count':count, 'blocked_reason': '当前正式版本不能移除，请先切换到另一个正式版本。' if version_id == task.get('reference_version_id') else '',
                    'behavior':'仅从常用列表移除，引用记录保留' if count else '移入版本回收站，30天后按引用检查清理'}
        if action=='restore':
            return store.put(db,'reference_metadata',{**metadata,'hidden':False,'delete_after':None})
        if action in ('purge_preview', 'purge'):
            preview = purge_snapshot(store, db, task, version, metadata)
            if action == 'purge_preview':
                return preview
            require(data.get('confirmed'), 'CONFIRM_REQUIRED', '彻底删除后无法恢复，请明确确认。')
            require(data.get('fingerprint') == preview['fingerprint'], 'VERSION_CHANGED', '版本状态或引用已经变化，请重新查看删除影响。', 409)
            require(preview['can_purge'], 'VERSION_REFERENCED', preview['blocked_reason'], 409)
            db.execute('DELETE FROM reference_versions WHERE id=?', (version_id,))
            # Keep a small catalogue tombstone, never a recoverable answer copy.
            store.put(db, 'reference_metadata', {**metadata, 'state': 'purged', 'delete_after': None, 'purged_at': now()})
            for cached in db.execute('SELECT key,response FROM idempotency').fetchall():
                from .trash import strings
                if version_id in set(strings(json.loads(cached['response']))):
                    db.execute('DELETE FROM idempotency WHERE key=?', (cached['key'],))
            store.event(db, 'reference.purged', version, reference_version_id=version_id)
            # Original and shared image assets are reclaimed by reference-aware library cleanup.
            return {'purged': version_id}
        require(action in ('preview', 'activate'), 'VERSION_ACTION', '不支持此版本操作。')
        old = store.get(db, 'reference_versions', task['reference_version_id']) if task.get('reference_version_id') else None
        active = [r['id'] for r in store.list(db, 'runs', task_id=task_id) if r['state'] in ('queued', 'running')]
        incompatible = bool(old and store.list(db, 'submissions', task_id=task_id) and
                            (old['page_count'] != version['page_count'] or (old.get('page_ids') or old.get('asset_ids')) != (version.get('page_ids') or version.get('asset_ids'))))
        snapshot = {'task_revision': task['revision'], 'draft_revision': draft['revision'], 'version_id': version_id,
                    'active_runs': active, 'incompatible_pages': incompatible,
                    'draft_will_be_preserved': True, 'scoring_requires_confirmation': bool(task.get('scoring_enabled'))}
        stamp = digest(snapshot)
        if action == 'preview':
            return {**snapshot, 'fingerprint': stamp}
        require(data.get('fingerprint') == stamp and data.get('confirmed'), 'VERSION_CHANGED', '状态已变化，请重新查看切换影响并确认。', 409)
        require(not active, 'RUN_ACTIVE', '请先等待或停止当前处理，再切换正式版本。')
        require(not incompatible, 'PAGE_ORDER_REBIND', '此历史版本页结构与已有作业不同，不能直接切换；请导出到新任务。')
        # Every outgoing work copy is retained, including unconfirmed teacher edits.
        content = {k: v for k, v in version.items() if k not in ('id', 'revision', 'created_at', 'updated_at', 'version')}
        restored = store.put(db, 'reference_drafts', {**content, 'id': task_id, 'parent_id': task_id,
            'state': 'confirmed', 'confirmed_version_id': version_id,
            'replaced_drafts': [*draft.get('replaced_drafts', []), {k: v for k, v in draft.items() if k != 'replaced_drafts'}]})
        store.put(db, 'tasks', {**task, 'reference_version_id': version_id, 'scoring_version_id': None if task.get('scoring_enabled') else task.get('scoring_version_id')})
        if metadata.get('hidden'):
            store.put(db, 'reference_metadata', {**metadata, 'hidden': False, 'delete_after': None})
        for group in store.list(db, 'groups', task_id=task_id):
            store.put(db, 'groups', {**group, 'reference_version_id': version_id})
        store.event(db, 'reference.activated', restored, reference_version_id=version_id)
        return {'reference_version_id': version_id, 'draft': restored}
