"""Local capture drafts and atomic, explicitly authorized page intake.

The camera transport is a frontend concern. A capture is only a local draft until
commit; the injected queue callback uses the same SQLite transaction as intake.
"""
from __future__ import annotations

from contextlib import nullcontext
import hashlib

from .core import digest, now, require, uid
from .contracts import Box


def _request(data, namespace):
    request_id = str(data.get('request_id') or '')
    require(request_id and len(request_id) <= 180, 'REQUEST_REQUIRED', '操作标识缺失，请重新打开采集。')
    return namespace + ':' + request_id


def _available(service, db, table, identifier):
    service.require_available(db, table, identifier)
    return service.store.get(db, table, identifier)


def _context(service, db, session, active=True):
    store = service.store
    service.require_available(db, 'tasks', session['task_id'])
    service.require_available(db, 'groups', session['group_id'])
    if active:
        require(session['state'] == 'active', 'CAPTURE_STOPPED', '本轮采集已停止；已确认照片仍保留。', 409)
    group = store.get(db, 'groups', session['group_id'])
    task = store.get(db, 'tasks', session['task_id'])
    require(group.get('reference_version_id') == session['reference_version_id'],
            'CAPTURE_REFERENCE_CHANGED', '参考答案已切换，请停止后重新开始采集。', 409)
    from .scoring import gate
    gate(store, db, task['id'])
    require(bool(task.get('scoring_enabled')) == bool(session.get('scoring_enabled')) and
            (not task.get('scoring_enabled') or task.get('scoring_version_id') == session.get('scoring_version_id')),
            'CAPTURE_SCORING_CHANGED', '计分规则已变化，请停止后重新开始采集。', 409)
    ref = store.get(db, 'reference_versions', session['reference_version_id'])
    if session.get('cohort_id'):
        cohort = _available(service, db, 'cohorts', session['cohort_id'])
        require(cohort['group_id'] == group['id'], 'COHORT_SCOPE', '名单必须属于当前班级。', 409)
        if session.get('cohort_submission_ids') is not None:
            require(cohort['submission_ids'] == session['cohort_submission_ids'],
                    'CAPTURE_COHORT_CHANGED', '补页名单成员或顺序已变化，请停止后重新选择名单。', 409)
    return task, group, ref


def _position(service, db, session):
    store = service.store
    single = session.get('mode', 'whole_submission') == 'single_page'
    if single and session.get('cohort_submission_ids') is not None:
        cohort = _available(service, db, 'cohorts', session['cohort_id'])
        ids = session['cohort_submission_ids']
        require(cohort['submission_ids'] == ids, 'CAPTURE_COHORT_CHANGED', '补页名单已变化，请停止后重新选择。', 409)
        for identifier in reversed(ids) if session.get('reverse_cohort') else ids:
            sub = _available(service, db, 'submissions', identifier)
            _check_submission(service, db, session, sub)
            # Trashed pages still own their slot; only explicit capture withdrawal releases it.
            if not any(p['slot'] == session['slot'] and not p.get('capture_withdrawn')
                       for p in store.list(db, 'pages', parent_id=identifier)):
                return {'paper_tag': sub['paper_tag'], 'slot': session['slot'], 'submission_id': identifier}
        return None
    if not single and session.get('current_submission_id'):
        sub = store.get(db, 'submissions', session['current_submission_id'])
        pages = store.list(db, 'pages', parent_id=sub['id'], include_deleted=False)
        free = [slot for slot in range(1, session['page_count'] + 1) if slot not in {p['slot'] for p in pages}]
        if free:
            return {'paper_tag': sub['paper_tag'], 'slot': free[0], 'submission_id': sub['id']}
    group = store.get(db, 'groups', session['group_id'])
    start = session.get('start_tag') if not session.get('committed_count') else None
    tag = max(int(start or 1), int(group.get('next_tag', 1)))
    return {'paper_tag': str(tag).zfill(3), 'slot': session['slot'] if single else 1, 'submission_id': None}


def _check_submission(service, db, session, sub):
    require(sub['group_id'] == session['group_id'] and sub['page_count'] == session['page_count'],
            'CAPTURE_SUBMISSION_SCOPE', '补页名单含其他班级或页数的作业，请重新选择。', 409)
    pages = service.store.list(db, 'pages', parent_id=sub['id'])
    require(all(p.get('reference_version_id') == session['reference_version_id'] for p in pages if not p.get('capture_withdrawn')) and
            (not session.get('scoring_enabled') or sub.get('scoring_version_id') == session.get('scoring_version_id')),
            'CAPTURE_REFERENCE_CHANGED', '该作业使用其他答案或计分规则，请在原版本下补页。', 409)


