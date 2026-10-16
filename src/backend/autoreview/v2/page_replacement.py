"""Explicit local replacement, immutable review audit and dependency fences."""
from contextlib import nullcontext
from .core import digest, now, require


def view(service, db, page_id):
    store = service.store
    service.require_available(db, 'pages', page_id)
    page = store.get(db, 'pages', page_id)
    service.require_available(db, 'groups', page['group_id'])
    pages = {p['id']: p for p in store.list(db, 'pages', task_id=page['task_id'], include_deleted=False)}
    work = [w for w in store.list(db, 'work_items', task_id=page['task_id'])
            if w['state'] != 'superseded' and w.get('page_id') in pages and w.get('generation') == pages[w['page_id']]['generation']]
    affected = {page_id}
    while True:
        expanded = affected | {w['page_id'] for w in work if affected.intersection(w.get('dependency_versions', {}))}
        if expanded == affected:
            break
        affected = expanded
    rows = [pages[key] for key in sorted(affected)]
    snapshots = [{k: p.get(k) for k in ('id','generation','image_version_id','reviewed','overrides','history','history_cursor','annotations')}
                 for p in rows]
    sub = store.get(db, 'submissions', page['parent_id'])
    return {'page_id': page_id, 'paper_tag': sub['paper_tag'], 'slot': page['slot'], 'page_count': sub['page_count'],
            'revision': page['revision'], 'image_version_id': page['image_version_id'], 'generation': page['generation'],
            'fingerprint': digest(snapshots), 'affected_page_ids': sorted(affected), 'dependent_pages': len(rows)-1,
            'reviewed_pages': sum(bool(p.get('reviewed')) for p in rows),
            'teacher_modified_pages': sum(bool(p.get('overrides') or p.get('annotations') or p.get('history')) for p in rows)}, page, rows, work


def replace(service, page_id, data, db=None):
    store = service.store
    with store.connection(write=not data.get('preview')) if db is None else nullcontext(db) as connection:
        key = 'page:replacement:' + page_id + ':' + data['request_id'] if data.get('request_id') else None
        if key and not data.get('preview'):
            cached = store.once(connection, key, data)
            if cached:
                return cached
        preview, page, affected, work = view(service, connection, page_id)
        if data.get('preview'):
            return preview
        if data.get('fingerprint'):
            require(preview['fingerprint'] == data['fingerprint'], 'REPLACEMENT_CHANGED',
                    '照片或教师结论已变化，请重新查看替换影响并再次确认。', 409)
            require(data.get('replacement_ack') is True, 'REPLACEMENT_ACK', '请检查本张照片和替换影响后确认。')
        require(page['revision'] == data.get('revision'), 'REVISION_CONFLICT', '页面已变化，请重新读取替换影响后确认。', 409)
        store.get(connection, 'assets', data['asset_id'])
        old = store.get(connection, 'image_versions', page['image_version_id'])
        version = store.put(connection, 'image_versions', {'parent_id': page_id, 'task_id': page['task_id'],
            'group_id': page['group_id'], 'asset_id': data['asset_id'], 'version': old['version']+1, 'replaces': old['id']})
        interrupted = []
        for current in affected:
            # The previous teacher state stays immutable even when its image/context changes.
            store.put(connection, 'review_revisions', {'parent_id': current['id'], 'task_id': current['task_id'],
                'group_id': current['group_id'], 'kind': 'image_replacement', 'source_page_id': page_id,
                'image_version_id': current['image_version_id'], 'snapshot': current, 'replaced_at': now()})
            changes = {'generation': current['generation']+1, 'state':'pending', 'observation_id':None,
                'overrides':{}, 'reviewed':False, 'history':[], 'history_cursor':-1, 'annotations':[],
                'photo_status':'clear', 'machine_photo_status':'clear', 'replacement_pending':True}
            if current['id'] == page_id:
                changes.update(image_version_id=version['id'], preparation=None)
                if current.get('capture_session_id'):
                    changes['capture_withheld'] = True
            else:
                changes['capture_dependency_wait'] = True
            for item in work:
                if item.get('page_id') != current['id']:
                    continue
                if item['state'] == 'completed':
                    run = store.get(connection, 'runs', item['parent_id'])
                    changes['replacement_continuation_group_id'] = run['parent_id']
                if item['state'] in ('pending','running'):
                    interrupted.append(item['id'])
                    store.put(connection, 'work_items', {**item, 'state':'superseded'})
            fresh = store.put(connection, 'pages', {**current, **changes})
            if current['id'] == page_id:
                page = fresh
        for parent_id in {p['parent_id'] for p in affected}:
            service.update_submission(connection, parent_id)
        result = {**page, 'interrupted_work_ids': interrupted, 'replacement_affected_page_ids': preview['affected_page_ids']}
        store.event(connection, 'page.replaced', page, image_version_id=version['id'], affected_count=len(affected))
        return store.remember(connection,key,data,result) if key else result
