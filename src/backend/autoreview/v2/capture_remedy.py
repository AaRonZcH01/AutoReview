"""Per-photo corrections preserve paper identity, order and immutable call history."""
from .core import digest, now, require


def _view(service, db, page_id):
    store = service.store
    page = store.get(db, 'pages', page_id)
    service.require_available(db, 'groups', page['group_id'])
    require(page['state'] != 'deleted' and page.get('capture_session_id'), 'CAPTURE_PAGE_REQUIRED', '此入口仅处理图库中的流式照片。')
    work = store.list(db, 'work_items', task_id=page['task_id'])
    affected_ids = {page_id}
    while True:
        expanded = affected_ids | {w['page_id'] for w in work if w['state'] != 'superseded' and affected_ids.intersection(w.get('dependency_versions', {}))}
        if expanded == affected_ids:
            break
        affected_ids = expanded
    affected = [store.get(db, 'pages', key) for key in sorted(affected_ids)]
    require(all(not p.get('reviewed') and not p.get('overrides') and not p.get('history') and not p.get('annotations')
                and p.get('photo_status') not in ('readable', 'retake', 'retake_required') for p in affected),
            'CAPTURE_TEACHER_WORK', '本页或跨页关联页面已有教师复核或修改。请先在复核详情中取消复核并处理教师修改，避免丢失结论。', 409)
    sub = store.get(db, 'submissions', page['parent_id'])
    fingerprint = digest([{'id': p['id'], 'generation': p['generation'], 'image': p['image_version_id'], 'withheld': p.get('capture_withheld', False)} for p in affected])
    return {'page_id': page_id, 'paper_tag': sub['paper_tag'], 'slot': page['slot'], 'dependent_pages': len(affected)-1,
            'affected_page_ids': sorted(affected_ids), 'fingerprint': fingerprint}, page, affected, work


def remedy(service, runner, page_id, data):
    store = service.store
    action = data.get('action')
    require(action in ('hold', 'resume', 'replace'), 'CAPTURE_REMEDY_ACTION', '请选择撤回识别、重新提交或替换照片。')
    key = 'capture:remedy:' + page_id + ':' + str(data.get('request_id') or '')
    require(data.get('preview') or data.get('request_id'), 'REQUEST_ID', '补救操作缺少操作标识。')
    with store.connection(write=not data.get('preview')) as db:
        if not data.get('preview'):
            cached = store.once(db, key, data)
            if cached:
                return cached
        view, page, affected, work = _view(service, db, page_id)
        if data.get('preview'):
            return {**view, 'action': action, 'withheld': bool(page.get('capture_withheld'))}
        require(data.get('fingerprint') == view['fingerprint'], 'CAPTURE_REMEDY_CHANGED', '照片或关联页面已变化，请重新查看补救影响后确认。', 409)
        if action == 'resume':
            require(page.get('capture_withheld'), 'CAPTURE_NOT_WITHHELD', '本页没有撤回识别，请先读取最新状态。', 409)
            session = store.get(db, 'capture_sessions', page['capture_session_id'])
            local = session.get('delivery_mode', 'immediate') == 'local'
            require(local or data.get('cloud_consent') is True, 'CLOUD_CONSENT', '重新提交将发送本页及必要跨页图片，并使用账户额度，请明确确认。')
            require(local or session['config']['profile_id'] == service.settings.data.get('active_profile_id'), 'CAPTURE_ACCOUNT_CHANGED', '当前账户与这张照片的冻结账户不同，请切回原账户后重新提交。', 409)
            prep = page.get('preparation') or {}
            expected = digest({'image_version': page['image_version_id'], 'reference': session['reference_version_id'], 'slot': page['slot'], 'seq': page['seq']})
            require(prep.get('confirmed') and prep.get('stamp') == expected, 'IMAGE_PREVIEW_REQUIRED', '替换后的照片需先在图片准备中核对本页及答题区，再重新提交。')
            page = store.put(db, 'pages', {**page, 'capture_withheld': False, 'capture_dependency_wait': False})
            siblings = store.list(db, 'pages', parent_id=page['parent_id'], include_deleted=False)
            queue = {'queued_page_ids': [], 'waiting_page_ids': [], 'runs': []} if local else runner.enqueue_capture(db, session, [p['id'] for p in siblings])
            result = {**view, 'action': action, 'page': page, 'queue': queue}
        else:
            for item in work:
                if item['state'] != 'superseded' and item.get('page_id') in view['affected_page_ids']:
                    store.put(db, 'work_items', {**item, 'state': 'superseded', 'capture_withdrawn': True})
            for p in affected:
                store.put(db, 'pages', {**p, 'state': 'pending', 'generation': p['generation']+1, 'observation_id': None,
                    'capture_withheld': p['id'] == page_id or bool(p.get('capture_withheld')), 'capture_dependency_wait': p['id'] != page_id,
                    'capture_remedy_at': now(), 'photo_status': 'clear', 'machine_photo_status': 'clear'})
            if action == 'replace':
                store.get(db, 'assets', data.get('asset_id', ''))
                fresh = store.get(db, 'pages', page_id)
                service.replace_page(page_id, {'asset_id': data['asset_id'], 'revision': fresh['revision']}, db=db)
            for run_id in {w['parent_id'] for w in work if w.get('page_id') in view['affected_page_ids']}:
                run = store.get(db, 'runs', run_id)
                if run['state'] in ('queued', 'running', 'paused') and not any(w['state'] in ('pending', 'running') for w in store.list(db, 'work_items', parent_id=run_id)):
                    store.put(db, 'runs', {**run, 'state': 'completed', 'ended_at': now()})
            result = {**view, 'action': action, 'page': store.get(db, 'pages', page_id),
                'interrupted_work_ids': [w['id'] for w in work if w['state'] != 'superseded' and w.get('page_id') in view['affected_page_ids']]}
        for parent_id in {p['parent_id'] for p in affected}:
            service.update_submission(db, parent_id)
        store.event(db, 'capture.remedy_' + action, page, affected_count=len(affected))
        return store.remember(db, key, data, result)