def start(service, task_id, data, config, account=None, write_guard=None, *, _db=None, _append_cohort=None):
    delivery = data.get('delivery_mode', 'immediate')
    require(delivery in ('immediate', 'local'), 'CAPTURE_DELIVERY_MODE', '请选择逐张云端识别或仅保存本地。')
    require(delivery == 'local' or data.get('cloud_consent') is True, 'CLOUD_CONSENT', '开始前请确认：核对后的照片将发送到当前 ChatGPT 账户。')
    store = service.store
    key = _request(data, 'capture:start')
    request = {**data, 'task_id': task_id}
    with store.connection(write=True) if _db is None else nullcontext(_db) as db:
        if write_guard:
            write_guard()
        cached = store.once(db, key, request)
        if cached:
            return cached
        service.require_available(db, 'tasks', task_id)
        group = store.get(db, 'groups', data.get('group_id', ''))
        require(group['task_id'] == task_id, 'GROUP_SCOPE', '请选择当前作业的班级。')
        service.require_available(db, 'groups', group['id'])
        require(group.get('reference_version_id'), 'REFERENCE_NOT_CONFIRMED', '请先确认参考答案再开始采集。')
        from .scoring import gate
        gate(store, db, task_id)
        task = store.get(db, 'tasks', task_id)
        ref = store.get(db, 'reference_versions', group['reference_version_id'])
        require(not any(s['state'] == 'active' for s in store.list(db, 'capture_sessions', group_id=group['id'])),
                'CAPTURE_ACTIVE', '该班级已有采集会话，请继续或停止原会话。', 409)
        mode = data.get('mode', 'whole_submission')
        require(mode in ('whole_submission', 'single_page'), 'INTAKE_MODE', '请选择整份或单面编排。')
        slot = data.get('slot', 1)
        require(type(slot) is int and 1 <= slot <= ref['page_count'], 'PAGE_SLOT', '页码超出范本页数。')
        require(mode == 'single_page' or not data.get('cohort_id'), 'COHORT_SCOPE', '补页名单仅用于单面编排。')
        require(mode == 'whole_submission' or not data.get('submission_id'), 'CAPTURE_SUBMISSION_SCOPE', '单面补页请选择名单，不能使用整份接续。')
        cohort = _available(service, db, 'cohorts', data['cohort_id']) if data.get('cohort_id') else None
        if cohort:
            require(cohort['group_id'] == group['id'], 'COHORT_SCOPE', '补页名单必须属于当前班级。')
        require(type(data.get('reverse_cohort', False)) is bool, 'COHORT_ORDER', '请选择正序或倒序。')
        raw_tag = str(data.get('start_tag') or group.get('next_tag', 1))
        require(raw_tag.isdecimal(), 'PAPER_TAG', '纸签编号必须为正整数。')
        start_tag = int(raw_tag)
        require(start_tag >= 1 and (cohort or start_tag >= int(group.get('next_tag', 1))), 'PAPER_TAG', '起始纸签不能小于当前可用编号。')
        continuing = data.get('submission_id') or None
        if continuing:
            sub = store.get(db, 'submissions', continuing)
            pages = store.list(db, 'pages', parent_id=continuing, include_deleted=False)
            require(sub['group_id'] == group['id'] and sub['page_count'] == ref['page_count'] and len(pages) < ref['page_count'],
                    'CAPTURE_SUBMISSION_SCOPE', '只能接续当前班级尚未收齐的同页数作业。')
            require(all(p.get('reference_version_id') == ref['id'] for p in pages) and
                    (not task.get('scoring_enabled') or sub.get('scoring_version_id') == task.get('scoring_version_id')),
                    'CAPTURE_REFERENCE_CHANGED', '该缺页作业使用其他答案或计分规则，请在原版本下补页。', 409)
        countdown_seconds = service.settings.data.get('capture_countdown_seconds', 2)
        require(type(countdown_seconds) is int and 0 <= countdown_seconds <= 60,
                'CAPTURE_COUNTDOWN_INVALID', '拍摄间隔必须为 0 至 60 秒的整数。')
        arrangement = {'mode': mode, 'slot': slot, 'group_id': group['id'], 'page_count': ref['page_count'],
            'reference_version_id': ref['id'], 'scoring_enabled': bool(task.get('scoring_enabled')),
            'scoring_version_id': task.get('scoring_version_id'), 'start_tag': start_tag,
            'current_submission_id': continuing, 'committed_count': 0,
            'cohort_id': cohort['id'] if cohort else None,
            'cohort_submission_ids': list(cohort['submission_ids']) if cohort else None,
            'reverse_cohort': bool(cohort and data.get('reverse_cohort'))}
        if cohort:
            require(arrangement['cohort_submission_ids'], 'COHORT_COUNT', '这个名单还没有已确认作业，请先拍摄首面。')
            for identifier in arrangement['cohort_submission_ids']:
                _check_submission(service, db, arrangement, _available(service, db, 'submissions', identifier))
            require(_position(service, db, arrangement), 'CAPTURE_COHORT_COMPLETE', '该名单此页已全部收齐，请选择其他页码或名单。')
        elif mode == 'single_page':
            # Only draft recovery may append to the original growing roster.
            if _append_cohort:
                cohort = _available(service, db, 'cohorts', _append_cohort)
                require(cohort['group_id'] == group['id'] and cohort.get('source') == 'stream_capture',
                        'COHORT_SCOPE', '原采集名单无法接续，请重新开始。', 409)
            else:
                cohort = store.put(db, 'cohorts', {'parent_id': group['id'], 'task_id': task_id,
                    'group_id': group['id'], 'submission_ids': [], 'name': f"流式单面·第{slot}页·起始{str(start_tag).zfill(3)}",
                    'mode': mode, 'source': 'stream_capture'})
            arrangement['cohort_id'] = cohort['id']
        chain = store.put(db, 'continuation_groups', {'parent_id': task_id, 'task_id': task_id,
            'state': 'active', 'user_intent': 'continue', 'started_at': now(), 'source': 'stream_capture'})
        # Never persist authentication payloads or tokens from the account adapter.
        identity = (account or {}).get('account') or {}
        safe_account = {k: identity[k] for k in ('email', 'planType', 'type') if k in identity}
        session = store.put(db, 'capture_sessions', {'parent_id': task_id, 'task_id': task_id,
            'group_id': group['id'], 'state': 'active', 'source': data.get('source', 'camera'),
            'reference_version_id': ref['id'], 'page_count': ref['page_count'],
            'scoring_enabled': bool(task.get('scoring_enabled')), 'scoring_version_id': task.get('scoring_version_id'),
            'delivery_mode': delivery, 'config': dict(config) if delivery == 'immediate' else {},
            'account': safe_account if delivery == 'immediate' else {}, 'cloud_consent_at': now() if delivery == 'immediate' else None,
            'capture_countdown_seconds': countdown_seconds,
            'continuation_group_id': chain['id'], 'start_tag': start_tag, 'committed_count': 0,
            'current_submission_id': continuing, 'active_frame_id': None, **arrangement})
        result = {**session, 'next': _position(service, db, session), 'frames': [], 'active_frame': None}
        store.event(db, 'capture.started', session, group_id=group['id'], source=session['source'])
        return store.remember(db, key, request, result)


