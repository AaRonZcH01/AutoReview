"""Read-only import of v0.1. Its missing multi-page binding is never guessed as confirmed."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
from .core import require, uid, now, dumps, atomic_write, digest


def inspect_legacy(path, database=None):
    root = Path(path).resolve()
    source = Path(database) if database else root / 'autoreview.sqlite3'
    require(source.is_file(), 'LEGACY_DATABASE', '该目录未找到 v0.1 autoreview.sqlite3。')
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        require({'batches', 'submissions', 'pages', 'answer_versions'} <= tables, 'LEGACY_SCHEMA', '不是受支持的 v0.1 资料库。')
        batches = [dict(r) for r in db.execute('SELECT * FROM batches ORDER BY created_at')]
        pages = [dict(r) for r in db.execute('SELECT * FROM pages ORDER BY input_seq')]
        submissions = [dict(r) for r in db.execute('SELECT * FROM submissions ORDER BY created_at')]
    missing = []
    for page in pages:
        original = Path(page['file_path']).resolve()
        # Paths from another machine can be rebound only by basename + original SHA256.
        candidates = [original] if original.is_relative_to(root) else []
        candidates += [root / 'images' / Path(page['file_path']).name]
        found = next((p for p in candidates if p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest() == page['sha256']), None)
        page['resolved_path'] = str(found) if found else None
        if not found:
            missing.append(page['id'])
    return {'root': root, 'source': source, 'batches': batches, 'submissions': submissions, 'pages': pages,
            'missing_page_ids': missing}


def import_legacy(service, path, request_id):
    info = inspect_legacy(path)
    require(not info['missing_page_ids'], 'LEGACY_MISSING_IMAGES', '部分历史图片缺失或哈希不匹配，请先恢复原图。', missing_page_ids=info['missing_page_ids'])
    store = service.store
    request = {'legacy_root': str(info['root']), 'request_id': request_id}
    with store.connection() as db:
        previous = store.once(db, request_id, request)
        if previous:
            return previous
    archive_id = uid()
    relative = f'legacy-archives/{archive_id}/original.sqlite'
    destination = store.root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(info['source'].as_uri() + '?mode=ro', uri=True)) as source, closing(sqlite3.connect(destination)) as target:
        source.backup(target)
    info = inspect_legacy(path, database=destination)
    require(not info['missing_page_ids'], 'LEGACY_MISSING_IMAGES', '备份快照中的图片缺失，请恢复原图后重试。')
    with closing(sqlite3.connect(destination)) as old:
        old.row_factory = sqlite3.Row
        answers = [dict(r) for r in old.execute('SELECT * FROM answer_versions ORDER BY version')]
        reviews = [dict(r) for r in old.execute('SELECT * FROM review_history ORDER BY revision')]
    assets = {p['id']: service.save_asset(Path(p['resolved_path']).read_bytes(), p['file_name']) for p in info['pages']}
    imported = []
    with store.connection(write=True) as db:
        folder = store.put(db, 'folders', {'name': 'v0.1 导入 · 待确认页码与答案', 'seq': store.sequence(db, 'folders')})
        for batch in info['batches']:
            old_subs = [s for s in info['submissions'] if s['batch_id'] == batch['id']]
            count = max([sum(p['submission_id'] == s['id'] for p in info['pages']) for s in old_subs] or [1])
            task = store.put(db, 'tasks', {'parent_id': folder['id'], 'name': batch['name'],
                'legacy_batch_id': batch['id'], 'legacy_archive_path': relative, 'seq': store.sequence(db, 'tasks')})
            group = store.put(db, 'groups', {'parent_id': task['id'], 'task_id': task['id'],
                'name': batch['class_name'] or '未命名班级', 'reference_version_id': None,
                'next_tag': max([int(s['tag']) for s in old_subs if s['tag'].isdigit()] or [0]) + 1})
            latest = next((a for a in answers if a['batch_id'] == batch['id'] and a['version'] == batch['answer_version']), None)
            # Legacy format had no reliable per-page answer mapping. Teacher must resolve it.
            items = [{**{k: v for k, v in a.items() if k in ('english', 'chinese', 'variants')},
                      'id': uid(), 'slot': 1, 'number': str(a['question_number']), 'kind': 'dictation',
                      'uncertain': count > 1} for a in json.loads(latest['items_json'] if latest else '[]')]
            store.put(db, 'reference_drafts', {'id': task['id'], 'parent_id': task['id'], 'task_id': task['id'],
                'state': 'draft', 'items': items, 'asset_ids': [], 'page_count': max(1, count),
                'page_labels': [f'第{i+1}页' for i in range(count)], 'writing_rules': [],
                'issues': ['历史版本没有可靠的跨页答案对应关系；请确认页码与答案后才可开始新处理。'],
                'legacy_answer_versions': [a for a in answers if a['batch_id'] == batch['id']]})
            for old_sub in old_subs:
                sub = store.put(db, 'submissions', {'parent_id': group['id'], 'task_id': task['id'], 'group_id': group['id'],
                    'paper_tag': old_sub['tag'], 'page_count': count, 'seq': store.sequence(db, 'submission:' + group['id']),
                    'paper_feedback': old_sub.get('paper_feedback', 'pending'), 'state': 'pending'})
                old_pages = [p for p in info['pages'] if p['submission_id'] == old_sub['id']]
                for slot, old_page in enumerate(old_pages, 1):
                    page = store.put(db, 'pages', {'parent_id': sub['id'], 'task_id': task['id'], 'group_id': group['id'],
                        'seq': store.sequence(db, 'page:' + group['id']), 'slot': slot, 'generation': 1, 'state': 'pending',
                        'reviewed': False, 'overrides': {}, 'history': [], 'history_cursor': -1,
                        'reference_version_id': None, 'legacy_page_id': old_page['id'],
                        'legacy_results': {'machine': json.loads(old_page['machine_json'] or '[]'),
                            'teacher': json.loads(old_page['review_json'] or '{}'), 'reviewed': bool(old_page['reviewed']),
                            'review_history': [r for r in reviews if r['page_id'] == old_page['id']]}})
                    version = store.put(db, 'image_versions', {'parent_id': page['id'], 'task_id': task['id'],
                        'group_id': group['id'], 'asset_id': assets[old_page['id']]['id'], 'version': 1})
                    store.put(db, 'pages', {**page, 'image_version_id': version['id']})
                service.update_submission(db, sub['id'])
            imported.append(task['id'])
        response = {'task_ids': imported, 'archive_relative_path': relative, 'source_preserved': True,
                    'queue_resumed': False, 'teacher_confirmation_required': True}
        store.event(db, 'legacy.imported', archive_id=archive_id, task_count=len(imported), image_count=len(assets))
        store.remember(db, request_id, request, response)
    return response
