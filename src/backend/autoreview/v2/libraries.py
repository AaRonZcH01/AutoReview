"""Library operations never overwrite a source library or coalesce teaching records."""
from __future__ import annotations
import json
import sqlite3
import shutil
import hashlib
from contextlib import closing
from pathlib import Path
from .core import Store, TABLES, require, uid, now, dumps, digest, atomic_write


def inspect_library(path):
    root = Path(path).expanduser().resolve()
    require((root / 'library.json').is_file() and (root / 'database/autoreview.sqlite').is_file(),
            'LIBRARY_NOT_FOUND', '请选择包含 library.json 和 database 的资料库文件夹。')
    manifest = json.loads((root / 'library.json').read_text('utf-8'))
    require(manifest.get('schema_version') in (2,3,4,5), 'LIBRARY_VERSION', '资料库版本不兼容。')
    with closing(sqlite3.connect((root / 'database/autoreview.sqlite').as_uri() + '?mode=ro', uri=True)) as db:
        require(db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', 'LIBRARY_DAMAGED', '资料库完整性校验失败。')
        counts = {table: db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in ('tasks', 'groups', 'pages')}
    return {'path': str(root), 'library_id': manifest['library_id'], **counts}


def merge_library(service, path):
    store = service.store
    info = inspect_library(path)
    source = Path(info['path'])
    require(not source.is_relative_to(store.root) and not store.root.is_relative_to(source),
            'LIBRARY_NESTED', '来源资料库必须与当前资料库分离。')
    require(info['library_id'] != store.manifest['library_id'], 'SAME_LIBRARY', '不能合并当前资料库或其迁移副本。')
    # A read transaction supplies a consistent DB snapshot even when the source is open.
    with closing(sqlite3.connect((source / 'database/autoreview.sqlite').as_uri() + '?mode=ro', uri=True)) as origin:
        origin.row_factory = sqlite3.Row
        origin.execute('BEGIN')
        tables = {r[0] for r in origin.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        rows = {t: [dict(r) for r in origin.execute(f'SELECT * FROM {t} ORDER BY id')] for t in TABLES if t in tables}
        events = [dict(r) for r in origin.execute('SELECT * FROM events ORDER BY event_id')]
        counters = [dict(r) for r in origin.execute('SELECT * FROM counters')]
    fingerprint = digest({'library': info['library_id'], 'rows': rows, 'events': events})
    key = 'merge:' + fingerprint
    request = {'fingerprint': fingerprint}
    with store.connection() as db:
        prior = store.once(db, key, request)
        if prior:
            return {**prior, 'already_imported': True}
    batch = uid()
    # Copy evidence into a private namespace before the single publication transaction.
    namespace = f'assets/m-{batch[:12]}'
    destination = store.root / namespace
    ids = {row['id']: uid() for collection in rows.values() for row in collection}
    def collect_ids(value, context=None):
        if isinstance(value,dict):
            if context != 'options' and isinstance(value.get('id'),str): ids.setdefault(value['id'],uid())
            if isinstance(value.get('option_group_id'),str): ids.setdefault(value['option_group_id'],uid())
            if isinstance(value.get('region_id'),str): ids.setdefault(value['region_id'],uid())
            for k,v in value.items(): collect_ids(v,k)
        elif isinstance(value,list):
            for v in value: collect_ids(v,context)
    for collection in rows.values():
        for row in collection: collect_ids(json.loads(row['data']))
    paths = {}
    for directory in ('assets', 'attempt-artifacts', 'legacy-archives'):
        for original in (source / directory).rglob('*'):
            if not original.is_file():
                continue
            require(original.resolve().is_relative_to(source), 'LIBRARY_LINK', '来源包含指向资料库外的文件链接。')
            relative = original.relative_to(source).as_posix()
            copied = destination / (hashlib.sha256(relative.encode()).hexdigest()[:32] + original.suffix)
            copied.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, copied)
            require(hashlib.sha256(original.read_bytes()).digest() == hashlib.sha256(copied.read_bytes()).digest(),
                    'COPY_VERIFICATION', '来源在复制时发生变化，请停止来源程序后重试。')
            paths[relative] = copied.relative_to(store.root).as_posix()
            paths[str(original)] = str(copied)
    import zipfile
    for row in rows.get('resources', []):
        resource = json.loads(row['data'])
        relative = resource['logical_path']
        archive_path=(source/resource['container']).resolve()
        require(archive_path.is_relative_to(source),'LIBRARY_LINK','归档路径超出来源资料库。')
        with zipfile.ZipFile(archive_path) as archive: content=archive.read(resource['member'])
        require(hashlib.sha256(content).hexdigest()==resource['sha256'],'COPY_VERIFICATION','归档资源校验失败。')
        copied=(destination/(hashlib.sha256(relative.encode()).hexdigest()[:32]+Path(relative).suffix)).resolve()
        require(copied.is_relative_to(destination),'LIBRARY_LINK','归档逻辑路径无效。')
        atomic_write(copied,content)
        paths[relative]=copied.relative_to(store.root).as_posix()
    rows.pop('resources',None)
    rows.pop('archive_jobs',None)
    capture_chains = {json.loads(row['data']).get('continuation_group_id')
                      for row in rows.get('capture_sessions', [])}
    for row in rows.get('assets', []):
        asset = json.loads(row['data'])
        require(all(asset.get(key) in paths for key in ('relative_path','display_path','thumbnail_path')),
                'LIBRARY_MISSING_ASSET', '来源资料库缺少图片文件，未合并任何任务。请先恢复完整备份。')
    def remap(value):
        if isinstance(value, dict):
            return {ids.get(k, k): remap(v) for k, v in value.items()}
        if isinstance(value, list):
            return [remap(v) for v in value]
        if isinstance(value, str):
            return ids.get(value, paths.get(value, value))
        return value
    with store.connection(write=True) as db:
        db.execute('PRAGMA defer_foreign_keys=ON')
        prior = store.once(db, key, request)
        if prior:
            return {**prior, 'already_imported': True}
        folder = store.put(db, 'folders', {'name': f'合并资料 · {now()[:10]}', 'seq': store.sequence(db, 'folders')})
        for table, collection in rows.items():
            for row in collection:
                original_id = row['id']
                row = {**row, 'data': json.loads(row['data'])}
                mapped = remap(row)
                mapped['data']['import_provenance'] = {'library_id': info['library_id'], 'entity_id': original_id, 'merge_id': batch}
                if table in ('folders', 'tasks') and not mapped['parent_id']:
                    mapped['parent_id'] = folder['id']
                if table in ('runs', 'continuation_groups') and mapped['state'] in ('queued', 'running', 'interrupted'):
                    mapped['state'] = 'paused'
                    mapped['data']['intent'] = 'pause'
                    mapped['data']['generation'] = mapped['data'].get('generation', 0) + 1
                # Imported local drafts remain inspectable, but another library's
                # live camera state/account consent is never an authorization to
                # keep collecting or dispatch new frames in this library.
                if table == 'capture_sessions' and mapped['state'] == 'active':
                    mapped['state'] = 'stopped'
                    mapped['data'].update(stopped_at=now(), stop_reason='library_import')
                if table == 'continuation_groups' and original_id in capture_chains:
                    mapped['state'] = 'paused'
                    mapped['data']['user_intent'] = 'stop'
                if table == 'reference_drafts' and mapped['state'] == 'parsing':
                    mapped['state'] = 'draft'
                if table in ('review_batch_jobs','local_image_jobs') and mapped['state'] in ('queued','running'):
                    mapped['state']='interrupted'
                    mapped['data'].update(stage='interrupted',current=None,
                        error_message='从其他资料库导入的未完成本地操作已停止；已提交结果保留。')
                mapped['data'] = dumps(mapped['data'])
                columns = list(mapped)
                db.execute(f'INSERT INTO {table} ({",".join(columns)}) VALUES ({",".join("?" for _ in columns)})', tuple(mapped.values()))
        for event in events:
            event.pop('event_id')
            event['payload'] = json.loads(event['payload'])
            mapped = remap(event)
            mapped['payload'] = dumps(mapped['payload'])
            db.execute(f'INSERT INTO events ({",".join(mapped)}) VALUES ({",".join("?" for _ in mapped)})', tuple(mapped.values()))
        for counter in counters:
            name = counter['name']
            for old, new in ids.items():
                if name.endswith(':' + old):
                    name = name[:-len(old)] + new
                    break
            db.execute('INSERT INTO counters VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=max(value,excluded.value)', (name, counter['value']))
        result = {**info, 'merge_id': batch, 'source_preserved': True, 'auto_resume': False, 'folder_id': folder['id']}
        store.event(db, 'library.merged', source_library_id=info['library_id'], merge_id=batch)
        return store.remember(db, key, request, result)