def read(service, session_id):
    store = service.store
    with store.connection() as db:
        session = store.get(db, 'capture_sessions', session_id)
        service.require_available(db, 'tasks', session['task_id'], allow_archived=True)
        frames = store.list(db, 'capture_frames', parent_id=session_id)
        active = next((f for f in frames if f['id'] == session.get('active_frame_id') and f['state'] == 'draft'), None)
        result = {**session, 'frames': frames[-30:], 'active_frame': active,
                  'committed_files': [f.get('source_filename') or store.get(db, 'assets', f['asset_id'])['filename']
                                      for f in frames if f.get('state') == 'committed'],
                  'last_committed_frame_id': next((f['id'] for f in reversed(frames) if f['state'] == 'committed'), None)}
        if session['state'] == 'active':
            result['next'] = _position(service, db, session)
        if active:
            ref = store.get(db, 'reference_versions', session['reference_version_id'])
            result['active_frame'] = _frame_view(store, db, active, ref)
        return result


def list_sessions(service, task_id):
    with service.store.connection() as db:
        service.require_available(db, 'tasks', task_id)
        sessions = service.store.list(db, 'capture_sessions', task_id=task_id)
        for session in sessions:
            frame_id = session.get('active_frame_id')
            saved = service.store.get(db, 'capture_frames', frame_id, optional=True) if frame_id else None
            if saved and saved['state'] == 'draft':
                asset = service.store.get(db, 'assets', saved['asset_id'])
                session['saved_frame'] = {k: saved[k] for k in ('id', 'revision', 'paper_tag', 'slot', 'asset_id')}
                session['saved_frame']['source_filename'] = saved.get('source_filename') or asset['filename']
        return sessions


def _asset_signature(asset):
    return digest({key: asset.get(key) for key in ('id', 'sha256', 'display_sha256', 'processing_hash')})


def _continuation_versions(store, db, submission_id):
    return {p['id']: {'slot': p['slot'], 'image_version_id': p.get('image_version_id')}
            for p in store.list(db, 'pages', parent_id=submission_id, include_deleted=False)} if submission_id else {}


def _saved_draft(service, db, frame_id, revision):
    store = service.store
    saved = store.get(db, 'capture_frames', frame_id)
    session = store.get(db, 'capture_sessions', saved['parent_id'])
    service.require_available(db, 'tasks', saved['task_id'])
    service.require_available(db, 'groups', saved['group_id'])
    require(session['state'] == 'stopped' and session.get('active_frame_id') == frame_id and saved['state'] == 'draft',
            'CAPTURE_DRAFT_UNAVAILABLE', '这张保留照片已被继续检查、放弃或确认，请读取最新列表。', 409)
    require(saved['revision'] == revision, 'REVISION_CONFLICT', '保留照片已变化，请读取最新列表后重试。', 409)
    return saved, session


