"""Reusable reference evidence, explicitly separate from student observations."""
import hashlib
import io
import json
import zipfile
from pathlib import Path, PurePosixPath
from .core import atomic_write, dumps, uid, now, require
from .resources import Resources
from .trash import strings


def export_pack(service, task_id, destination_path=None):
    store = service.store
    with store.connection() as db:
        task = store.get(db,'tasks',task_id)
        draft = store.get(db,'reference_drafts',task_id)
        versions = store.list(db,'reference_versions',task_id=task_id)
        scoring = {'draft':store.get(db,'scoring_drafts',task_id,optional=True),'versions':store.list(db,'scoring_versions',task_id=task_id)}
        evidence = [{k:r.get(k) for k in ('id','created_at','attempt_id','purpose','result','prompt_hash')}
                    for r in store.list(db, 'checkpoints', task_id=task_id) if r.get('purpose') == 'reference']
        hashes = {r.get('prompt_hash') for r in evidence}
        prompts = [r for r in store.list(db, 'prompt_versions') if r.get('prompt_hash') in hashes]
        all_assets = {a['id']:a for a in store.list(db,'assets')}
        selected = set(strings([draft,versions])) & set(all_assets)
        while True:
            more = {v for key in selected for v in strings(all_assets[key]) if v in all_assets}
            if more <= selected: break
            selected |= more
        assets=[all_assets[key] for key in sorted(selected)]
    data={'format':'autoreview-reference','schema':3,'created_at':now(),'name':task['name'],
          'source_library_id':store.manifest['library_id'],'draft':draft,'versions':versions,'assets':assets,
          'overlay':{'coordinate_space':'normalized-display-image','items':draft['items']},
          'scoring':scoring, 'machine_evidence':evidence, 'prompt_records':prompts,
          'student_data_included':False,'confirmation_transferable':False}
    relative={v for a in assets for v in strings(a) if v.startswith('assets/')}
    buffer=io.BytesIO();checksums={}
    with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as z:
        files={'reference.json':dumps(data).encode('utf-8')}
        for path in relative: files['files/'+path]=Resources(store).resolve(path).read_bytes()
        from PIL import Image, ImageDraw, ImageFont
        font_path=Path('C:/Windows/Fonts/msyh.ttc')
        font=ImageFont.truetype(str(font_path),24) if font_path.exists() else ImageFont.load_default()
        for slot,aid in enumerate(draft.get('asset_ids',[]),1):
            picture=Image.open(service.asset(aid)).convert('RGB');draw=ImageDraw.Draw(picture)
            for item in draft['items']:
                b=item.get('answer_zone')
                if item['slot']!=slot or not b: continue
                bounds=(b['x1']*picture.width,b['y1']*picture.height,b['x2']*picture.width,b['y2']*picture.height)
                draw.rectangle(bounds,outline='#138777',width=3)
                draw.text((bounds[0],max(0,bounds[1]-28)),item['section']+'/'+item['number'],fill='#15594f',font=font,stroke_width=1,stroke_fill='white')
            preview=io.BytesIO();picture.save(preview,'PNG');files[f'previews/page-{slot}.png']=preview.getvalue()
        for name,content in files.items():
            z.writestr(name,content);checksums[name]=hashlib.sha256(content).hexdigest()
        z.writestr('manifest.json',dumps({'format':'autoreview-reference','version':3,'checksums':checksums}))
    destination=Path(destination_path).expanduser().resolve() if destination_path else Path(service.settings.data['export_directory'])/f'AutoReview-reference-{uid()[:8]}.zip'
    if not destination_path: destination.parent.mkdir(parents=True, exist_ok=True)
    require(destination.suffix.lower()=='.zip' and destination.parent.is_dir(), 'EXPORT_PATH', '请选择有效保存目录中的 ZIP 文件。')
    atomic_write(destination,buffer.getvalue())
    return {'path':str(destination),'page_count':draft['page_count'],'item_count':len(draft['items'])}


