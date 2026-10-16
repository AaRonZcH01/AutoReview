"""Verified cold resources. Logical asset paths remain stable across compression."""
import hashlib
import json
import threading
import zipfile
from pathlib import Path
from .core import require, atomic_write, uid, now

_lock = threading.RLock()


class Resources:
    def __init__(self, store):
        self.store = store

    def safe(self, relative):
        path = (self.store.root / relative).resolve()
        require(path.is_relative_to(self.store.root) and not Path(relative).is_absolute() and '..' not in Path(relative).parts,
                'RESOURCE_PATH', '资源路径超出资料库。')
        return path

    def record(self, relative, db=None):
        key = hashlib.sha256(relative.encode()).hexdigest()
        if db is not None:
            return self.store.get(db, 'resources', key, optional=True)
        with self.store.connection() as connection:
            return self.store.get(connection, 'resources', key, optional=True)

    def resolve(self, relative):
        with _lock:
            loose = self.safe(relative)
            row = self.record(relative)
            if not row:
                require(loose.is_file(), 'RESOURCE_MISSING', '资料图片文件缺失，请从备份恢复。', 404)
                return loose
            cache = self.store.root / '.cache/resources' / row['sha256']
            if cache.is_file() and hashlib.sha256(cache.read_bytes()).hexdigest() == row['sha256']:
                return cache
            with zipfile.ZipFile(self.safe(row['container'])) as archive:
                value = archive.read(row['member'])
            require(hashlib.sha256(value).hexdigest() == row['sha256'], 'RESOURCE_CHECKSUM', '归档资源校验失败，原归档保留。')
            atomic_write(cache, value)
            return cache

    def pack(self, relatives, archive_id):
        """Verify the published ZIP before any reader pointer changes or loose deletion."""
        container = f'archives/{archive_id}.zip'
        target = self.safe(container)
        temporary = target.with_suffix('.building')
        target.parent.mkdir(parents=True, exist_ok=True)
        records = []
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for relative in sorted(set(relatives)):
                value = self.resolve(relative).read_bytes()
                member = hashlib.sha256(relative.encode()).hexdigest()
                archive.writestr(member, value)
                records.append({'id': member, 'logical_path': relative, 'container': container, 'member': member,
                                'sha256': hashlib.sha256(value).hexdigest(), 'bytes': len(value)})
        with zipfile.ZipFile(temporary) as archive:
            for row in records:
                require(hashlib.sha256(archive.read(row['member'])).hexdigest() == row['sha256'], 'ARCHIVE_CHECKSUM', '压缩结果校验失败，原资料未移除。')
        temporary.replace(target)
        return records, target.stat().st_size

    def cleanup_loose(self, records):
        for row in records:
            self.safe(row['logical_path']).unlink(missing_ok=True)

    def purge(self, relative):
        """Remove all copies; repack shared ZIPs without deleting live members."""
        with _lock:
            row = self.record(relative)
            if row:
                target = self.safe(row['container'])
                with self.store.connection() as db:
                    remaining = [r for r in self.store.list(db, 'resources') if r['container'] == row['container'] and r['id'] != row['id']]
                if target.exists() and remaining:
                    temp = target.with_suffix('.repack')
                    with zipfile.ZipFile(target) as old, zipfile.ZipFile(temp, 'w', zipfile.ZIP_DEFLATED) as new:
                        for r in remaining:
                            value = old.read(r['member'])
                            require(hashlib.sha256(value).hexdigest() == r['sha256'], 'RESOURCE_CHECKSUM', '归档校验失败，清理已停止。')
                            new.writestr(r['member'], value)
                    with zipfile.ZipFile(temp) as check:
                        require(check.testzip() is None, 'RESOURCE_CHECKSUM', '重压缩校验失败。')
                    temp.replace(target)
                else:
                    target.unlink(missing_ok=True)
                with self.store.connection(write=True) as db:
                    db.execute('DELETE FROM resources WHERE id=?', (row['id'],))
                (self.store.root / '.cache/resources' / row['sha256']).unlink(missing_ok=True)
            self.safe(relative).unlink(missing_ok=True)