def recover_draft(service, frame_id, data, config, account=None, write_guard=None):
    """Copy only retained pixels into a new, unchecked session; never revive a queue."""
    store = service.store
    key = _request(data, 'capture:recover:' + frame_id)
    require(data.get('delivery_mode', 'immediate') == 'local' or data.get('cloud_consent') is True,
            'CLOUD_CONSENT', '请确认新一轮核对后的照片会发送到当前账户。')
    with store.connection() as db:
        cached = store.once(db, key, data)
        if cached:
            return {'session': read(service, cached['session_id']), 'frame': frame(service, cached['frame_id'])}
        old, old_session = _saved_draft(service, db, frame_id, data.get('revision'))
        group = store.get(db, 'groups', old['group_id'])
        ref = store.get(db, 'reference_versions', group.get('reference_version_id', ''))
        asset = store.get(db, 'assets', old['asset_id'])
        signature = _asset_signature(asset)
        require(old.get('asset_signature', signature) == signature, 'CAPTURE_ASSET_CHANGED', '保留照片的图片版本已变化，请重新拍摄或选择图片。', 409)
        versions = _continuation_versions(store, db, old.get('submission_id'))
        require(not old.get('submission_id') or old.get('continuation_versions') == versions,
                'CAPTURE_DEPENDENCY_CHANGED', '同纸签已收页面的图片版本已变化，或早期草稿缺少版本凭据，请重新拍摄或选择图片。', 409)
    # Decode/registration and hashing stay outside the write transaction.
    for path_key, hash_key in (('relative_path', 'sha256'), ('display_path', 'display_sha256')):
        path = store.root / asset[path_key]
        require(path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == asset[hash_key],
                'CAPTURE_ASSET_CHANGED', '保留照片的文件已变化或缺失，请重新拍摄或选择图片。', 409)
    preparation = build_preparation(service, asset['id'], ref, old['slot'])
    with store.connection(write=True) as db:
        if write_guard:
            write_guard()
        cached = store.once(db, key, data)
        if cached:
            session = store.get(db, 'capture_sessions', cached['session_id'])
            fresh_frame = store.get(db, 'capture_frames', cached['frame_id'])
            return {'session': {**session, 'next': _position(service, db, session)}, 'frame': _frame_view(store, db, fresh_frame)}
        current, original_session = _saved_draft(service, db, frame_id, old['revision'])
        group_now = store.get(db, 'groups', old['group_id'])
        require(group_now.get('reference_version_id') == ref['id'], 'CAPTURE_REFERENCE_CHANGED', '准备过程中参考答案已变化，请重新读取保留照片。', 409)
        require(_asset_signature(store.get(db, 'assets', old['asset_id'])) == signature and current['asset_id'] == old['asset_id'],
                'CAPTURE_ASSET_CHANGED', '保留照片的图片版本已变化，请重新读取。', 409)
        require(_continuation_versions(store, db, old.get('submission_id')) == versions,
                'CAPTURE_DEPENDENCY_CHANGED', '同纸签已收页面在准备过程中变化，请重新拍摄或选择图片。', 409)
        source = data.get('source', old_session.get('source', 'files'))
        require(source in ('camera', 'files'), 'CAPTURE_SOURCE', '请选择摄像头或本地图片作为下一张来源。')
        if old_session.get('cohort_submission_ids') is not None:
            cohort = _available(service, db, 'cohorts', old_session['cohort_id'])
            require(cohort['submission_ids'] == old_session['cohort_submission_ids'], 'CAPTURE_COHORT_CHANGED',
                    '原补页名单成员或顺序已变化，请重新拍摄，不能恢复旧位置。', 409)
        new_session = start(service, old['task_id'], {'request_id': 'recover:' + digest([frame_id, data['request_id']]),
            'group_id': old['group_id'], 'source': source, 'start_tag': None if old.get('submission_id') else old['paper_tag'],
            'submission_id': old.get('submission_id') if old_session.get('mode', 'whole_submission') == 'whole_submission' else None,
            'mode': old_session.get('mode', 'whole_submission'), 'slot': old_session.get('slot', 1),
            'cohort_id': old_session.get('cohort_id') if old_session.get('cohort_submission_ids') is not None else None,
            'reverse_cohort': old_session.get('reverse_cohort', False), 'cloud_consent': data.get('cloud_consent'),
            'delivery_mode': data.get('delivery_mode', 'immediate')}, config, account, write_guard, _db=db,
            _append_cohort=old_session.get('cohort_id') if old_session.get('mode') == 'single_page' and old_session.get('cohort_submission_ids') is None else None)
        require(new_session['next'] and new_session['next']['slot'] == old['slot'] and new_session['next']['paper_tag'] == old['paper_tag'],
                'CAPTURE_POSITION_CHANGED', '原纸签或页位已被其他导入占用，请重新拍摄或选择图片。', 409)
        # No old zones, confirmation, submission intent, or frozen model config is copied.
        value = store.put(db, 'capture_frames', {'parent_id': new_session['id'], 'task_id': old['task_id'],
            'group_id': old['group_id'], 'state': 'draft', 'capture_id': uid(), 'asset_id': asset['id'],
            'source_filename': old.get('source_filename') or asset['filename'], 'reference_version_id': ref['id'],
            'preparation': preparation, 'captured_at': old.get('captured_at') or now(), 'recovered_from_frame_id': frame_id,
            'asset_signature': signature, 'continuation_versions': versions,
            'seq': store.sequence(db, 'capture:' + new_session['id']), **new_session['next']})
        stored_session = store.get(db, 'capture_sessions', new_session['id'])
        stored_session = store.put(db, 'capture_sessions', {**stored_session, 'active_frame_id': value['id']})
        store.put(db, 'capture_frames', {**current, 'state': 'recovered', 'recovered_to_frame_id': value['id']})
        store.put(db, 'capture_sessions', {**original_session, 'active_frame_id': None})
        store.event(db, 'capture.draft_recovered', value, source_frame_id=frame_id)
        store.remember(db, key, data, {'session_id': new_session['id'], 'frame_id': value['id']})
        return {'session': {**stored_session, 'next': new_session['next']}, 'frame': _frame_view(store, db, value, ref)}


def discard_draft(service, frame_id, data):
    store = service.store
    key = _request(data, 'capture:discard:' + frame_id)
    with store.connection(write=True) as db:
        cached = store.once(db, key, data)
        if cached:
            return cached
        value = store.get(db, 'capture_frames', frame_id)
        session = store.get(db, 'capture_sessions', value['parent_id'])
        service.require_available(db, 'tasks', value['task_id'])
        require(session['state'] in ('active', 'stopped') and session.get('active_frame_id') == frame_id and value['state'] == 'draft',
                'CAPTURE_DRAFT_UNAVAILABLE', '这张照片已确认、放弃或被重拍，请读取最新状态。', 409)
        require(value['revision'] == data.get('revision'), 'REVISION_CONFLICT', '草稿已变化，请重新查看后放弃。', 409)
        value = store.put(db, 'capture_frames', {**value, 'state': 'discarded', 'discarded_at': now()})
        store.put(db, 'capture_sessions', {**session, 'active_frame_id': None})
        store.event(db, 'capture.draft_discarded', value)
        return store.remember(db, key, data, {'id': value['id'], 'state': value['state'], 'images_preserved': True})


def _frame_view(store, db, value, ref=None):
    ref = ref or store.get(db, 'reference_versions', value['reference_version_id'])
    return {**value, 'asset': store.get(db, 'assets', value['asset_id']),
            'questions': [{'id': a['id'], 'number': a['number'], 'section': a.get('section', '')}
                          for a in ref['items'] if a['slot'] == value['slot'] and not a.get('excluded')]}


