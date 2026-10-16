from __future__ import annotations
import hashlib
import io
import re
import time
from pathlib import Path
from contextlib import nullcontext
from PIL import Image, ImageOps
from .core import Store, MachineSettings, Problem, require, now, uid, atomic_write, digest
from .contracts import Answer, Box, prompt_record, capabilities
from .grading import resolve_page
from .telemetry import Diagnostics


class Service:
    def __init__(self, settings: MachineSettings, store: Store | None = None):
        self.settings = settings
        self.store = store or Store(Path(settings.data['library_location']))
        from .read_cache import ReadCache
        self.read_cache=ReadCache()
        self.diagnostics = Diagnostics(self.store, settings.data)
        from .trash import Trash
        self.trash = Trash(self)
        self.trash.adopt_legacy()
        from .resources import recover_archives
        recover_archives(self)
        with self.store.connection(write=True) as db:
            for purpose in ('student', 'reference', 'writing', 'student_verify'):
                record = prompt_record(purpose)
                if not self.store.get(db, 'prompt_versions', record['id'], optional=True):
                    self.store.put(db, 'prompt_versions', record)

    def close(self):
        self.diagnostics.close()
        # This cache is reconstructible; original and archived resources are preserved.
        for cached in (self.store.root / '.cache/resources').glob('*'):
            if cached.is_file():
                try:
                    cached.unlink()
                except OSError:
                    pass
        self.store.close()

    def snapshot(self):
        with self.store.connection() as db:
            return {**{table: self.store.list(db, table, include_deleted=False) for table in
                    ('folders', 'class_presets', 'tasks', 'groups', 'runs', 'continuation_groups')},
                    'settings': self.settings.data, 'library': self.store.manifest,
                    'archive_jobs': self.store.list(db, 'archive_jobs'),
                    'capabilities': capabilities(), 'trash_count': len(self.store.list(db, 'trash_entries', state='deleted'))}

    def create(self, table, data):
        require(table in ('folders', 'class_presets', 'tasks', 'groups'), 'INVALID_ENTITY', '不支持此创建操作。')
        name = str(data.get('name', '')).strip()
        require(name and len(name) <= 160, 'NAME_REQUIRED', '请输入名称（最多160字）。')
        with self.store.connection(write=True) as db:
            parent = data.get('parent_id') or None
            if table in ('folders', 'tasks') and parent:
                self.require_available(db, 'folders', parent)
            if table == 'groups':
                self.require_available(db, 'tasks', parent)
                task = self.store.get(db, 'tasks', parent)
                data = {**data, 'task_id': task['id'], 'reference_version_id': task.get('reference_version_id'),
                        'next_tag': 1}
            result = self.store.put(db, table, {**data, 'name': name, 'parent_id': parent,
                        'state': 'active', 'seq': self.store.sequence(db, table)})
            self.store.event(db, table + '.created', result)
            if table == 'tasks':
                self.store.put(db, 'reference_drafts', {'id': result['id'], 'parent_id': result['id'],
                    'task_id': result['id'], 'state': 'draft', 'items': [], 'asset_ids': [],
                    'page_count': 1, 'page_labels': ['第1页'], 'writing_rules': [{'id':'hooked-y','description':'y采用下方弯钩手写体；模糊保持待核实，书写问题须教师确认。'},{'id':'handwritten-forms','description':'以课堂自然手写体辨读；a/g单层手写及自然笔画连接可接受。不得按印刷字体模板或词语合法性修正学生原文，模糊须待核实。'}], 'issues': []})
            return result

    def edit(self, table, identifier, data):
        require(table in ('folders', 'class_presets', 'tasks', 'groups'), 'INVALID_ENTITY', '不支持此修改操作。')
        if table == 'folders' and data.get('state') in ('archived','active'):
            with self.store.connection() as db:
                folder = self.store.get(db, 'folders', identifier)
            if data.get('state') == 'archived' or folder['state'] == 'archived':
                from .resources import archive_folder
                return archive_folder(self, identifier, data['state']=='active')
        if data.get('state') == 'deleted':
            return self.trash.move(table, identifier, data.get('revision'))
        if data.get('state') == 'active':
            with self.store.connection() as db:
                old = self.store.get(db, table, identifier)
            if old['state'] == 'deleted':
                return self.trash.restore_object(table, identifier)
        with self.store.connection(write=True) as db:
            old = self.store.get(db, table, identifier)
            self.require_available(db, table, identifier)
            changes = {k: v for k, v in data.items() if k in ('name', 'parent_id', 'state')}
            require(changes.get('state', 'active') in ('active', 'archived', 'deleted'), 'INVALID_STATE', '无效状态。')
            if 'name' in changes:
                require(str(changes['name']).strip(), 'NAME_REQUIRED', '名称不能为空。')
            if 'parent_id' in changes:
                require(table in ('folders', 'tasks') or changes['parent_id'] == old['parent_id'], 'MOVE_NOT_SUPPORTED', '班级属于原任务，不可跨任务移动。')
                if table in ('folders', 'tasks') and changes['parent_id']:
                    self.require_available(db, 'folders', changes['parent_id'])
                cursor = changes['parent_id']
                if table not in ('folders', 'tasks'):
                    cursor = None
                seen = {identifier} if table == 'folders' else set()
                while cursor:
                    require(cursor not in seen, 'FOLDER_CYCLE', '文件夹不能移入自身或其子文件夹。')
                    seen.add(cursor)
                    cursor = self.store.get(db, 'folders', cursor)['parent_id']
            if changes.get('state') == 'deleted':
                if table == 'folders':
                    require(not any(x['parent_id'] == identifier and x['state'] != 'deleted'
                                    for t in ('folders', 'tasks') for x in self.store.list(db, t)),
                            'FOLDER_NOT_EMPTY', '请先移动或删除文件夹内的任务。')
                if table in ('tasks', 'groups'):
                    task_id = identifier if table == 'tasks' else old['task_id']
                    require(not any(r['state'] in ('queued', 'running') for r in self.store.list(db, 'runs', task_id=task_id)),
                            'RUN_ACTIVE', '请先停止该任务的处理。', 409)
            result = self.store.put(db, table, {**old, **changes}, expected=data.get('revision'))
            self.store.event(db, table + '.updated', result, changes=changes)
            return result

    def require_available(self, db, table, identifier, allow_archived=False):
        row = self.store.get(db, table, identifier)
        seen = set()
        while row:
            require(allow_archived or row['state'] != 'archived', 'OBJECT_ARCHIVED', '此资料已归档，只读查看；修改前请先从归档空间恢复。', 409)
            require(row['state'] != 'deleted', 'OBJECT_DELETED', '此资料或其上级已在回收站，请先恢复。', 409)
            require(row['id'] not in seen, 'FOLDER_CYCLE', '资料层级无效。')
            seen.add(row['id'])
            if table == 'groups':
                table, identifier = 'tasks', row['task_id']
            elif table in ('folders', 'tasks') and row.get('parent_id'):
                table, identifier = 'folders', row['parent_id']
            else:
                break
            row = self.store.get(db, table, identifier)

    def add_groups(self, task_id, data, connection=None):
        with (nullcontext(connection) if connection is not None else self.store.connection(write=True)) as db:
            request = {**data, 'task_id': task_id}
            cached = self.store.once(db, data['request_id'], request)
            if cached:
                return cached
            self.require_available(db, 'tasks', task_id)
            task = self.store.get(db, 'tasks', task_id)
            presets = [self.store.get(db, 'class_presets', i) for i in data.get('preset_ids', [])]
            require(all(p['state'] != 'deleted' for p in presets), 'PRESET_DELETED', '所选预设已删除，请刷新。')
            choices = [(p['name'], p['id']) for p in presets]
            custom = str(data.get('custom', '')).strip()
            require(len(custom) <= 160, 'NAME_REQUIRED', '班级名称最多160字。')
            if custom:
                preset = next((p for p in self.store.list(db, 'class_presets', include_deleted=False) if p['name'] == custom), None)
                if data.get('save_preset') and not preset:
                    preset = self.store.put(db, 'class_presets', {'name': custom, 'seq': self.store.sequence(db, 'class_presets')})
                choices.append((custom, preset['id'] if preset else None))
            require(choices, 'GROUP_REQUIRED', '请选择预设班级或填写临时班级。')
            existing = {g['name'] for g in self.store.list(db, 'groups', task_id=task_id, include_deleted=False)}
            added = []
            for name, preset_id in choices:
                if name in existing:
                    continue
                row = self.store.put(db, 'groups', {'parent_id': task_id, 'task_id': task_id,
                    'name': name, 'preset_id': preset_id, 'next_tag': 1, 'reference_version_id': task.get('reference_version_id'),
                    'seq': self.store.sequence(db, 'groups')})
                existing.add(name)
                added.append(row)
            self.store.event(db, 'groups.added', task, count=len(added))
            return self.store.remember(db, data['request_id'], request, {'groups': added})

    def create_task(self, data):
        with self.store.connection(write=True) as db:
            key = 'task:' + data['request_id']
            cached = self.store.once(db, key, data)
            if cached:
                return cached
            name = str(data.get('name', '')).strip()
            require(name and len(name) <= 160, 'NAME_REQUIRED', '请输入任务名称（最多160字）。')
            parent = data.get('parent_id') or None
            if parent:
                self.require_available(db, 'folders', parent)
            task = self.store.put(db, 'tasks', {'name': name, 'parent_id': parent, 'scoring_enabled':bool(data.get('scoring_enabled')), 'seq': self.store.sequence(db, 'tasks')})
            self.store.put(db, 'reference_drafts', {'id': task['id'], 'parent_id': task['id'], 'task_id': task['id'],
                'state': 'draft', 'items': [], 'asset_ids': [], 'page_ids': [], 'page_count': 1,
                'page_labels': ['第1页'], 'writing_rules': [{'id':'hooked-y','description':'y采用下方弯钩手写体；模糊保持待核实，书写问题须教师确认。'},{'id':'handwritten-forms','description':'以课堂自然手写体辨读；a/g单层手写及自然笔画连接可接受。不得按印刷字体模板或词语合法性修正学生原文，模糊须待核实。'}], 'issues': []})
            self.add_groups(task['id'], {**data, 'request_id': data['request_id'] + ':groups'}, db)
            self.store.event(db, 'tasks.created', task)
            return self.store.remember(db, key, data, task)

    def intake_draft(self, task_id, data=None):
        with self.store.connection(write=data is not None) as db:
            self.require_available(db, 'tasks', task_id)
            old = self.store.get(db, 'intake_drafts', task_id, optional=True)
            if data is None:
                return old or {'id': task_id, 'assets': [], 'form': {}, 'revision': 0}
            require(data.get('revision', 0) == (old or {}).get('revision', 0), 'REVISION_CONFLICT', '导入草稿已在其他窗口更新，请重新打开。', 409)
            ids = data.get('assets', [])
            assets = [self.store.get(db, 'assets', a['id']) for a in ids]
            row = self.store.put(db, 'intake_drafts', {'id': task_id, 'parent_id': task_id, 'task_id': task_id,
                'assets': [{**a, 'cohort_position': ids[i].get('cohort_position', i)} for i, a in enumerate(assets)],
                'form': data.get('form', {}), 'state': 'draft'})
            return row

    def save_asset(self, content: bytes, filename: str):
        started = time.perf_counter()
        require(len(content) <= 40 * 1024 * 1024, 'IMAGE_TOO_LARGE', '单张图片不能超过40MB。')
        try:
            with Image.open(io.BytesIO(content)) as original:
                require(original.width * original.height <= 60_000_000, 'IMAGE_TOO_LARGE', '图片像素超过6000万。')
                picture = ImageOps.exif_transpose(original).convert('RGB')
                picture.load()
        except Problem:
            raise
        except Exception as error:
            raise Problem('IMAGE_INVALID', '无法读取图片，请使用 PNG、JPEG 或 WebP。') from error
        sha = hashlib.sha256(content).hexdigest()
        image_id = uid()
        relative = f'assets/{image_id}/original.bin'
        atomic_write(self.store.root / relative, content)
        from .imaging import auto_operations, render
        operations, operation_log = auto_operations(picture, enabled=self.settings.data.get('auto_orient_images', True))
        picture, geometry = render(picture, operations)
        output = io.BytesIO()
        picture.save(output, 'PNG', compress_level=2)
        display_sha = hashlib.sha256(output.getvalue()).hexdigest()
        atomic_write(self.store.root / f'assets/{image_id}/display.png', output.getvalue())
        picture.thumbnail((400, 400))
        output = io.BytesIO()
        picture.save(output, 'JPEG', quality=82)
        atomic_write(self.store.root / f'assets/{image_id}/thumbnail.jpg', output.getvalue())
        with Image.open(self.store.root / f'assets/{image_id}/display.png') as image:
            width, height = image.size
        with self.store.connection(write=True) as db:
            result = self.store.put(db, 'assets', {'id': image_id, 'relative_path': relative,
                'display_path': f'assets/{image_id}/display.png', 'thumbnail_path': f'assets/{image_id}/thumbnail.jpg',
                'sha256': sha, 'filename': Path(filename).name, 'width': width, 'height': height, 'bytes': len(content),
                'operations': operations, 'operation_log': [{**entry, 'operation_id': uid(), 'created_at': now()} for entry in operation_log],
                'geometry': geometry, 'base_asset_id': image_id, 'display_sha256': display_sha,
                'processing_hash': digest({'source': sha, 'operations': operations, 'algorithm': geometry['algorithm']}),
                'import_ms': round((time.perf_counter() - started) * 1000)})
            self.store.event(db, 'asset.imported', result, bytes=len(content), width=width, height=height,
                             elapsed_ms=result['import_ms'])
            return result

    def prepare_asset(self, identifier, data, status=None):
        from .imaging import render, auto_operations, rotate_exact
        def stage(value):
            if status: status(value)
        with self.store.connection() as db:
            old = self.store.get(db, 'assets', identifier)
        operations = list(old.get('operations', []))
        log = list(old.get('operation_log', []))
        if data.get('action') == 'restore':
            operations = []
            log.append({'operation': 'restore', 'source': 'manual', 'status': 'completed', 'reason': '恢复原件，既有处理版本仍保留。'})
        elif data.get('action') == 'automatic':
            stage('reading')
            with Image.open(self.store.asset_path(old)) as source:
                picture = ImageOps.exif_transpose(source).convert('RGB')
            stage('processing')
            operations, log = auto_operations(picture, enabled=self.settings.data.get('auto_orient_images', True))
        elif data.get('action') == 'disable':
            index = int(data.get('index', -1))
            require(0 <= index < len(operations), 'OPERATION_NOT_FOUND', '处理步骤不存在。')
            removed = operations[index:]
            # Downstream crop/corners belong to the removed coordinate system. Do not reuse them.
            operations = operations[:index] + [o for o in removed[1:] if o['kind'] == 'rotate']
            log.append({'operation': 'disable', 'source': 'manual', 'status': 'completed', 'index': index,
                        'reason': '已停用此步骤，并撤去依赖旧坐标系的后续裁剪/透视；旋转从原件重新计算。'})
        else:
            operation = data.get('operation', {})
            require(operation.get('kind') in ('rotate', 'perspective', 'crop'), 'IMAGE_OPERATION', '不支持的图片操作。')
            if operation.get('kind') == 'rotate' and operation.get('angle') in (-90,90,180):
                operation = {**operation, 'pixel_preserving': True}
            operations.append({**operation, 'source': 'manual'})
            log.append({'operation': operation['kind'], 'source': 'manual', 'status': 'completed', 'parameters': operation})
        from .imaging import VERSION
        processing_hash = digest({'source': old['sha256'], 'operations': operations, 'algorithm': VERSION})
        with self.store.connection() as db:
            duplicate = next((a for a in self.store.list(db, 'assets') if a.get('base_asset_id') == old.get('base_asset_id',old['id'])
                              and a.get('processing_hash') == processing_hash), None)
            if duplicate:
                stage('cached')
                return duplicate
        stage('reading')
        if operations and operations[-1].get('pixel_preserving') and data.get('operation'):
            with Image.open(self.asset(old['id'])) as source:
                picture = source.convert('RGB')
            stage('processing')
            rendered, geometry = rotate_exact(picture, old['geometry'], operations[-1]['angle'])
        else:
            with Image.open(self.store.asset_path(old)) as source:
                picture = ImageOps.exif_transpose(source).convert('RGB')
            stage('processing')
            rendered, geometry = render(picture, operations)
        identifier = uid()
        display, thumbnail = f'assets/{identifier}/display.png', f'assets/{identifier}/thumbnail.jpg'
        stage('saving')
        buffer = io.BytesIO(); rendered.save(buffer, 'PNG', compress_level=2)
        display_sha = hashlib.sha256(buffer.getvalue()).hexdigest()
        atomic_write(self.store.root / display, buffer.getvalue())
        rendered.thumbnail((400,400)); buffer = io.BytesIO(); rendered.save(buffer,'JPEG',quality=82)
        atomic_write(self.store.root / thumbnail, buffer.getvalue())
        with self.store.connection(write=True) as db:
            result = self.store.put(db, 'assets', {**old, 'id': identifier, 'display_path': display, 'thumbnail_path': thumbnail,
                'parent_asset_id': old['id'], 'base_asset_id': old.get('base_asset_id', old['id']), 'operations': operations,
                'operation_log': [{**entry, 'operation_id': entry.get('operation_id') or uid(), 'created_at': entry.get('created_at') or now()} for entry in log],
                'geometry': geometry, 'width': geometry['size'][0], 'height': geometry['size'][1], 'display_sha256': display_sha,
                'processing_hash': digest({'source': old['sha256'], 'operations': operations, 'algorithm': geometry['algorithm']})})
            self.store.event(db, 'image.prepared', result, parent_asset_id=old['id'], algorithm=geometry['algorithm'], elapsed_ms=geometry['elapsed_ms'])
            return result

    def asset(self, identifier, variant='display'):
        with self.store.connection() as db:
            asset = self.store.get(db, 'assets', identifier)
        key = {'display': 'display_path', 'thumbnail': 'thumbnail_path', 'original': 'relative_path'}.get(variant)
        require(key, 'ASSET_VARIANT', '图片版本不存在。')
        return self.store.asset_path({'relative_path': asset[key]})

    def batch_images(self, data, progress=None, status=None):
        def stage(value, page=None, done=0):
            if status: status(stage=value,current_page_id=(page or {}).get('id'),
                current_label=labels.get((page or {}).get('id')),completed_count=done,total_count=len(pages))
        key='image-batch:'+data.get('request_id','')
        with self.store.connection() as db:
            if data.get('request_id'):
                cached=self.store.once(db,key,data)
                if cached is not None: return cached
            if data.get('undo_id'):
                group = self.store.get(db,'image_operation_groups',data['undo_id'])
                require(group['state']=='applied','NO_HISTORY','该批操作已经撤销。')
                pages = [self.store.get(db,'pages',p['page_id']) for p in group['changes']]
                require(all(p['image_version_id']==c['after_version'] for p,c in zip(pages,group['changes'])), 'REVISION_CONFLICT','部分图片已有后续修改，不能整批撤销。',409)
                targets = [c['before_asset'] for c in group['changes']]
            else:
                pages = [self.store.get(db,'pages',entry['id']) for entry in data.get('pages',[])]
                require(pages and len({p['id'] for p in pages})==len(pages),'PAGE_SCOPE','请选择不重复的图片。')
                require(all(p['revision']==entry['revision'] for p,entry in zip(pages,data['pages'])),'REVISION_CONFLICT','图片已变化，请刷新。',409)
                require(data.get('angle') in (-90,90,180),'ROTATION_RANGE','批量处理仅支持明确方向旋转；透视需逐图检查。')
                targets = []
            require(len({p['task_id'] for p in pages})==1,'PAGE_SCOPE','一次操作仅限同一任务。')
            require(not any(r['state'] in ('queued','running') for r in self.store.list(db,'runs',task_id=pages[0]['task_id'])), 'RUN_ACTIVE','请先停止本任务的处理。')
            before = [self.store.get(db,'image_versions',p['image_version_id']) for p in pages]
            labels = {p['id']: f"{self.store.get(db,'submissions',p['parent_id'])['paper_tag']} / 第{p['slot']}页" for p in pages}
        if not targets:
            for i,v in enumerate(before):
                targets.append(self.prepare_asset(v['asset_id'],{'operation':{'kind':'rotate','angle':data['angle']}},
                    lambda value,i=i:stage(value,pages[i],i))['id'])
                if progress: progress(i+1,len(before))
        stage('applying',done=len(pages))
        with self.store.connection(write=True) as db:
            if data.get('request_id'):
                cached=self.store.once(db,key,data)
                if cached is not None: return cached
            self.require_available(db,'tasks',pages[0]['task_id'])
            require(not any(r['state'] in ('queued','running') for r in self.store.list(db,'runs',task_id=pages[0]['task_id'])), 'RUN_ACTIVE','图片处理期间任务已开始批改，未切换图片。',409)
            changes = []
            for page,old,target in zip(pages,before,targets):
                current = self.store.get(db,'pages',page['id'])
                require(current['revision']==page['revision'],'REVISION_CONFLICT','批量处理期间图片发生变化，未切换任何图片。',409)
                version = self.store.put(db,'image_versions',{'parent_id':page['id'],'task_id':page['task_id'],'group_id':page['group_id'],
                    'asset_id':target,'version':old['version']+1,'replaces':old['id']})
                self.store.put(db,'pages',{**page,'image_version_id':version['id'],'generation':page['generation']+1,'state':'pending',
                    'observation_id':None,'preparation':None,'overrides':{},'reviewed':False,'history':[],'history_cursor':-1})
                self.update_submission(db,page['parent_id'])
                changes.append({'page_id':page['id'],'before_asset':old['asset_id'],'before_version':old['id'],'after_version':version['id']})
            if data.get('undo_id'):
                self.store.put(db,'image_operation_groups',{**group,'state':'undone'})
            result = self.store.put(db,'image_operation_groups',{'task_id':pages[0]['task_id'],'state':'applied','changes':changes,
                'action':'undo' if data.get('undo_id') else 'rotate','angle':data.get('angle')})
            self.store.event(db,'images.batch_operation',result,count=len(pages),action=result['action'])
            return self.store.remember(db,key,data,result) if data.get('request_id') else result

    def batch_prepare_images(self, data, progress=None, status=None, job_id=None):
        # Position previews are independent, unlike an atomic image replacement.
        # Completed previews stay usable if a later page fails or the app stops.
        entries = data.get('pages', [])
        result = []
        for i, entry in enumerate(entries):
            with self.store.connection() as db:
                page = self.store.get(db, 'pages', entry['id'])
                require(page['revision'] == entry['revision'], 'REVISION_CONFLICT', '待定位图片已变化，请刷新范围。', 409)
                label = f"{self.store.get(db,'submissions',page['parent_id'])['paper_tag']} / 第{page['slot']}页"
            if status: status(stage='locating',current_page_id=page['id'],current_label=label,
                              completed_count=i,total_count=len(entries))
            result.append(self.prepare_page(page['id'], {'reference_version_id':data.get('reference_version_id'),
                          'force':bool(data.get('force'))}, job_id=job_id))
            if progress: progress(i+1,len(entries))
        return {'action':'prepare','pages':result}

    def preparation_busy(self, db, page):
        active = any(r['state'] in ('queued','running') for r in self.store.list(db,'runs',task_id=page['task_id']))
        if not active:
            return False
        isolated = page.get('replacement_pending') and page['state']=='pending' and not any(page.get(k) for k in ('observation_id','reviewed','overrides','history','annotations'))
        if not isolated:
            return True
        # Replaced generations are fenced and withheld from their old queue.
        # Other photographs may continue; never edit a current in-flight input.
        return any(w['state'] in ('pending','running') and
                   ((w.get('page_id')==page['id'] and w.get('generation')==page['generation']) or
                    w.get('dependency_versions',{}).get(page['id'])==page['image_version_id'])
                   for w in self.store.list(db,'work_items',task_id=page['task_id']))

    def prepare_page(self, page_id, data, *, job_id=None):
        from .imaging import align, project, corners
        with self.store.connection() as db:
            page = self.store.get(db, 'pages', page_id)
            self.require_available(db, 'groups', page['group_id'])
            from .scoring import gate
            gate(self.store, db, page['task_id'])
            require(not any(j['state'] in ('queued','running') and j['id']!=job_id for j in self.store.list(db,'local_image_jobs',task_id=page['task_id'])),'IMAGE_JOB_ACTIVE','图片旋转正在进行，完成后再定位。',409)
            require(not self.preparation_busy(db,page),
                    'RUN_ACTIVE', '请先停止本任务的处理，再调整图片定位。', 409)
            version = self.store.get(db, 'image_versions', page['image_version_id'])
            group = self.store.get(db, 'groups', page['group_id'])
            ref = self.store.get(db, 'reference_versions', data.get('reference_version_id') or group['reference_version_id'])
        require(ref['task_id'] == page['task_id'], 'REFERENCE_SCOPE', '参考答案不属于当前任务。')
        stamp = digest({'image_version': version['id'], 'reference': ref['id'], 'slot': page['slot'], 'seq': page['seq']})
        if data.get('action') != 'confirm' and not data.get('force') and (page.get('preparation') or {}).get('stamp') == stamp:
            return page
        if data.get('action') == 'confirm':
            preparation = page.get('preparation') or {}
            require(preparation.get('stamp') == stamp and data.get('stamp') == stamp and page['revision'] == data.get('revision'),
                    'PREPARATION_CHANGED', '图片、位置或参考答案已变化，请重新预览。', 409)
            zones = data.get('zones', preparation['zones'])
            require(not preparation.get('boundary_warnings') or data.get('boundary_ack'),
                    'REGION_BOUNDARY_REVIEW', '部分自动定位区域超出照片，请逐张核对边缘是否缺题后确认。')
            expected = {a['id'] for a in ref['items'] if a['slot'] == page['slot'] and not a.get('excluded')}
            require(set(zones) == expected, 'REGION_MISMATCH', '题目与区域必须一一对应。')
            for identifier, zone in zones.items():
                if zone is not None:
                    Box.model_validate(zone)
                else:
                    require(not ref.get('asset_ids'), 'REGION_REQUIRED', '范本题目缺少完整作答区域。')
            preparation = {**preparation, 'zones': zones, 'manual_edited': zones != preparation.get('previous_zones', zones), 'confirmed': True, 'confirmed_at': now()}
        else:
            answers = [a for a in ref['items'] if a['slot'] == page['slot'] and not a.get('excluded')]
            template = ref.get('asset_ids', [])
            mapping = align(self.asset(template[page['slot']-1]), self.asset(version['asset_id']),
                            [a.get('evidence_box') or (a.get('answer_zone') or a.get('box') if a['kind'] not in ('choice','letter_choice','marked_choice','matching','ordering') else None) for a in answers]) if template else {
                                'matrix': [[1,0,0],[0,1,0],[0,0,1]], 'status': 'manual_text', 'reason': '纯文字答案：按页与题号识别。'}
            zones, polygons, boundary_warnings = {}, {}, []
            for answer in answers:
                box = answer.get('answer_zone') or answer.get('box')
                pts = project(corners(box), mapping['matrix']) if box else None
                polygons[answer['id']] = pts
                zones[answer['id']] = {'x1': min(p[0] for p in pts), 'y1': min(p[1] for p in pts),
                    'x2': max(p[0] for p in pts), 'y2': max(p[1] for p in pts)} if pts else None
                zone = zones[answer['id']]
                if zone and any(v < 0 or v > 1 for v in zone.values()):
                    boundary_warnings.append({'item_id': answer['id'], 'number': answer['number'],
                                              'projected_box': zone, 'message': '自动定位触及图片外缘，请检查缺题、裁切及完整答题区。'})
                    clipped = {k: max(0, min(1, v)) for k, v in zone.items()}
                    if clipped['x2'] > clipped['x1'] and clipped['y2'] > clipped['y1']:
                        zones[answer['id']] = clipped
            preparation = {'stamp': stamp, 'reference_version_id': ref['id'], 'image_version_id': version['id'],
                'mapping': mapping, 'zones': zones, 'polygons': polygons, 'confirmed': False,
                'boundary_warnings': boundary_warnings,
                'previous_zones': (page.get('preparation') or {}).get('zones', zones)}
        with self.store.connection(write=True) as db:
            current = self.store.get(db, 'pages', page_id)
            require(not any(j['state'] in ('queued','running') and j['id']!=job_id for j in self.store.list(db,'local_image_jobs',task_id=page['task_id'])),'IMAGE_JOB_ACTIVE','图片旋转正在进行，未覆盖新图片的定位。',409)
            require(not self.preparation_busy(db,current),
                    'RUN_ACTIVE','定位期间任务已开始批改，未覆盖定位状态。',409)
            if current['revision'] != page['revision'] and data.get('action') != 'confirm' and not data.get('force') and (current.get('preparation') or {}).get('stamp') == stamp:
                return current
            require(current['revision'] == page['revision'], 'REVISION_CONFLICT', '图片已发生变化，请重新预览。', 409)
            changed_geometry = preparation['confirmed'] and preparation.get('previous_zones', preparation['zones']) != preparation['zones']
            invalidation = {'state': 'pending', 'generation': current['generation']+1, 'observation_id': None,
                            'reviewed': False, 'overrides': {}, 'history': [], 'history_cursor': -1} if changed_geometry else {}
            result = self.store.put(db, 'pages', {**current, **invalidation, 'preparation': preparation})
            if invalidation:
                self.update_submission(db, current['parent_id'])
            self.store.event(db, 'image.preparation_' + ('confirmed' if preparation['confirmed'] else 'previewed'), result,
                             preparation_stamp=stamp)
            return result

    def draft(self, task_id, changes=None):
        with self.store.connection(write=changes is not None) as db:
            draft = self.store.get(db, 'reference_drafts', task_id)
            key = 'save:'+task_id+':'+changes['request_id'] if changes and changes.get('request_id') else None
            if key:
                cached = self.store.once(db, key, changes)
                if cached is not None:
                    if cached.get('operation') == 'save':
                        from .reference_review import decorated
                        return decorated(draft)
                    return cached
            from .reference_review import decorated
            if changes is None:
                return decorated(draft)
            self.require_available(db, 'tasks', task_id)
            require(draft['state'] != 'parsing', 'REFERENCE_BUSY', '范本仍在解析，请先终止或等待完成。', 409)
            allowed = {k: v for k, v in changes.items() if k in
                       ('items', 'asset_ids', 'page_ids', 'page_count', 'page_labels', 'writing_rules', 'issues', 'edit_history', 'edit_cursor', 'machine_candidates', 'review_provenance')}
            for identifier in allowed.get('asset_ids', []):
                self.store.get(db, 'assets', identifier)
            if 'asset_ids' in allowed and allowed['asset_ids']:
                allowed['page_count'] = len(allowed['asset_ids'])
            require(1 <= int(allowed.get('page_count', draft['page_count'])) <= 50,
                    'PAGE_COUNT', '页数应在1至50之间。')
            if 'edit_history' in allowed:
                retained = {digest(frame) for frame in allowed['edit_history']}
                archived = draft.get('edit_history_archive', [])
                seen = {digest(frame) for frame in archived}
                removed = [frame for frame in draft.get('edit_history', []) if digest(frame) not in retained | seen]
                allowed['edit_history_archive'] = [*archived, *removed]
            result = decorated(self.store.put(db, 'reference_drafts', {**draft, **allowed, 'state': 'draft'}, expected=changes.get('revision')))
            if key: self.store.remember(db, key, changes, {'operation':'save','task_id':task_id,'committed_revision':result['revision']})
            return result

    def confirm_reference(self, task_id, revision):
        with self.store.connection(write=True) as db:
            draft = self.store.get(db, 'reference_drafts', task_id)
            self.require_available(db, 'tasks', task_id)
            if draft.get('state') == 'confirmed' and draft.get('confirmed_version_id'):
                existing = self.store.get(db, 'reference_versions', draft['confirmed_version_id'])
                if existing.get('source_draft_revision') == revision:
                    task = self.store.get(db, 'tasks', task_id)
                    from .scoring import prepare_reference_publication, bind_reference_publication
                    prepare_reference_publication(self, db, task, existing['items'])
                    score_id = bind_reference_publication(self, db, task, existing)
                    if score_id != task.get('scoring_version_id'):
                        self.store.put(db, 'tasks', {**task, 'scoring_version_id': score_id})
                    return existing
            require(draft['revision'] == revision and draft['state'] != 'parsing',
                    'REFERENCE_CHANGED', '范本已变化，请重新检查。', 409)
            from .reference_review import issues, decorated
            blockers = issues(draft)
            require(not blockers, 'REFERENCE_ISSUES', '请完成答案与完整答题区核对，并修正问题清单：' + '；'.join(dict.fromkeys(i['message'] for i in blockers)), issues=blockers)
            draft = {**decorated(draft), 'edit_history_archive':draft.get('edit_history_archive', [])}
            items = [Answer.model_validate({**a, 'id': a.get('id') or uid()}).model_dump() for a in draft['items']]
            require(items, 'REFERENCE_EMPTY', '至少输入一条参考答案。')
            require(all(1 <= a['slot'] <= draft['page_count'] for a in items), 'REFERENCE_SLOT', '题目的页码超出范本页数。')
            require(len({a['id'] for a in items}) == len(items), 'REFERENCE_DUPLICATE', '题目ID重复。')
            require(len({(a['slot'], a['section'], a['number'], a['part_id']) for a in items}) == len(items),
                    'REFERENCE_DUPLICATE', '同页同分区的题号重复，请修改。')
            require(all(not a['uncertain'] or a['excluded'] for a in items), 'REFERENCE_UNCERTAIN', '请修正所有不确定答案，或明确排除并填写原因。')
            require(all(not a['excluded'] or a['exclusion_reason'].strip() for a in items), 'EXCLUSION_REASON', '排除题目必须填写原因。')
            for answer in items:
                if answer['excluded']:
                    continue
                if draft.get('asset_ids'):
                    require(answer.get('answer_zone') and answer.get('geometry_checked'), 'REFERENCE_GEOMETRY', '请逐项确认完整答题区与原图对应，不能只框住范本笔迹。')
                require(all(1 <= slot <= draft['page_count'] for slot in answer['dependencies']), 'DEPENDENCY_SLOT', '跨页依赖页码无效。')
                if answer['kind'] == 'matching':
                    from .contracts import LocalMatchingRule
                    rule = LocalMatchingRule.model_validate(answer.get('matching_rule') or answer.get('answer_spec'))
                    require(rule.allowed_edges and len(rule.nodes) >= 2, 'MATCHING_INCOMPLETE', '连线题必须确认端点与正确配对。')
                    require(not draft.get('asset_ids') or all(n.box and (n.slot == answer['slot'] or n.slot in answer['dependencies']) for n in rule.nodes),
                            'MATCHING_GEOMETRY', '连线端点需要有效位置；其他页的端点页位必须加入跨页依赖。')
                else:
                    require(answer['english'].strip(), 'REFERENCE_BLANK', '请输入具体参考答案。')
            require(all(a['excluded'] or a['english'].strip() or a['kind'] not in ('dictation', 'copying') for a in items),
                    'REFERENCE_BLANK', '英文参考答案不能为空；无法确定的题目请明确排除。')
            self.require_available(db, 'tasks', task_id)
            task = self.store.get(db, 'tasks', task_id)
            groups = self.store.list(db, 'groups', task_id=task_id)
            old_id = task.get('reference_version_id')
            if old_id:
                old_ref = self.store.get(db, 'reference_versions', old_id)
                old_pages = old_ref.get('page_ids') or old_ref.get('asset_ids', [])
                new_pages = draft.get('page_ids') or draft.get('asset_ids', [])
                require(not old_pages or old_pages == new_pages or not self.store.list(db, 'submissions', task_id=task_id),
                        'PAGE_ORDER_REBIND', '已有作业时不能直接更改范本页数或页序；请另建任务并重新编排，旧资料保留。')
                require(old_ref['page_count'] == draft['page_count'] or not self.store.list(db, 'submissions', task_id=task_id),
                        'PAGE_COUNT_REBIND', '已有学生作业时不能直接改变页数，请新建任务并重新编排图片。')
            from .scoring import prepare_reference_publication, bind_reference_publication
            prepare_reference_publication(self, db, task, items)
            if old_id:
                def content(row):
                    return {**{k:row.get(k) for k in ('page_count','asset_ids','page_ids','page_labels','writing_rules')},
                        'items':[{k:v for k,v in Answer.model_validate(a).model_dump().items() if k!='geometry_checked'} for a in row['items']]}
                if digest(content({**draft,'items':items}))==digest(content(old_ref)):
                    score_id=bind_reference_publication(self,db,task,old_ref)
                    self.store.put(db,'tasks',{**task,'scoring_version_id':score_id})
                    self.store.put(db,'reference_drafts',{**draft,'items':items,'state':'confirmed','confirmed_version_id':old_ref['id']})
                    self.store.event(db,'reference.reused',old_ref,source_draft_revision=revision)
                    return old_ref
            version = self.store.put(db, 'reference_versions', {k: v for k, v in {
                **draft, 'id': uid(), 'state': 'confirmed', 'items': items,
                'version': self.store.sequence(db, 'reference:' + task_id), 'confirmed_at': now(),
                'source_draft_revision': revision}.items() if k not in ('created_at', 'updated_at', 'revision', 'confirmed_version_id')})
            score_id = bind_reference_publication(self, db, task, version)
            self.store.put(db, 'tasks', {**task, 'reference_version_id': version['id'], 'scoring_version_id': score_id})
            for group in groups:
                if group.get('reference_version_id') != version['id']:
                    self.store.put(db, 'groups', {**group, 'reference_version_id': version['id']})
            self.store.put(db, 'reference_drafts', {**draft, 'items': items, 'state': 'confirmed', 'confirmed_version_id': version['id']})
            self.store.event(db, 'reference.confirmed', version, version=version['version'], questions=len(items))
            return version

    def detail(self, task_id, known_revision=None):
        with self.store.connection() as db:
            task = self.store.get(db, 'tasks', task_id)
            live_groups = {g['id'] for g in self.store.list(db, 'groups', task_id=task_id, include_deleted=False)}
            pages = [p for p in self.store.list(db, 'pages', task_id=task_id, include_deleted=False) if p['group_id'] in live_groups]
            for page in pages:
                version = self.store.get(db, 'image_versions', page['image_version_id'])
                page['asset_id'] = version['asset_id']
                image_asset = self.store.get(db, 'assets', version['asset_id'])
                baseline_asset = self.store.get(db, 'assets', image_asset.get('base_asset_id', image_asset['id']))
                page['image_edited'] = image_asset.get('display_sha256') != baseline_asset.get('display_sha256')
                page['results'] = self.results(db, page)
                observation = self.store.get(db, 'observations', page['observation_id']) if page.get('observation_id') else None
                page['photo_issues'] = observation.get('photo_issues', []) if observation else []
                from .photo_issues import describe
                page['photo_details'] = describe(page['photo_issues'],page['results'],(observation or {}).get('structure',[]))
            draft = self.store.get(db, 'reference_drafts', task_id)
            from .reference_versions import catalogue
            versions = catalogue(self.store, db, task_id)
            scores = self.store.list(db, 'score_snapshots', task_id=task_id)
            captures = self.store.list(db, 'capture_sessions', task_id=task_id)
            active_frames = {c.get('active_frame_id') for c in captures if c['group_id'] in live_groups}
            capture_drafts = [f for f in self.store.list(db, 'capture_frames', task_id=task_id) if f['id'] in active_frames and f['state'] == 'draft']
            return {'capture_drafts': capture_drafts, 'hidden_answer_versions':catalogue(self.store, db, task_id, hidden=True),'scoring_versions':self.store.list(db,'scoring_versions',task_id=task_id),'score_snapshots': scores, 'answer_versions': versions, 'task': task, 'draft_unchanged':draft['revision']==known_revision, 'draft': None if draft['revision']==known_revision else __import__('autoreview.v2.reference_review', fromlist=['decorated']).decorated(draft),
                    'pages': pages, **{t: [{k:v for k,v in row.items() if k not in ('edit_history','edit_history_archive')} if t=='reference_versions' else row for row in self.store.list(db, t, task_id=task_id, include_deleted=False)]
                     for t in ('groups', 'reference_versions', 'submissions', 'runs', 'work_items', 'attempts', 'cohorts', 'report_snapshots')}}

    def results(self, db, page):
        ref_id = page.get('reference_version_id')
        if not ref_id:
            return []
        observation = self.store.get(db, 'observations', page['observation_id']) if page.get('observation_id') else None
        results=resolve_page(self.store.get(db, 'reference_versions', ref_id), page, observation)
        from .annotations import apply
        sub=self.store.get(db,'submissions',page['parent_id'])
        policy=self.store.get(db,'scoring_versions',sub.get('scoring_version_id') or '',optional=True)
        from .review_readiness import pending_reasons
        for answer in results:
            answer['review_pending_reasons']=pending_reasons(answer)
            answer['review_pending']=bool(answer['review_pending_reasons'])
        return apply(results,policy)

    def intake(self, group_id, data):
        """Manifest is explicit ordered asset IDs. Preview and commit use the same algorithm."""
        with self.store.connection(write=not data.get('preview', False)) as db:
            request = {**data, 'group_id': group_id}
            if not data.get('preview'):
                cached = self.store.once(db, data.get('request_id', ''), request)
                if cached:
                    return cached
            group = self.store.get(db, 'groups', group_id)
            from .scoring import gate
            gate(self.store, db, group['task_id'])
            reference_id = group.get('reference_version_id')
            require(reference_id, 'REFERENCE_NOT_CONFIRMED', '范本尚未确认，图片已保存在本地待编排；请先确认参考答案再正式编排。')
            self.require_available(db, 'groups', group_id)
            ref = self.store.get(db, 'reference_versions', reference_id)
            n = ref['page_count']
            assets = data.get('asset_ids', [])
            require(assets, 'IMAGES_REQUIRED', '请先选择图片。')
            mode = data.get('mode', self.settings.data['last_intake_mode'])
            require(mode in ('whole_submission', 'single_page'), 'INTAKE_MODE', '请选择整份或单面。')
            slot = int(data.get('slot', 1))
            require(1 <= slot <= n, 'PAGE_SLOT', '页码超出范本页数。')
            start = int(data.get('start_tag') or group.get('next_tag', 1))
            require(start >= 1, 'PAPER_TAG', '纸签编号从001开始。')
            cohort = self.store.get(db, 'cohorts', data['cohort_id']) if data.get('cohort_id') else None
            if cohort:
                require(cohort['group_id'] == group_id and mode == 'single_page', 'COHORT_SCOPE', '名单必须属于当前班级且使用单面模式。')
                ids = list(cohort['submission_ids'])
                if data.get('reverse_cohort'):
                    ids.reverse()
                require(len(assets) <= len(ids), 'COHORT_COUNT', '图片数超过已有名单，请另建名单。')
                # Explicit positions permit missing/rescan pages without shifting subsequent students.
                positions = data.get('cohort_positions', list(range(len(assets))))
                require(len(positions) == len(assets) and len(set(positions)) == len(positions)
                        and all(isinstance(p, int) and 0 <= p < len(ids) for p in positions),
                        'COHORT_POSITIONS', '补页位置必须唯一且位于名单内。')
                selected = [self.store.get(db, 'submissions', ids[p]) for p in positions]
            else:
                selected = []
            manifest = []
            for index, asset_id in enumerate(assets):
                asset = self.store.get(db, 'assets', asset_id)
                ordinal = index // n if mode == 'whole_submission' else index
                tag = selected[index]['paper_tag'] if cohort else str(start + ordinal).zfill(3)
                page_slot = index % n + 1 if mode == 'whole_submission' else slot
                manifest.append({'asset_id': asset_id, 'filename': asset['filename'], 'paper_tag': tag,
                                 'slot': page_slot, 'submission_id': selected[index]['id'] if cohort else None})
            submissions = self.store.list(db, 'submissions', group_id=group_id)
            existing = {s['paper_tag']: s for s in submissions if not s.get('capture_withdrawn')}
            for entry in manifest:
                sub = existing.get(entry['paper_tag'])
                require(not sub or cohort, 'TAG_EXISTS', '编号已存在，请使用补页名单或替换图片。', 409)
                if sub:
                    occupied = any(p['slot'] == entry['slot'] for p in self.store.list(db, 'pages', parent_id=sub['id']))
                    require(not occupied, 'SLOT_OCCUPIED', '该编号的页面已存在，请使用替换功能。', 409)
            response = {'manifest': manifest, 'page_count': n,
                        'incomplete': mode == 'whole_submission' and len(assets) % n != 0}
            response['fingerprint'] = digest({'manifest': manifest, 'reference_id': reference_id, 'group_revision': group['revision']})
            if data.get('preview'):
                return response
            if data.get('fingerprint'):
                require(data['fingerprint'] == response['fingerprint'], 'INTAKE_CHANGED', '编排已变化，请重新检查后确认。', 409)
            created_ids = []
            for entry in manifest:
                sub = existing.get(entry['paper_tag'])
                if not sub:
                    sub = self.store.put(db, 'submissions', {'parent_id': group_id, 'group_id': group_id,
                        'task_id': group['task_id'], 'paper_tag': entry['paper_tag'], 'page_count': n,
                        'seq': self.store.sequence(db, 'submission:' + group_id), 'state': 'incomplete'})
                    existing[entry['paper_tag']] = sub
                    created_ids.append(sub['id'])
                page = self.store.put(db, 'pages', {'parent_id': sub['id'], 'task_id': group['task_id'],
                    'group_id': group_id, 'slot': entry['slot'], 'seq': self.store.sequence(db, 'page:' + group_id),
                    'state': 'pending', 'generation': 1, 'overrides': {}, 'reviewed': False,
                    'reference_version_id': reference_id, 'history': [], 'history_cursor': -1})
                version = self.store.put(db, 'image_versions', {'parent_id': page['id'], 'task_id': group['task_id'],
                    'group_id': group_id, 'asset_id': entry['asset_id'], 'version': 1})
                self.store.put(db, 'pages', {**page, 'image_version_id': version['id']})
                self.update_submission(db, sub['id'])
                entry.update(submission_id=sub['id'], page_id=page['id'])
            cohort_row = cohort
            if created_ids:
                cohort_row = self.store.put(db, 'cohorts', {'parent_id': group_id, 'task_id': group['task_id'],
                    'group_id': group_id, 'submission_ids': created_ids, 'name': data.get('cohort_name') or
                    f"{manifest[0]['paper_tag']}–{manifest[-1]['paper_tag']}", 'mode': mode})
                self.store.put(db, 'groups', {**group, 'next_tag': max(group.get('next_tag', 1), start + len(created_ids))})
            session = self.store.put(db, 'intake_sessions', {'parent_id': group_id, 'task_id': group['task_id'],
                'group_id': group_id, 'manifest': manifest, 'mode': mode, 'cohort_id': cohort_row['id'] if cohort_row else None})
            self.store.event(db, 'intake.committed', session, image_count=len(manifest), mode=mode)
            response.update(session_id=session['id'], cohort=cohort_row)
            self.store.remember(db, data['request_id'], request, response)
        self.settings.save(last_intake_mode=mode)
        return response

    def update_submission(self, db, identifier):
        sub = self.store.get(db, 'submissions', identifier)
        pages = self.store.list(db, 'pages', parent_id=identifier, include_deleted=False)
        complete = len(pages) == sub['page_count']
        state = 'reviewed' if complete and all(p.get('reviewed') for p in pages) else (
            'processed' if complete and all(p['state'] == 'processed' for p in pages) else 'pending' if complete else 'incomplete')
        from .scoring import recompute
        current = {**sub, 'state': state, 'loaded_pages': len(pages), 'processed_pages': sum(p['state'] == 'processed' for p in pages)}
        score = recompute(self, db, current, pages)
        return self.store.put(db, 'submissions', {**current, **score})

    def replace_page(self, page_id, data, db=None):
        from .page_replacement import replace
        return replace(self, page_id, data, db)
    def review(self, page_id, data, db=None, *, defer_update=False):
        with self.store.connection(write=True) if db is None else nullcontext(db) as db:
            page = self.store.get(db, 'pages', page_id)
            self.require_available(db, 'groups', page['group_id'])
            require(page['revision'] == data.get('revision'), 'REVISION_CONFLICT', '该页面已经变化，请刷新。', 409)
            require(page['state'] == 'processed', 'PAGE_NOT_PROCESSED', '页面尚未处理完成。')
            action = data.get('action', 'edit')
            history, cursor = page.get('history', []), page.get('history_cursor', -1)
            if action in ('undo', 'redo'):
                new_cursor = cursor + (-1 if action == 'undo' else 1)
                require(-1 <= new_cursor < len(history), 'NO_HISTORY', '没有可撤销或重做的操作。')
                prior = self.store.get(db, 'review_revisions', history[new_cursor]) if new_cursor >= 0 else {'overrides': {}, 'reviewed': False}
                page.update(overrides=prior['overrides'], reviewed=prior['reviewed'], history_cursor=new_cursor,
                            photo_status=prior.get('photo_status', page.get('machine_photo_status', 'clear')))
            else:
                overrides = dict(page.get('overrides', {}))
                question_id = data.get('question_id')
                if question_id:
                    require(question_id in {a['id'] for a in self.results(db, page)}, 'QUESTION_NOT_FOUND', '题号不存在。')
                    change = {k: v for k, v in data.get('change', {}).items() if k in
                              ('raw_english', 'status', 'writing_status', 'box', 'note', 'observation', 'score_unit_status')}
                    if 'observation' in change:
                        from .contracts import Cell
                        original = next(a for a in self.results(db, page) if a['id'] == question_id)
                        change['observation'] = Cell.model_validate({**(original.get('observation') or {}),
                            'question_id': question_id, 'number': original['number'], 'slot': page['slot'],
                            'raw_english': original.get('raw_english') or '', 'uncertain': False,
                            **change['observation']}).model_dump()
                    require(change.get('status', 'correct') in ('correct', 'incorrect', 'missing', 'unknown'), 'GRADE_STATUS', '无效判定。')
                    require(change.get('writing_status', 'clear') in ('clear', 'confirmed_error', 'pending', 'not_assessed'), 'WRITING_STATUS', '无效书写判定。')
                    if change.get('box'):
                        Box.model_validate(change['box'])
                    if 'score_unit_status' in change:
                        require(isinstance(change['score_unit_status'],dict) and all(v in ('correct','incorrect','unknown','missing') for v in change['score_unit_status'].values()), 'SCORE_STATUS', '计分单元判定无效。')
                        sub=self.store.get(db,'submissions',page['parent_id'])
                        policy=self.store.get(db,'scoring_versions',sub.get('scoring_version_id') or '',optional=True)
                        units=[u for u in (policy or {}).get('units',[]) if u['question_id']==question_id]
                        require(set(change['score_unit_status']) <= {u['id'] for u in units},'SCORE_UNIT','计分单元不属于当前作业此题的冻结规则。')
                        current_score=self.store.get(db,'score_snapshots',sub.get('score_snapshot_id') or '',optional=True)
                        defaults={u['id']:u['status'] for u in (current_score or {}).get('units',[])}
                        if any(u['rule']=='matching_source' for u in units):
                            from .scoring import evaluate
                            actual=evaluate(policy,self.results(db,page))
                            defaults.update({u['id']:u['status'] for u in actual['units'] if u['id'] in {v['id'] for v in units}})
                        statuses={**overrides.get(question_id,{}).get('score_unit_status',{}),**change['score_unit_status']}
                        change['score_unit_status']=statuses
                        values=[statuses.get(u['id'],defaults.get(u['id'],'unknown')) for u in units]
                        change['status']='unknown' if 'unknown' in values else 'incorrect' if any(v in ('incorrect','missing') for v in values) else 'correct'
                    # Any definite teacher content verdict finishes the redundant
                    # handwriting check in the same undoable transaction.
                    decided = (data.get('change',{}).get('status') in ('correct','incorrect','missing') or
                               any(v in ('correct','incorrect','missing') for v in data.get('change',{}).get('score_unit_status',{}).values()))
                    if decided and 'writing_status' not in change:
                        change['writing_status']='clear'
                    prior_override = dict(overrides.get(question_id, {}))
                    if any(k in change for k in ('status','raw_english','observation')) and 'score_unit_status' not in change:
                        prior_override.pop('score_unit_status',None)
                    if ('raw_english' in change or 'observation' in change) and 'status' not in change:
                        prior_override.pop('status', None)
                    overrides[question_id] = {**prior_override, **change}
                if data.get('photo_status'):
                    require(data['photo_status'] in ('accepted', 'retake_required', 'pending'), 'PHOTO_STATUS', '照片问题状态无效。')
                    page['photo_status'] = data['photo_status']
                reviewed = action == 'confirm'
                candidate = {**page, 'overrides': overrides}
                if reviewed:
                    require(page.get('photo_status') not in ('pending', 'retake_required'), 'PHOTO_REVIEW_REQUIRED',
                            '请先确认照片可判读，或替换重拍图片。照片问题不计为学生错误。')
                    from .review_readiness import needs_review,pending_summary
                    unresolved = [a for a in self.results(db, candidate) if needs_review(a)]
                    require(not unresolved or data.get('accept_unknown'), 'UNRESOLVED_REVIEW',
                            f'本页仍有{len(unresolved)}题待核实：{pending_summary(unresolved)}。可修改，或明确保留为未知。',
                            question_ids=[a['id'] for a in unresolved])
                revision = self.store.put(db, 'review_revisions', {'parent_id': page_id, 'task_id': page['task_id'],
                    'group_id': page['group_id'], 'overrides': overrides, 'reviewed': reviewed, 'action': action,
                    'photo_status': page.get('photo_status', 'clear'),
                    'image_version_id': page['image_version_id'], 'reference_version_id': page['reference_version_id'],
                    'observation_id': page.get('observation_id')})
                page.update(overrides=overrides, reviewed=reviewed, history=history[:cursor + 1] + [revision['id']], history_cursor=cursor + 1)
                if question_id:
                    category = data.get('category', 'other')
                    require(category in ('transcription', 'layout', 'reference', 'rule', 'writing', 'photo', 'other'), 'FEEDBACK_CATEGORY', '反馈类别无效。')
                    self.store.put(db, 'review_feedback', {'parent_id': page_id, 'task_id': page['task_id'], 'group_id': page['group_id'],
                        'question_id': question_id, 'category': category, 'review_revision_id': revision['id'],
                        'observation_id': page.get('observation_id'), 'image_version_id': page['image_version_id'],
                        'reference_version_id': page['reference_version_id'], 'curated_for_evaluation': False})
            result = self.store.put(db, 'pages', page)
            if not defer_update:
                self.update_submission(db, page['parent_id'])
            sub = self.store.get(db, 'submissions', page['parent_id'])
            if sub.get('paper_feedback') == 'done' and action != 'confirm':
                self.store.put(db, 'submissions', {**sub, 'paper_feedback': 'needs_update'})
            self.store.event(db, 'review.' + action, result, question_id=question_id if action not in ('undo', 'redo') else None)
            return result
