"""Explicit batch scopes with revision fencing and individual failure receipts."""
from .core import require, digest, Problem


def batch(service, task_id, data):
    store = service.store
    if data.get('action') == 'confirm':
        snapshot = data.get('preview') or {}
        key = 'prepare-batch:' + task_id + ':' + data.get('request_id', '')
        require(data.get('request_id'), 'REQUEST_ID', '缺少操作标识。')
        with store.connection() as db:
            cached = store.once(db, key, data)
            if cached is not None:
                return cached
        fresh = batch(service, task_id, {'action': 'preview', 'pages': snapshot.get('pages', []),
                                        'reference_version_id': snapshot.get('reference_version_id')})
        require(fresh['fingerprint'] == snapshot.get('fingerprint'), 'PREPARATION_CHANGED',
                '确认范围或图片已变化，请重新预览。', 409)
        confirmed, blocked = [], list(fresh['blocked'])
        for entry in fresh['eligible']:
            try:
                page = service.prepare_page(entry['id'], {'action': 'confirm', 'revision': entry['revision'],
                    'stamp': entry['stamp'], 'reference_version_id': fresh['reference_version_id']})
                confirmed.append({'id': page['id'], 'revision': page['revision']})
            except Problem as error:
                blocked.append({**entry, 'reason': error.message})
        result = {'confirmed': confirmed, 'blocked': blocked}
        with store.connection(write=True) as db:
            return store.remember(db, key, data, result)
    entries = data.get('pages', [])
    require(entries and len({e['id'] for e in entries}) == len(entries), 'PAGE_SCOPE', '请选择图片范围。')
    eligible, blocked, snapshots = [], [], []
    with store.connection() as db:
        service.require_available(db, 'tasks', task_id)
        task = store.get(db, 'tasks', task_id)
        reference = data.get('reference_version_id') or task.get('reference_version_id')
        for entry in entries:
            p = store.get(db, 'pages', entry['id'])
            require(p['task_id'] == task_id, 'PAGE_SCOPE', '图片不属于当前任务。')
            require(p['revision'] == entry['revision'], 'REVISION_CONFLICT', '图片已变化，请刷新后选择。', 409)
            prep = p.get('preparation') or {}
            sub = store.get(db, 'submissions', p['parent_id'])
            item = {'id': p['id'], 'revision': p['revision'], 'stamp': prep.get('stamp'),
                    'label': str(sub['paper_tag']) + ' / 第' + str(p['slot']) + '页'}
            stamp = digest({'image_version': p['image_version_id'], 'reference': reference, 'slot': p['slot'], 'seq': p['seq']})
            reason = ('尚未生成当前版本定位预览' if not prep or prep.get('stamp') != stamp else
                      '边缘区域需逐张核对' if prep.get('boundary_warnings') else
                      '定位不可靠，请逐张检查' if prep.get('mapping', {}).get('status') not in ('aligned', 'manual_text') else '')
            (blocked if reason else eligible).append({**item, **({'reason': reason} if reason else {})})
            snapshots.append({'id': p['id'], 'revision': p['revision']})
    result = {'pages': snapshots, 'eligible': eligible, 'blocked': blocked, 'reference_version_id': reference}
    return {**result, 'fingerprint': digest(result)}