def frame(service, frame_id):
    with service.store.connection() as db:
        value = service.store.get(db, 'capture_frames', frame_id)
        service.require_available(db, 'tasks', value['task_id'], allow_archived=True)
        return _frame_view(service.store, db, value)


def build_preparation(service, asset_id, ref, slot):
    """Same image alignment and boundary semantics as ordinary preflight."""
    from .imaging import align, project, corners
    answers = [a for a in ref['items'] if a['slot'] == slot and not a.get('excluded')]
    templates = ref.get('asset_ids') or []
    mapping = align(service.asset(templates[slot - 1]), service.asset(asset_id),
        [a.get('evidence_box') or (a.get('answer_zone') or a.get('box') if a['kind'] not in
         ('choice', 'letter_choice', 'marked_choice', 'matching', 'ordering') else None) for a in answers]) if templates else {
            'matrix': [[1, 0, 0], [0, 1, 0], [0, 0, 1]], 'status': 'manual_text', 'reason': '纯文字答案：按页与题号识别。'}
    zones, polygons, warnings = {}, {}, []
    for answer in answers:
        box = answer.get('answer_zone') or answer.get('box')
        points = project(corners(box), mapping['matrix']) if box else None
        polygons[answer['id']] = points
        zone = {'x1': min(p[0] for p in points), 'y1': min(p[1] for p in points),
                'x2': max(p[0] for p in points), 'y2': max(p[1] for p in points)} if points else None
        if zone and any(v < 0 or v > 1 for v in zone.values()):
            warnings.append({'item_id': answer['id'], 'number': answer['number'],
                'projected_box': zone, 'message': '自动定位触及图片外缘，请检查是否缺题或裁切。'})
            clipped = {k: max(0, min(1, v)) for k, v in zone.items()}
            if clipped['x2'] > clipped['x1'] and clipped['y2'] > clipped['y1']:
                zone = clipped
        zones[answer['id']] = zone
    return {'stamp': digest({'asset_id': asset_id, 'reference': ref['id'], 'slot': slot}),
            'reference_version_id': ref['id'], 'mapping': mapping, 'zones': zones, 'polygons': polygons,
            'confirmed': False, 'boundary_warnings': warnings, 'previous_zones': zones}


def stage(service, session_id, data):
    store = service.store
    capture_id = str(data.get('capture_id') or '')
    require(capture_id and len(capture_id) <= 180, 'CAPTURE_ID', '照片标识缺失，请重新拍摄。')
    request = {**data, 'session_id': session_id}
    key = 'capture:frame:' + session_id + ':' + capture_id
    with store.connection() as db:
        cached = store.once(db, key, request)
        if cached:
            return _frame_view(store, db, store.get(db, 'capture_frames', cached['id']))
        session = store.get(db, 'capture_sessions', session_id)
        _, _, ref = _context(service, db, session)
        asset = store.get(db, 'assets', data.get('asset_id', ''))
        position = _position(service, db, session)
        require(position, 'CAPTURE_COHORT_COMPLETE', '该名单此页已全部收齐，请停止采集并另开一轮。', 409)
        session_revision = session['revision']
        continuation_versions = _continuation_versions(store, db, position.get('submission_id'))
    preparation = build_preparation(service, asset['id'], ref, position['slot'])
    with store.connection(write=True) as db:
        cached = store.once(db, key, request)
        if cached:
            return _frame_view(store, db, store.get(db, 'capture_frames', cached['id']))
        current = store.get(db, 'capture_sessions', session_id)
        _context(service, db, current)
        require(current['revision'] == session_revision, 'CAPTURE_CHANGED', '采集位置已变化，请读取当前状态后重试。', 409)
        require(_position(service, db, current) == position, 'CAPTURE_CHANGED', '纸签已变化，请重新检查。', 409)
        require(_continuation_versions(store, db, position.get('submission_id')) == continuation_versions,
                'CAPTURE_DEPENDENCY_CHANGED', '同纸签已收页面的图片版本已变化，请重新拍摄。', 409)
        old_id = current.get('active_frame_id')
        if old_id:
            old = store.get(db, 'capture_frames', old_id)
            require(old['state'] == 'draft', 'CAPTURE_CHANGED', '上一张照片状态已变化，请刷新。', 409)
            store.put(db, 'capture_frames', {**old, 'state': 'retaken'})
        value = store.put(db, 'capture_frames', {'parent_id': session_id, 'task_id': session['task_id'],
            'group_id': session['group_id'], 'state': 'draft', 'capture_id': capture_id,
            'asset_id': asset['id'], 'source_filename': str(data.get('source_filename') or asset['filename'])[:512], 'reference_version_id': ref['id'], 'preparation': preparation,
            'asset_signature': _asset_signature(asset), 'continuation_versions': continuation_versions,
            'captured_at': data.get('captured_at') or now(), 'seq': store.sequence(db, 'capture:' + session_id), **position})
        store.put(db, 'capture_sessions', {**current, 'active_frame_id': value['id']})
        store.event(db, 'capture.local_preview', value, asset_id=asset['id'])
        store.remember(db, key, request, {'id': value['id']})
        return _frame_view(store, db, value, ref)