def import_pack(service, task_id, content, preview=False):
    require(len(content)<=512*1024**2,'PACK_TOO_LARGE','范本包不能超过512MB。')
    try:
        z=zipfile.ZipFile(io.BytesIO(content))
        infos=z.infolist();names=[i.filename for i in infos]
        require(len(names)==len(set(names)) and len(names)<=10000,'PACK_FORMAT','范本包含重复条目或条目过多。')
        require(sum(i.file_size for i in infos)<=2*1024**3,'PACK_TOO_LARGE','范本包解压后不能超过2GB。')
        for n in names:
            require('\\' not in n and ':' not in n and not PurePosixPath(n).is_absolute() and '..' not in PurePosixPath(n).parts,
                    'PACK_PATH','范本包包含不安全路径。')
        manifest=json.loads(z.read('manifest.json'))
        require(manifest.get('format')=='autoreview-reference' and manifest.get('version') in (1,2,3),'PACK_FORMAT','请选择程序导出的范本答案包。')
        checks=manifest['checksums'];require(set(names)==set(checks)|{'manifest.json'},'PACK_FORMAT','范本包清单与文件不一致。')
        files={n:z.read(n) for n in checks}
        require(all(hashlib.sha256(files[n]).hexdigest()==sha for n,sha in checks.items()),'PACK_CHECKSUM','范本包校验失败。')
        data=json.loads(files['reference.json'])
        require(data.get('schema') in (1,2,3) and data.get('format')=='autoreview-reference','PACK_FORMAT','不支持此范本包版本。')
        from .contracts import Answer
        for item in data['draft']['items']: Answer.model_validate(item)
        assets = data['assets']; asset_ids = {a['id'] for a in assets}
        require(len(asset_ids) == len(assets), 'PACK_ASSET', '范本包图片标识重复。')
        require(set(data['draft'].get('asset_ids', [])) <= asset_ids, 'PACK_ASSET', '范本包缺少关联图片。')
        require(not data['draft'].get('asset_ids') or len(data['draft']['asset_ids']) == data['draft']['page_count'],
                'PACK_ASSET', '范本图片数量与页数不一致。')
        for asset in assets:
            for field in ('relative_path', 'display_path', 'thumbnail_path'):
                path = asset[field]
                require(path.startswith('assets/') and 'files/'+path in files, 'PACK_ASSET', '范本包缺少图片文件。')
    except (zipfile.BadZipFile,KeyError,ValueError,TypeError) as error:
        from .core import Problem
        raise Problem('PACK_FORMAT','范本包结构无效，未改变当前草稿。') from error
    if preview:
        return {'name': data.get('name'), 'page_count': data['draft']['page_count'],
                'item_count': len(data['draft']['items']), 'version_count': len(data.get('versions', [])),
                'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content)}
    store=service.store
    with store.connection() as db:
        service.require_available(db,'tasks',task_id)
        old=store.get(db,'reference_drafts',task_id)
        require(old['state']!='parsing','REFERENCE_BUSY','请先停止范本解析。')
    ids={}
    def collect(value, context=None):
        if isinstance(value,dict):
            if context != 'options' and isinstance(value.get('id'),str): ids[value['id']]=uid()
            if value.get('option_group_id'): ids.setdefault(value['option_group_id'],uid())
            for key,x in value.items(): collect(x,key)
        elif isinstance(value,list):
            for x in value: collect(x,context)
    collect(data)
    for pid in data['draft'].get('page_ids',[]): ids[pid]=uid()
    for a in data['draft']['items']:
        for o in a.get('options',[]):
            if o.get('region_id'): ids[o['region_id']]=uid()
    paths={}
    batch=uid()
    for a in data['assets']:
        for key in ('relative_path','display_path','thumbnail_path'):
            path=a[key];name='files/'+path
            require(name in files,'PACK_ASSET','范本包缺少图片。')
            paths[path]=f'assets/r-{batch[:8]}/{hashlib.sha256(path.encode()).hexdigest()[:24]}{Path(path).suffix}'
            Resources(store).safe(paths[path])
    def remap(value, field=None):
        if field in ('english','chinese','variants','label','name','filename','number','section','part_id','description','reason','issues','exclusion_reason'):
            return value
        if isinstance(value,dict): return {ids.get(k,k):remap(v,k) for k,v in value.items()}
        if isinstance(value,list): return [remap(v) for v in value]
        if isinstance(value,str):
            if value.startswith('{'):
                try: return json.dumps(remap(json.loads(value)), ensure_ascii=False, separators=(',',':'))
                except (ValueError,TypeError): pass
            return ids.get(value,paths.get(value,value))
        return value
    for source,target in paths.items(): atomic_write(Resources(store).safe(target),files['files/'+source])
    with store.connection(write=True) as db:
        service.require_available(db,'tasks',task_id)
        current=store.get(db,'reference_drafts',task_id)
        require(current['revision']==old['revision'],'REVISION_CONFLICT','导入期间草稿已变化，请重试。',409)
        for a in data['assets']: store.put(db,'assets',remap(a))
        draft=remap(data['draft'])
        draft={**draft,'id':task_id,'parent_id':task_id,'task_id':task_id,'state':'draft','review_receipts':{},
               'confirmed_version_id':None,'import_provenance':{'source_library_id':data.get('source_library_id'),'imported_at':now(),'source_name':data.get('name')},
               'imported_machine_evidence':remap(data.get('machine_evidence', [])),
               'imported_history':remap(data.get('versions',[])),
               'replaced_drafts':[*(old.get('replaced_drafts',[])),
                   {k:v for k,v in old.items() if k!='replaced_drafts'}]}
        draft.pop('validation_issues',None)
        result=store.put(db,'reference_drafts',draft,expected=old['revision'])
        imported_score=remap(data.get('scoring',{}).get('draft'))
        if imported_score:
            imported_score.update(id=task_id,parent_id=task_id,task_id=task_id,reference_version_id=None,total_confirmed_hash=None)
            imported_score['units']=[{k:v for k,v in u.items() if k!='confirmed_hash'} for u in imported_score.get('units',[])]
            from .scoring import structure
            imported_score['structure_hash']=structure(draft['items'])
            imported_score['imported_history']=remap(data.get('scoring',{}).get('versions',[]))
            store.put(db,'scoring_drafts',imported_score)
        store.event(db,'reference.imported',result,pages=result['page_count'],items=len(result['items']))
    from .reference_review import decorated
    return decorated(result)
