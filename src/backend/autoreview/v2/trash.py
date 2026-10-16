"""Explicit retention operations; file reclamation is journaled and reference checked."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
from .core import TABLES, Problem, require, now, uid

KINDS = {'folders': '文件夹', 'tasks': '批改任务', 'groups': '任务内班级', 'class_presets': '预设班级'}


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from strings(v)


class Trash:
    def __init__(self, service):
        self.service, self.store = service, service.store

    def members(self, db, table, identifier):
        out = [(table, self.store.get(db, table, identifier))]
        for kind, row in out:
            child_types = ('folders', 'tasks') if kind == 'folders' else ('groups',) if kind == 'tasks' else ()
            for child in child_types:
                out.extend((child, x) for x in self.store.list(db, child, parent_id=row['id']) if x['state'] != 'deleted')
        return out

    def location(self, db, table, row):
        parts, seen = [], set()
        while row.get('parent_id'):
            table = 'tasks' if table == 'groups' else 'folders'
            if row['parent_id'] in seen:
                break
            seen.add(row['parent_id'])
            row = self.store.get(db, table, row['parent_id'], optional=True)
            if not row:
                break
            parts.insert(0, row.get('name', ''))
        return ' / '.join(parts) or ('预设班级列表' if table == 'class_presets' else '根目录')

    def describe(self, db, table, identifier):
        members = self.members(db, table, identifier)
        groups = {r['id'] for t, r in members if t == 'groups'}
        return {'table': table, 'type_label': KINDS[table], 'object_id': identifier,
            'name': members[0][1]['name'], 'original_path': self.location(db, table, members[0][1]),
            'counts': {'folders': sum(t == 'folders' for t, _ in members),
                'tasks': sum(t == 'tasks' for t, _ in members), 'groups': len(groups),
                'submissions': sum(s['group_id'] in groups for s in self.store.list(db, 'submissions')),
                'pages': sum(p['group_id'] in groups for p in self.store.list(db, 'pages'))},
            'members': [{'table': t, 'id': r['id'], 'state': r['state'], 'parent_id': r['parent_id']} for t, r in members]}

    def adopt_legacy(self):
        with self.store.connection(write=True) as db:
            known = {(m['table'], m['id']) for e in self.store.list(db, 'trash_entries') for m in e.get('members', [])}
            for table in KINDS:
                for row in self.store.list(db, table, state='deleted'):
                    if (table, row['id']) in known:
                        continue
                    stamp = now()
                    entry = self.describe(db, table, row['id'])
                    entry['members'][0]['state'] = 'active'
                    self.store.put(db, 'trash_entries', {**entry, 'state': 'deleted', 'deleted_at': stamp,
                        'expires_at': (datetime.fromisoformat(stamp) + timedelta(days=30)).isoformat(), 'legacy_grace': True})

    def move(self, table, identifier, revision=None):
        require(table in KINDS, 'INVALID_ENTITY', '此对象不能移入回收站。')
        with self.store.connection(write=True) as db:
            self.service.require_available(db, table, identifier, allow_archived=True)
            row = self.store.get(db, table, identifier)
            require(revision is None or row['revision'] == revision, 'REVISION_CONFLICT', '资料已变化，请刷新。', 409)
            info = self.describe(db, table, identifier)
            task_ids = {m['id'] for m in info['members'] if m['table'] == 'tasks'}
            if table == 'groups':
                task_ids.add(row['task_id'])
            require(not any(r['task_id'] in task_ids and r['state'] in ('queued', 'running') for r in self.store.list(db, 'runs')),
                    'RUN_ACTIVE', '请先停止涉及任务的处理，再移入回收站。', 409)
            group_ids = {m['id'] for m in info['members'] if m['table'] == 'groups'}
            require(not any(s['state'] == 'active' and
                    (s.get('group_id') in group_ids or table != 'groups' and s.get('task_id') in task_ids)
                    for s in self.store.list(db, 'capture_sessions')),
                    'CAPTURE_ACTIVE', '请先结束涉及班级的流式采集，再移入回收站；已确认照片和未确认草稿会保留。', 409)
            stamp = now()
            entry = self.store.put(db, 'trash_entries', {**info, 'state': 'deleted', 'deleted_at': stamp,
                'expires_at': (datetime.fromisoformat(stamp) + timedelta(days=30)).isoformat()})
            for member in info['members']:
                obj = self.store.get(db, member['table'], member['id'])
                self.store.put(db, member['table'], {**obj, 'state': 'deleted', 'trash_entry_id': entry['id']})
            self.store.event(db, 'trash.moved', entry, counts=info['counts'])
            return entry

    def rows(self):
        with self.store.connection() as db:
            items = [e for e in self.store.list(db, 'trash_entries') if e['state'] in ('deleted', 'purging')]
            items.sort(key=lambda e: (e.get('deleted_at') or e['created_at'], e['created_at'], e['id']), reverse=True)
            return {'items': items,
                    'policy': '移入30天后自动彻底删除；程序关闭期间到期的资料，下次打开此资料库时清理。'}

    def restore_object(self, table, identifier):
        with self.store.connection() as db:
            entries = [e for e in self.store.list(db, 'trash_entries', state='deleted')
                       if any(m['table'] == table and m['id'] == identifier for m in e['members'])]
        require(entries, 'TRASH_NOT_FOUND', '回收站记录不存在。')
        return self.restore(entries[0]['id'])

    def restore(self, identifier, destination=None):
        with self.store.connection(write=True) as db:
            restored = set()
            def restore_one(key):
                if key in restored:
                    return
                e = self.store.get(db, 'trash_entries', key)
                if e['state'] == 'restored':
                    return
                require(e['state'] == 'deleted', 'PURGE_STARTED', '该资料已开始彻底清理，不能恢复。', 409)
                for m in e['members']:
                    obj = self.store.get(db, m['table'], m['id'], optional=True)
                    require(obj is not None, 'PARENT_PURGED', '资料上级已被清理。')
                    if m['id'] == e['object_id'] and destination is not None and m['table'] in ('folders', 'tasks'):
                        if destination:
                            self.service.require_available(db, 'folders', destination)
                        obj['parent_id'] = destination or None
                    parent_table = 'tasks' if m['table'] == 'groups' else 'folders'
                    if obj.get('parent_id'):
                        parent = self.store.get(db, parent_table, obj['parent_id'])
                        if parent['state'] == 'deleted' and parent.get('trash_entry_id') != key:
                            require(parent.get('trash_entry_id'), 'PARENT_DELETED', '请先恢复上级资料。')
                            restore_one(parent['trash_entry_id'])
                    self.store.put(db, m['table'], {**obj, 'state': m['state'], 'trash_entry_id': None})
                self.store.put(db, 'trash_entries', {**e, 'state': 'restored', 'restored_at': now()})
                restored.add(key)
            restore_one(identifier)
            self.store.event(db, 'trash.restored', restored_entries=len(restored))
            return {'restored': len(restored)}

    def deletion_set(self, db, entry, included=()):
        rows = {t: self.store.list(db, t) for t in TABLES}
        selected = {t: set() for t in TABLES}
        for m in entry['members']:
            selected[m['table']].add(m['id'])
        # A separately deleted descendant retains its own retention deadline.
        for other in rows['trash_entries']:
            if other['id'] == entry['id'] or other['id'] in included or other['state'] != 'deleted':
                continue
            for m in other['members']:
                parent_table = TABLES[m['table']]
                if parent_table and m.get('parent_id') in selected[parent_table]:
                    require(datetime.fromisoformat(other['expires_at']) <= datetime.now(timezone.utc),
                            'CHILD_RETENTION', '下级资料仍在独立的30天保留期内，请先恢复或单独清理下级。', 409)
                    # Even expired entries are cleaned independently before their parent.
                    raise Problem('CHILD_RETENTION', '请先清理已到期的下级资料。', 409)
        changed = True
        while changed:
            changed = False
            for table, items in rows.items():
                if table in ('assets', 'trash_entries', 'purge_jobs', 'usage_ledger', 'prompt_versions', 'image_operation_groups', 'resources', 'archive_jobs'):
                    continue
                for row in items:
                    parent = TABLES[table]
                    owns = row.get('task_id') in selected['tasks'] or row.get('group_id') in selected['groups']
                    owns |= bool(parent and row.get('parent_id') in selected[parent])
                    if owns and row['id'] not in selected[table]:
                        selected[table].add(row['id']); changed = True
        return rows, selected

    def plan(self, db, entry, included=()):
        rows, selected = self.deletion_set(db, entry, included)
        doomed_pages = selected['pages']
        # Batch receipts may span groups. Purging one group removes the shared
        # gallery undo receipt (individual surviving page histories remain).
        # This avoids retaining removed paper tags or dangling page references.
        for operation in rows['review_batches']:
            if any(c['id'] in doomed_pages for c in operation.get('changes',[])):
                selected['review_batches'].add(operation['id'])
        for job in rows['review_batch_jobs']:
            if job.get('batch_id') in selected['review_batches'] or any(p['id'] in doomed_pages for p in job.get('targets',[])):
                selected['review_batch_jobs'].add(job['id'])
        for report in rows['report_snapshots']:
            if any(p['page_id'] in doomed_pages for p in report.get('pages', [])):
                selected['report_snapshots'].add(report['id'])
        for export in rows['exports']:
            if export['parent_id'] in selected['report_snapshots']:
                selected['exports'].add(export['id'])
        asset_map = {a['id']: a for a in rows['assets']}
        all_ids = set(asset_map)
        candidates, live = set(), set()
        paths, live_paths = set(), set()
        for table, items in rows.items():
            if table in ('assets', 'trash_entries', 'purge_jobs', 'usage_ledger', 'image_operation_groups', 'resources', 'archive_jobs'):
                continue
            for row in items:
                refs = set(strings(row))
                if row['id'] in selected[table]:
                    candidates |= refs & all_ids
                    paths |= {v for v in refs if v.startswith(('assets/', 'attempt-artifacts/'))}
                else:
                    live |= refs & all_ids
                    live_paths |= {v for v in refs if v.startswith(('assets/', 'attempt-artifacts/'))}
        def expand(ids):
            while True:
                more = {ref for aid in ids for ref in strings(asset_map.get(aid, {})) if ref in all_ids}
                if more <= ids:
                    return ids
                ids |= more
        candidates = expand(candidates)
        bases = {asset_map[a].get('base_asset_id', a) for a in candidates}
        candidates = expand(candidates | {key for key, a in asset_map.items() if a.get('base_asset_id', key) in bases})
        live = expand(live | (all_ids - candidates))
        dead = candidates - live
        selected['assets'] |= dead
        # Historical image operation metadata is tied to the assets it describes.
        for row in rows['image_operation_groups']:
            refs = set(strings(row)) & all_ids
            if refs and refs <= dead:
                selected['image_operation_groups'].add(row['id'])
        def file_refs(value):
            return {v for v in strings(value) if v.startswith('assets/') or v.startswith('attempt-artifacts/')}
        paths |= set().union(*(file_refs(asset_map[a]) for a in dead)) if dead else set()
        protected = set().union(*(file_refs(a) for key, a in asset_map.items() if key not in dead)) if asset_map else set()
        paths -= protected | live_paths
        cache_root=(self.store.root/'cache'/'annotated').resolve()
        for page_id in selected['pages']:
            folder=(cache_root/page_id).resolve()
            require(folder.parent==cache_root,'INVALID_CACHE_PATH','缓存路径无效。')
            paths |= {p.relative_to(self.store.root).as_posix() for p in folder.glob('*.png') if p.is_file()}
        files = []
        for relative in sorted(paths):
            path = (self.store.root / relative).resolve()
            require(path.is_relative_to(self.store.root) and (path.parts[len(self.store.root.parts)] in ('assets', 'attempt-artifacts') or relative.startswith('cache/annotated/')),
                    'INVALID_ASSET_PATH', '清理路径超出允许范围。')
            from .resources import Resources
            resource = Resources(self.store).record(relative, db)
            if resource:
                files.append({'path': relative, 'bytes': resource['bytes']})
            elif path.is_file():
                files.append({'path': path.relative_to(self.store.root).as_posix(), 'bytes': path.stat().st_size})
        return rows, selected, files

    def ordered(self, db, identifiers):
        require(identifiers, 'TRASH_SELECTION', '请选择待清理资料。')
        entries = [self.store.get(db, 'trash_entries', key) for key in dict.fromkeys(identifiers)]
        require(all(e['state'] == 'deleted' for e in entries), 'PURGE_STARTED', '选择的资料已发生变化，请刷新。', 409)
        result = []
        while entries:
            leaves = [e for e in entries if not any(
                m.get('parent_id') in {v['id'] for v in e['members']}
                for child in entries if child['id'] != e['id'] for m in child['members'])]
            require(leaves, 'TRASH_HIERARCHY', '回收站层级异常，不能安全清理。')
            result.extend(leaves)
            entries = [e for e in entries if e not in leaves]
        return result

    def preview_many(self, identifiers):
        with self.store.connection() as db:
            entries = self.ordered(db, identifiers)
            combined = {'id': '', 'members': [m for e in entries for m in e['members']]}
            _, _, files = self.plan(db, combined, {e['id'] for e in entries})
            return {'items': entries, 'exclusive_bytes': sum(f['bytes'] for f in files), 'files': len(files)}

    def purge_many(self, identifiers):
        preview = self.preview_many(identifiers)
        for entry in preview['items']:
            self.purge(entry['id'])
        return {'ok': True, 'purged': len(preview['items'])}

    def preview(self, identifier):
        with self.store.connection() as db:
            entry = self.store.get(db, 'trash_entries', identifier)
            require(entry['state'] == 'deleted', 'PURGE_STARTED', '资料已经进入清理。')
            _, _, files = self.plan(db, entry)
            return {**entry, 'exclusive_bytes': sum(f['bytes'] for f in files), 'files': len(files)}

    def purge(self, identifier):
        with self.store.connection(write=True, purge=True) as db:
            db.execute('PRAGMA defer_foreign_keys=ON')
            entry = self.store.get(db, 'trash_entries', identifier)
            require(entry['state'] == 'deleted', 'PURGE_STARTED', '该项已清理或正在清理。', 409)
            rows, selected, files = self.plan(db, entry)
            affected_tasks = selected['tasks'] | {r['task_id'] for r in rows['groups'] if r['id'] in selected['groups']}
            require(not any(r['state'] in ('queued', 'running') and r['task_id'] in affected_tasks for r in rows['runs']),
                    'RUN_ACTIVE', '任务正在处理，稍后再清理。', 409)
            for a in rows['attempts']:
                if a['id'] in selected['attempts']:
                    self.store.put(db, 'usage_ledger', {k: a.get(k) for k in
                        ('id', 'task_id', 'group_id', 'state', 'purpose', 'elapsed_ms', 'usage', 'prompt_version_id', 'schema_version', 'started_at', 'ended_at')} | {'label': '已清理任务的历史用量'})
            # Preserve unaffected pages from mixed reports as explicitly partial snapshots.
            for report in rows['report_snapshots']:
                if report['id'] not in selected['report_snapshots'] or report['task_id'] in selected['tasks']:
                    continue
                remaining = [p for p in report.get('pages', []) if p['page_id'] not in selected['pages']]
                if remaining:
                    stats = report.get('statistics', {})
                    kept_rows = [r for r in stats.get('rows', []) if r['page_id'] not in selected['pages']]
                    self.store.put(db, 'report_snapshots', {'parent_id': report['parent_id'], 'task_id': report['task_id'],
                        'name': '清理后保留部分（非原始完整报告）', 'partial': True, 'pages': remaining,
                        'statistics': {'rows': kept_rows, 'group_ids': sorted({r['group_id'] for r in kept_rows})}})
            for run in rows['runs']:
                if run['id'] not in selected['runs'] and set(run.get('group_ids', [])) & selected['groups']:
                    self.store.put(db, 'runs', {**run, 'group_ids': [g for g in run['group_ids'] if g not in selected['groups']], 'state': 'superseded'})
            for task in rows['tasks']:
                if task['id'] in selected['tasks']:continue
                history=task.get('batch_review_history',[]);removed=selected['review_batches']
                if any(identifier in removed for identifier in history):
                    cursor=task.get('batch_review_cursor',-1)
                    retained=[identifier for identifier in history if identifier not in removed]
                    self.store.put(db,'tasks',{**task,'batch_review_history':retained,
                        'batch_review_cursor':sum(identifier not in removed for identifier in history[:cursor+1])-1})
            for table, identifiers in selected.items():
                for key in identifiers:
                    db.execute(f'DELETE FROM {table} WHERE id=?', (key,))
            # Response caches can contain full imported records; remove affected responses.
            doomed = set().union(*selected.values())
            for row in db.execute('SELECT key,response FROM idempotency').fetchall():
                if set(strings(json.loads(row['response']))) & doomed:
                    db.execute('DELETE FROM idempotency WHERE key=?', (row['key'],))
            for tid in selected['tasks']:
                db.execute('DELETE FROM events WHERE task_id=?', (tid,))
            job = self.store.put(db, 'purge_jobs', {'state': 'pending', 'files': files, 'freed_bytes': 0})
            self.store.put(db, 'trash_entries', {**entry, 'state': 'purging', 'purge_job_id': job['id']})
            self.store.event(db, 'trash.purge_started', count=sum(len(v) for v in selected.values()))
        self.finish_jobs()
        return {'ok': True, 'estimated_bytes': sum(f['bytes'] for f in files)}

    def finish_jobs(self):
        with self.store.connection() as db:
            jobs = self.store.list(db, 'purge_jobs', state='pending')
        for job in jobs:
            pending, freed = [], job.get('freed_bytes', 0)
            for item in job['files']:
                path = (self.store.root / item['path']).resolve()
                require(path.is_relative_to(self.store.root) and (item['path'].split('/')[0] in ('assets', 'attempt-artifacts') or item['path'].startswith('cache/annotated/')),
                        'INVALID_ASSET_PATH', '清理路径超出资料库。')
                try:
                    from .resources import Resources
                    Resources(self.store).purge(item['path'])
                    freed += item['bytes']
                except OSError:
                    pending.append(item)
            with self.store.connection(write=True) as db:
                self.store.put(db, 'purge_jobs', {**job, 'files': pending, 'freed_bytes': freed, 'state': 'pending' if pending else 'completed'})
                if not pending:
                    for e in self.store.list(db, 'trash_entries', state='purging'):
                        if e.get('purge_job_id') == job['id']:
                            # Tombstone keeps no teacher names or original hierarchy.
                            self.store.put(db, 'trash_entries', {'id': e['id'], 'state': 'purged', 'purged_at': now(), 'freed_bytes': freed})
        if jobs:
            import sqlite3
            from contextlib import closing
            try:
                with closing(sqlite3.connect(self.store.path, timeout=.5)) as db:
                    db.execute('VACUUM')
            except sqlite3.OperationalError:
                pass  # Busy readers may defer physical database compaction.

    def sweep(self):
        from .reference_versions import sweep as sweep_versions
        sweep_versions(self.service)
        self.finish_jobs()
        entries = self.rows()['items']
        for e in reversed(entries):
            if e['state'] == 'deleted' and datetime.fromisoformat(e['expires_at']) <= datetime.now(timezone.utc):
                try:
                    self.purge(e['id'])
                except Exception as error:
                    self.service.diagnostics.record('TRASH_CLEANUP_DEFERRED', str(error), level='info', component='trash', entry_id=e['id'])