def edit(service, frame_id, data):
    store = service.store
    key = _request(data, 'capture:edit:' + frame_id)
    with store.connection() as db:
        cached = store.once(db, key, data)
        if cached:
            return _frame_view(store, db, store.get(db, 'capture_frames', frame_id))
        old = store.get(db, 'capture_frames', frame_id)
        session = store.get(db, 'capture_sessions', old['parent_id'])
        _, _, ref = _context(service, db, session)
        require(old['state'] == 'draft' and session.get('active_frame_id') == frame_id,
                'CAPTURE_CHANGED', '此照片已提交或被重拍，不能再修改草稿。', 409)
        require(data.get('revision') == old['revision'], 'REVISION_CONFLICT', '照片草稿已变化，请重新读取。', 409)
        asset = store.get(db, 'assets', data.get('asset_id') or old['asset_id'])
    preparation = build_preparation(service, asset['id'], ref, old['slot']) if asset['id'] != old['asset_id'] else old['preparation']
    with store.connection(write=True) as db:
        cached = store.once(db, key, data)
        if cached:
            return _frame_view(store, db, store.get(db, 'capture_frames', frame_id))
        current = store.get(db, 'capture_frames', frame_id)
        session = store.get(db, 'capture_sessions', old['parent_id'])
        _context(service, db, session)
        require(current['state'] == 'draft' and session.get('active_frame_id') == frame_id,
                'CAPTURE_CHANGED', '此照片已提交或被重拍，不能覆盖。', 409)
        value = store.put(db, 'capture_frames', {**current, 'asset_id': asset['id'], 'asset_signature': _asset_signature(asset), 'preparation': preparation}, expected=old['revision'])
        store.remember(db, key, data, {'id': value['id']})
        return _frame_view(store, db, value, ref)


def commit(service, frame_id, data, enqueue_pages):
    store = service.store
    key = _request(data, 'capture:commit:' + frame_id)
    with store.connection(write=True) as db:
        current = store.get(db, 'capture_frames', frame_id)
        require(current['state'] != 'withdrawn', 'CAPTURE_WITHDRAWN', '这张照片已撤回，请重拍；不能重新使用旧确认请求。', 409)
        cached = store.once(db, key, data)
        if cached:
            return cached
        current = store.get(db, 'capture_frames', frame_id)
        # A second click with another operation id must not intake the frame twice.
        if current.get('commit_result'):
            return current['commit_result']
        session = store.get(db, 'capture_sessions', current['parent_id'])
        local = session.get('delivery_mode', 'immediate') == 'local'
        require(local or data.get('cloud_consent') is True, 'CLOUD_CONSENT', '请确认将当前照片发送到此采集会话的 ChatGPT 账户。')
        _, group, ref = _context(service, db, session)
        require(current['state'] == 'draft' and session.get('active_frame_id') == frame_id,
                'CAPTURE_CHANGED', '此照片已被重拍或结束，请读取当前状态。', 409)
        require(current['revision'] == data.get('revision'), 'REVISION_CONFLICT', '照片草稿已修改，请重新检查后确认。', 409)
        require(_position(service, db, session) == {k: current[k] for k in ('paper_tag', 'slot', 'submission_id')},
                'CAPTURE_CHANGED', '班级编排已变化，请重新预览当前纸签与页位。', 409)
        preparation = current['preparation']
        require(data.get('preparation_stamp') == preparation['stamp'], 'PREPARATION_CHANGED', '处理图片已变化，请重新核对。', 409)
        require(data.get('images_prepared') is True, 'IMAGE_PREVIEW_REQUIRED', '请检查当前照片、纸签、页位和完整答题区。')
        require(not preparation.get('boundary_warnings') or data.get('boundary_ack') is True,
                'REGION_BOUNDARY_REVIEW', '照片边缘有定位警告，请先核对是否缺题或裁切。')
        zones = data.get('zones', preparation['zones'])
        expected = {a['id'] for a in ref['items'] if a['slot'] == current['slot'] and not a.get('excluded')}
        require(isinstance(zones, dict) and set(zones) == expected, 'REGION_MISMATCH', '题目与完整作答区必须一一对应。')
        for zone in zones.values():
            if zone is not None:
                Box.model_validate(zone)
            else:
                require(not ref.get('asset_ids'), 'REGION_REQUIRED', '范本题目缺少完整作答区，请先补齐定位。')
        if current['submission_id']:
            sub = store.get(db, 'submissions', current['submission_id'])
        else:
            require(not any(s['paper_tag'] == current['paper_tag'] and not s.get('capture_withdrawn') for s in store.list(db, 'submissions', group_id=group['id'])),
                    'TAG_EXISTS', '该纸签已被使用，请重新检查编排。', 409)
            sub = store.put(db, 'submissions', {'parent_id': group['id'], 'task_id': session['task_id'],
                'group_id': group['id'], 'paper_tag': current['paper_tag'], 'page_count': session['page_count'],
                'scoring_version_id': session.get('scoring_version_id'), 'seq': store.sequence(db, 'submission:' + group['id']), 'state': 'incomplete'})
            store.put(db, 'groups', {**group, 'next_tag': max(group.get('next_tag', 1), int(current['paper_tag']) + 1)})
        cohort_before = None
        if session.get('mode') == 'single_page' and session.get('cohort_submission_ids') is None:
            cohort = store.get(db, 'cohorts', session['cohort_id'])
            cohort_before = list(cohort['submission_ids'])
            require(sub['id'] not in cohort_before, 'CAPTURE_COHORT_CHANGED', '当前纸签已在名单中，请重新读取采集状态。', 409)
            store.put(db, 'cohorts', {**cohort, 'submission_ids': [*cohort_before, sub['id']]})
        page = store.put(db, 'pages', {'parent_id': sub['id'], 'task_id': session['task_id'], 'group_id': group['id'],
            'slot': current['slot'], 'seq': store.sequence(db, 'page:' + group['id']), 'state': 'pending',
            'generation': 1, 'overrides': {}, 'reviewed': False, 'reference_version_id': ref['id'],
            'history': [], 'history_cursor': -1, 'capture_session_id': session['id'], 'capture_frame_id': frame_id,
            'capture_delivery_mode': session.get('delivery_mode', 'immediate')})
        version = store.put(db, 'image_versions', {'parent_id': page['id'], 'task_id': session['task_id'],
            'group_id': group['id'], 'asset_id': current['asset_id'], 'version': 1})
        stamp = digest({'image_version': version['id'], 'reference': ref['id'], 'slot': page['slot'], 'seq': page['seq']})
        page = store.put(db, 'pages', {**page, 'image_version_id': version['id'], 'preparation': {**preparation,
            'stamp': stamp, 'image_version_id': version['id'], 'zones': zones, 'confirmed': True,
            'confirmed_at': now(), 'manual_edited': zones != preparation['zones']}})
        candidate_pages = [p['id'] for p in store.list(db, 'pages', parent_id=sub['id'], include_deleted=False)
                           if (p.get('preparation') or {}).get('confirmed')]
        queue = {'queued_page_ids': [], 'waiting_page_ids': [], 'runs': []} if local else enqueue_pages(db, session, candidate_pages)
        service.update_submission(db, sub['id'])
        complete = len(candidate_pages) == session['page_count']
        updated_session = store.put(db, 'capture_sessions', {**session, 'active_frame_id': None,
            'current_submission_id': None if complete else sub['id'], 'committed_count': session['committed_count'] + 1,
            'last_committed_frame_id': frame_id})
        result = {'frame_id': frame_id, 'page_id': page['id'], 'submission_id': sub['id'],
            'paper_tag': sub['paper_tag'], 'slot': page['slot'], 'page_count': session['page_count'],
            'state': 'saved_local' if local else 'waiting_dependencies' if page['id'] in queue.get('waiting_page_ids', []) else 'queued',
            'queue': queue, 'next': _position(service, db, updated_session), 'session_id': session['id']}
        store.put(db, 'capture_frames', {**current, 'state': 'committed', 'page_id': page['id'],
            'submission_id': sub['id'], 'committed_at': now(), 'commit_result': result,
            'undo_checkpoint': {'current_submission_id': session.get('current_submission_id'),
                'group_next_tag': group.get('next_tag', 1), 'committed_count': session['committed_count'],
                'submission_created': not current.get('submission_id'), 'image_version_id': version['id'],
                'cohort_submission_ids_before': cohort_before}})
        store.event(db, 'capture.committed', page, session_id=session['id'], capture_id=current['capture_id'],
                    queued_count=len(queue.get('queued_page_ids', [])), waiting_count=len(queue.get('waiting_page_ids', [])))
        return store.remember(db, key, data, result)