def archive_folder(service, folder_id, restore=False):
    store, resources = service.store, Resources(service.store)
    with _lock:
        with store.connection(write=True) as db:
            service.require_available(db, 'folders', folder_id, allow_archived=restore)
            folder = store.get(db, 'folders', folder_id)
            require(folder['state'] == ('archived' if restore else 'active'), 'ARCHIVE_STATE', '文件夹状态已变化，请刷新。')
            folders = {folder_id}
            while True:
                more = {f['id'] for f in store.list(db, 'folders') if f.get('parent_id') in folders}
                if more <= folders: break
                folders |= more
            tasks = {t['id'] for t in store.list(db, 'tasks') if t.get('parent_id') in folders}
            require(not any(r['task_id'] in tasks and r['state'] in ('running','queued','stopping') for r in store.list(db,'runs')),
                    'RUN_ACTIVE', '请先停止此文件夹内的处理。')
            if not restore:
                require(not any(s['task_id'] in tasks and s['state'] == 'active'
                                for s in store.list(db, 'capture_sessions')),
                        'CAPTURE_ACTIVE', '请先结束此文件夹内的流式采集，再归档；未确认草稿仍会保留。', 409)
            job = store.put(db, 'archive_jobs', {'folder_id': folder_id, 'state': 'preparing', 'action': 'restore' if restore else 'archive'})
            from .core import TABLES
            from .trash import strings
            refs = set()
            for table in TABLES:
                if table in ('resources','archive_jobs','assets'): continue
                for row in store.list(db, table):
                    if row.get('task_id') in tasks or (table == 'tasks' and row['id'] in tasks): refs.update(strings(row))
            assets = {a['id']:a for a in store.list(db,'assets')}
            selected = refs & set(assets)
            while True:
                more = {v for key in selected for v in strings(assets[key]) if v in assets}
                if more <= selected: break
                selected |= more
            relatives = {v for key in selected for v in strings(assets[key]) if v.startswith(('assets/','attempt-artifacts/'))}
            relatives |= {v for v in refs if v.startswith('attempt-artifacts/')}
        if restore:
            rows = [resources.record(r) for r in relatives]
            rows = [r for r in rows if r]
            for row in rows:
                atomic_write(resources.safe(row['logical_path']), resources.resolve(row['logical_path']).read_bytes())
            with store.connection(write=True) as db:
                current = store.get(db,'folders',folder_id)
                require(current['revision'] == folder['revision'], 'REVISION_CONFLICT', '文件夹已变化，尚未切换归档状态。',409)
                store.put(db,'folders',{**current,'state':'active'})
                # Keep cold pointers until verified loose copies are all ready.
                for row in rows: db.execute('DELETE FROM resources WHERE id=?',(row['id'],))
                result = store.put(db,'archive_jobs',{**job,'state':'completed','file_count':len(rows)})
            # Containers may be shared by still archived tasks; only delete unreferenced ones.
            with store.connection() as db: live={r['container'] for r in store.list(db,'resources')}
            for container in {r['container'] for r in rows}-live: resources.safe(container).unlink(missing_ok=True)
            compact_containers(store)
            return result
        records, compressed = resources.pack(relatives,job['id'])
        with store.connection(write=True) as db:
            current=store.get(db,'folders',folder_id)
            require(current['revision']==folder['revision'],'REVISION_CONFLICT','文件夹已变化，原资料未移除。',409)
            for row in records: store.put(db,'resources',row)
            store.put(db,'folders',{**current,'state':'archived','archived_at':now(),'archive_id':job['id']})
            result=store.put(db,'archive_jobs',{**job,'state':'cleanup','files':[r['logical_path'] for r in records],
                'original_bytes':sum(r['bytes'] for r in records),'compressed_bytes':compressed,'file_count':len(records)})
        resources.cleanup_loose(records)
        compact_containers(store)
        with store.connection(write=True) as db: result=store.put(db,'archive_jobs',{**result,'state':'completed'})
        return result


def compact_containers(store):
    resources=Resources(store)
    with _lock:
        with store.connection() as db: rows=store.list(db,'resources')
        groups={}
        for r in rows: groups.setdefault(r['container'],[]).append(r)
        for target in (store.root/'archives').glob('*.zip'):
            relative=target.relative_to(store.root).as_posix();live=groups.get(relative,[])
            if not live:
                target.unlink(missing_ok=True);continue
            with zipfile.ZipFile(target) as old:
                if set(old.namelist())=={r['member'] for r in live}: continue
                temporary=target.with_suffix('.repack')
                with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED) as new:
                    for row in live:
                        value=old.read(row['member'])
                        require(hashlib.sha256(value).hexdigest()==row['sha256'],'RESOURCE_CHECKSUM','归档校验失败。')
                        new.writestr(row['member'],value)
            with zipfile.ZipFile(temporary) as check: require(check.testzip() is None,'RESOURCE_CHECKSUM','归档重压缩校验失败。')
            temporary.replace(target)


def recover_archives(service):
    store=service.store; resources=Resources(store)
    with store.connection() as db: jobs=store.list(db,'archive_jobs')
    for job in jobs:
        if job['state']=='cleanup':
            rows=[resources.record(p) for p in job.get('files',[])]
            resources.cleanup_loose([r for r in rows if r])
            with store.connection(write=True) as db: store.put(db,'archive_jobs',{**job,'state':'completed'})
        elif job['state']=='preparing':
            # No pointer was committed; redundant compressed output can be discarded safely.
            resources.safe(f'archives/{job["id"]}.zip').unlink(missing_ok=True)
            resources.safe(f'archives/{job["id"]}.building').unlink(missing_ok=True)
            with store.connection(write=True) as db: store.put(db,'archive_jobs',{**job,'state':'interrupted'})

    compact_containers(store)