def _withdraw_view(service, db, session_id, frame_id):
    """Validate the tail before any cancellation, deletion or number rollback."""
    store = service.store
    session = store.get(db, 'capture_sessions', session_id)
    service.require_available(db, 'tasks', session['task_id'])
    service.require_available(db, 'groups', session['group_id'])
    frames = store.list(db, 'capture_frames', parent_id=session_id)
    committed = [f for f in frames if f['state'] == 'committed']
    require(committed and committed[-1]['id'] == frame_id, 'CAPTURE_WITHDRAW_NOT_LAST', '只能撤回本轮最近确认的一张照片，请读取最新状态。', 409)
    value = committed[-1]
    checkpoint = value.get('undo_checkpoint')
    require(checkpoint, 'CAPTURE_WITHDRAW_LEGACY', '早期照片没有编号回退凭据，请在图库替换图片，不能安全撤回编号。', 409)
    require(not any(s['id'] != session_id and s['state'] == 'active' for s in store.list(db, 'capture_sessions', group_id=session['group_id'])),
            'CAPTURE_WITHDRAW_LATER', '该班级已开始其他采集，请在图库替换图片，避免回退占用编号。', 409)
    pages = store.list(db, 'pages', group_id=session['group_id'], include_deleted=False)
    page = store.get(db, 'pages', value['page_id'])
    require(page['state'] != 'deleted' and pages and pages[-1]['id'] == page['id'],
            'CAPTURE_WITHDRAW_LATER', '已有后续照片或导入占用编号；请在图库替换本页，不能回退其他作业。', 409)
    require(page['capture_frame_id'] == frame_id and page['generation'] == 1 and page['image_version_id'] == checkpoint['image_version_id'],
            'CAPTURE_WITHDRAW_EDITED', '本页已替换或重新处理，请在图库处理，不能撤回早期拍摄。', 409)
    work = [w for w in store.list(db, 'work_items', task_id=session['task_id'])
            if w['state'] != 'superseded' and (w.get('page_id') == page['id'] or page['id'] in w.get('dependency_versions', {}))]
    affected_ids = {page['id'], *(w['page_id'] for w in work)}
    affected = [store.get(db, 'pages', key) for key in sorted(affected_ids)]
    require(all(not p.get('reviewed') and not p.get('overrides') and not p.get('history') and not p.get('annotations')
                and p.get('photo_status') not in ('readable', 'retake') for p in affected),
            'CAPTURE_WITHDRAW_REVIEWED', '本页或依赖它的页面已有教师复核或修改；请在图库替换，保留教师结论。', 409)
    sub = store.get(db, 'submissions', page['parent_id'])
    group = store.get(db, 'groups', session['group_id'])
    cohort = store.get(db, 'cohorts', session['cohort_id']) if session.get('cohort_id') else None
    if checkpoint.get('cohort_submission_ids_before') is not None:
        require(cohort and cohort['submission_ids'] == [*checkpoint['cohort_submission_ids_before'], sub['id']],
                'CAPTURE_COHORT_CHANGED', '采集名单已有其他变化，请在图库替换，不回退名单。', 409)
    elif session.get('cohort_submission_ids') is not None:
        require(cohort and cohort['submission_ids'] == session['cohort_submission_ids'],
                'CAPTURE_COHORT_CHANGED', '补页名单已有其他变化，请在图库替换。', 409)
    siblings = [p for p in pages if p['parent_id'] == sub['id'] and p['id'] != page['id']]
    if not siblings:
        require(checkpoint['submission_created'] and group.get('next_tag') == int(sub['paper_tag']) + 1,
                'CAPTURE_WITHDRAW_LATER', '编号已经变化，无法安全回退；请在图库替换本页。', 409)
    active = store.get(db, 'capture_frames', session.get('active_frame_id') or '', optional=True)
    fingerprint = digest({'frame_id': frame_id, 'session_revision': session['revision'], 'next_tag': group.get('next_tag'),
        'cohort_submission_ids': cohort.get('submission_ids') if cohort else None,
        'affected': [{'id': p['id'], 'generation': p['generation'], 'image_version_id': p['image_version_id']} for p in affected]})
    view = {'frame_id': frame_id, 'session_id': session_id, 'paper_tag': sub['paper_tag'], 'slot': page['slot'],
        'page_count': session['page_count'], 'affected_page_ids': sorted(affected_ids),
        'dependent_pages': len(affected_ids) - 1, 'discard_current_draft': bool(active and active['state'] == 'draft'),
        'fingerprint': fingerprint, 'next': {'paper_tag': sub['paper_tag'], 'slot': page['slot']}}
    return view, session, value, page, sub, group, siblings, affected, work, active


def withdraw_last(service, session_id, data):
    store = service.store
    key = _request(data, 'capture:withdraw:' + session_id) if not data.get('preview') else None
    with store.connection(write=not data.get('preview')) as db:
        if key:
            cached = store.once(db, key, data)
            if cached:
                return cached
        view, session, value, page, sub, group, siblings, affected, work, active = _withdraw_view(service, db, session_id, data.get('frame_id'))
        if data.get('preview'):
            return view
        require(data.get('fingerprint') == view['fingerprint'], 'CAPTURE_WITHDRAW_CHANGED', '采集位置已变化，请重新查看撤回影响后确认。', 409)
        checkpoint = value['undo_checkpoint']
        for p in affected:
            removed = p['id'] == page['id']
            store.put(db, 'pages', {**p, 'state': 'deleted' if removed else 'pending', 'generation': p['generation'] + 1,
                'observation_id': None, 'capture_withdrawn': removed, 'withdrawn_frame_id': value['id'],
                'withdrawn_at': now(), 'preparation': p.get('preparation')})
        for item in work:
            store.put(db, 'work_items', {**item, 'state': 'superseded', 'capture_withdrawn': True, 'withdrawn_at': now()})
        # Preserve successful/failed attempt usage and immutable observations as audit.
        for run_id in {w['parent_id'] for w in work}:
            run = store.get(db, 'runs', run_id)
            remaining = [w for w in store.list(db, 'work_items', parent_id=run_id) if w['state'] in ('pending', 'running')]
            if not remaining and run['state'] in ('queued', 'running', 'paused'):
                store.put(db, 'runs', {**run, 'state': 'completed', 'ended_at': now(), 'withdrawn_page_ids': view['affected_page_ids']})
        if siblings:
            service.update_submission(db, sub['id'])
        else:
            store.put(db, 'submissions', {**sub, 'state': 'deleted', 'capture_withdrawn': True, 'withdrawn_at': now()})
            store.put(db, 'groups', {**group, 'next_tag': checkpoint['group_next_tag']})
        if checkpoint.get('cohort_submission_ids_before') is not None:
            cohort = store.get(db, 'cohorts', session['cohort_id'])
            store.put(db, 'cohorts', {**cohort, 'submission_ids': checkpoint['cohort_submission_ids_before']})
        if active and active['state'] == 'draft':
            store.put(db, 'capture_frames', {**active, 'state': 'retaken', 'withdrawn_by_frame_id': value['id']})
        store.put(db, 'capture_frames', {**value, 'state': 'withdrawn', 'withdrawn_at': now()})
        committed = [f for f in store.list(db, 'capture_frames', parent_id=session_id) if f['state'] == 'committed']
        updated = store.put(db, 'capture_sessions', {**session, 'active_frame_id': None,
            'current_submission_id': sub['id'] if siblings else checkpoint['current_submission_id'],
            'committed_count': checkpoint['committed_count'], 'last_committed_frame_id': committed[-1]['id'] if committed else None})
        result = {**view, 'session': {**updated, 'next': _position(service, db, updated)}, 'withdrawn': True,
                  'interrupted_work_ids': [w['id'] for w in work]}
        store.event(db, 'capture.withdrawn', page, session_id=session_id, frame_id=value['id'], affected_count=len(affected))
        return store.remember(db, key, data, result)


def stop(service, session_id, data=None):
    store = service.store
    with store.connection(write=True) as db:
        session = store.get(db, 'capture_sessions', session_id)
        service.require_available(db, 'tasks', session['task_id'])
        if session['state'] != 'active':
            return session
        # The active draft is retained; it never allocated a paper number or page.
        value = store.put(db, 'capture_sessions', {**session, 'state': 'stopped', 'stopped_at': now()})
        store.event(db, 'capture.stopped', value, committed_count=session['committed_count'])
        return value
