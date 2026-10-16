"""Per-copy, consented and reversible official CLI component updates.

No credential transport, auto inference, global install or periodic check.
Only the explicitly supported protocol/credential family is eligible.
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import os
import re
import shutil
import tarfile
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit
from .core import Problem, require, atomic_write, dumps, now, uid

RELEASES = 'https://api.github.com/repos/openai/codex/releases/latest'
FILES = ('codex', 'codex-code-mode-host', 'codex-command-runner', 'codex-windows-sandbox-setup')
FAMILY = (0, 163)
MAX_DOWNLOAD = 220 * 1024 * 1024
_verified = {}
_activation_owner = uid()


def version(value):
    match = re.fullmatch(r'(?:codex-cli |rust-v)?(\d+)\.(\d+)\.(\d+)(?:-([a-zA-Z0-9.-]+))?', value or '')
    require(match, 'CLI_VERSION', '组件版本格式不正确。')
    return (*map(int, match.group(1, 2, 3)), 0 if match[4] else 1)


def load(path, default=None):
    try: return json.loads(path.read_text('utf-8'))
    except (OSError, ValueError): return default


def root_for(home): return Path(home) / 'cli-component'


def checked_executable(root, entry):
    require(isinstance(entry, dict) and re.fullmatch(r'0\.163\.\d+-[a-f0-9]{8}', entry.get('directory', '')),
            'CLI_COMPONENT_INVALID', '独立组件路径不安全。')
    folder = root/'versions'/entry['directory']
    require(not folder.is_symlink() and folder.resolve().parent == (root/'versions').resolve(), 'CLI_COMPONENT_INVALID', '独立组件路径不安全。')
    manifest = load(folder/'manifest.json')
    require(manifest and manifest.get('version') == entry.get('version') and len(manifest.get('files', [])) == 4,
            'CLI_COMPONENT_INVALID', '独立组件清单缺失。')
    signature = tuple((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in [folder/'manifest.json', *[folder/(n+'.exe') for n in FILES]])
    if _verified.get(str(folder)) != signature:
        require({f.get('name') for f in manifest['files']} == {n+'.exe' for n in FILES}, 'CLI_COMPONENT_INVALID', '独立组件清单不完整。')
        for item in manifest['files']:
            path=folder/item['name']
            require(not path.is_symlink(), 'CLI_COMPONENT_INVALID', '独立组件文件不安全。')
            with path.open('rb') as stream: actual=hashlib.file_digest(stream, 'sha256').hexdigest()
            require(actual == item['sha256'], 'CLI_COMPONENT_INVALID', '独立组件校验失败，已恢复原组件。')
        _verified[str(folder)] = signature
    return folder/'codex.exe'


def rollback(home, bad_path=None, reason='新组件启动失败，已恢复原组件。'):
    root=root_for(home); active=load(root/'active.json', {})
    if not active.get('current'): return False
    if bad_path and str(root/'versions'/active['current']['directory']/'codex.exe') != str(bad_path): return False
    previous=active.get('previous')
    if previous:
        try: checked_executable(root, previous)
        except Exception: previous=None
    atomic_write(root/'active.json', dumps({'current': previous, 'previous': None}).encode())
    atomic_write(root/'state.json', dumps({'phase': 'rolled_back', 'message': reason, 'updated_at': now()}).encode())
    return True


def active_executable(home):
    root=root_for(home); active=load(root/'active.json', {})
    if not active.get('current'): return None
    if active.get('activation_owner') and active['activation_owner'] != _activation_owner:
        rollback(home,reason='上次组件切换未完成，已恢复原组件。')
        active=load(root/'active.json',{})
        if not active.get('current'):return None
    try: return str(checked_executable(root, active['current']))
    except Exception:
        rollback(home, reason='独立组件文件缺失或校验失败，已恢复原组件。')
        active=load(root/'active.json', {})
        return str(checked_executable(root, active['current'])) if active.get('current') else None


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        require(urlsplit(newurl).scheme == 'https' and urlsplit(newurl).hostname in
                {'api.github.com','github.com','release-assets.githubusercontent.com','objects.githubusercontent.com'},
                'CLI_DOWNLOAD_SOURCE', '组件下载跳转来源未通过检查。')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_official(url,headers=None):
    return urllib.request.build_opener(SafeRedirect()).open(
        urllib.request.Request(url, headers={'User-Agent': 'AutoReview-component-updater', 'Accept':'application/vnd.github+json',**(headers or {})}), timeout=25)


def release_candidate(releases, current):
    eligible=[]; unsupported=False
    for release in releases:
        if release.get('draft') or release.get('prerelease'): continue
        tag=release.get('tag_name','')
        if not re.fullmatch(r'rust-v\d+\.\d+\.\d+',tag): continue
        parsed=version(tag)
        if parsed[:2] != FAMILY:
            unsupported |= parsed > version(current)
            continue
        if parsed <= version(current): continue
        assets=[]
        for name in FILES:
            asset_name=name+'-x86_64-pc-windows-msvc.exe.tar.gz'
            asset=next((a for a in release.get('assets',[]) if a.get('name') == asset_name),None)
            if not asset or not re.fullmatch(r'sha256:[0-9a-f]{64}',asset.get('digest') or ''): break
            expected=f'https://github.com/openai/codex/releases/download/{tag}/{asset_name}'
            if asset.get('browser_download_url') != expected or not 0 < asset.get('size',0) <= MAX_DOWNLOAD: break
            assets.append({'name':name+'.exe','archive_name':asset_name,'url':expected,'sha256':asset['digest'][7:],'bytes':asset['size']})
        if len(assets)==4:
            eligible.append({'version':tag[6:],'tag':tag,'files':assets,'bytes':sum(a['bytes'] for a in assets),
                             'source':f'https://github.com/openai/codex/releases/tag/{tag}'})
    return (max(eligible,key=lambda r:version(r['version'])) if eligible else None), unsupported


class CliUpdater:
    def __init__(self, settings, provider, idle, switch_gate):
        self.settings,self.provider,self.idle,self.switch_gate=settings,provider,idle,switch_gate
        self.root=root_for(settings.home);self.state=load(self.root/'state.json', {'phase':'idle'})
        self.job=None; self.check_job=None;self.cancelled=threading.Event();self.lock=threading.RLock();self.startup_checked=False;self.closed=False
        if self.state.get('phase') in ('downloading','testing','switching','checking'):
            self.state={'phase':'interrupted','message':'上次组件更新未完成，原组件仍可用，请重新检查。'}

    def save(self, **fields):
        with self.lock:
            self.state.update(fields,updated_at=now())
            atomic_write(self.root/'state.json',dumps(self.state).encode())
        return self.status()

    def status(self):
        persisted=load(self.root/'state.json',{})
        if persisted.get('phase')=='rolled_back' and persisted.get('updated_at','')>self.state.get('updated_at',''):
            self.state=persisted
        with self.lock: state=dict(self.state)
        state['enabled']=bool(self.settings.data.get('cli_check_on_startup',False))
        state['compatible_series']='0.163.x'
        # Internal trusted download paths are never accepted back from the UI.
        state.pop('candidate',None);state.pop('prepared',None)
        return state

    async def current_version(self):
        from .provider import executable
        exe=executable(self.settings.home)
        proc=await asyncio.create_subprocess_exec(exe,'--version',stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,
                                                 creationflags=0x08000000 if os.name=='nt' else 0)
        try: out,_=await asyncio.wait_for(proc.communicate(),10)
        except BaseException:
            if proc.returncode is None:proc.kill();await proc.wait()
            raise
        result=out.decode().strip();version(result);return result

    async def check(self, trigger='manual'):
        require(trigger in ('startup','manual'), 'CLI_CHECK_TRIGGER','检查来源无效。')
        if trigger=='startup':
            if self.startup_checked or not self.settings.data.get('cli_check_on_startup',False): return self.status()
            self.startup_checked=True
        if self.job and not self.job.done(): return self.status()
        if self.state.get('phase')=='waiting_idle': return self.status()
        if self.check_job and not self.check_job.done(): return await asyncio.shield(self.check_job)
        self.check_job=asyncio.create_task(self._check())
        return await asyncio.shield(self.check_job)

    async def _check(self):
        self.save(phase='checking',message='正在检查官方兼容更新…',candidate=None,offer_id=None,error_code=None)
        try:
            current=await self.current_version()
            def fetch():
                with open_official(RELEASES) as response:
                    raw=response.read(2*1024*1024+1)
                require(len(raw)<=2*1024*1024,'CLI_RELEASE_METADATA','官方版本信息过大。')
                data=json.loads(raw);require(isinstance(data,dict),'CLI_RELEASE_METADATA','官方版本信息格式不正确。')
                return [data]
            # A remote update check must never exceed the local API timeout and
            # make a healthy service look disconnected. Do not fetch full history.
            candidate,unsupported=release_candidate(await asyncio.wait_for(asyncio.to_thread(fetch),20),current)
            if candidate:
                return self.save(phase='available',message='发现兼容更新，确认后才下载。',current_version=current,
                    candidate=candidate,offer_id=uid(),available_version=candidate['version'],download_bytes=candidate['bytes'],source=candidate['source'])
            return self.save(phase='up_to_date',current_version=current,available_version=None,
                message='有较新组件超出当前兼容系列，需要更新AutoReview主程序。' if unsupported else '当前组件已满足本程序支持的最新兼容版本。')
        except Exception as error:
            return self.save(phase='check_failed',message='官方组件更新检查暂不可用。现有组件仍可使用，可稍后重试。',error_code=getattr(error,'code',type(error).__name__))

    async def confirm(self, offer_id, confirmed):
        require(confirmed is True,'CLI_UPDATE_CONFIRM','请先在弹窗确认下载更新。')
        require(self.state.get('offer_id')==offer_id,'CLI_UPDATE_STALE','版本信息已变化，请重新检查。',409)
        if self.job and not self.job.done(): return self.status()
        require(self.state.get('phase')=='available' and self.state.get('candidate'),'CLI_UPDATE_STALE','请重新检查可用更新。',409)
        self.cancelled.clear();self.job=asyncio.create_task(self._prepare(dict(self.state['candidate'])))
        return self.status()

    def check_cancelled(self):
        if self.closed or self.cancelled.is_set(): raise Problem('CLI_UPDATE_CANCELLED','组件更新已取消，原组件保留。')

    def download(self,candidate,folder):
        completed=0;total=candidate['bytes'];files=[];start=time.monotonic()
        cache=self.root/'downloads';cache.mkdir(parents=True,exist_ok=True)
        for asset in candidate['files']:
            self.check_cancelled();archive=cache/(asset['sha256']+'.part');hashed=hashlib.sha256()
            require(not archive.is_symlink() and archive.resolve().parent==cache.resolve(),'CLI_ARCHIVE','组件缓存路径不安全。')
            size=archive.stat().st_size if archive.exists() else 0
            if size>asset['bytes']:archive.unlink();size=0
            if size:
                with archive.open('rb') as prefix:
                    while chunk:=prefix.read(1024*1024):hashed.update(chunk)
            self.save(downloaded_bytes=completed+size,total_bytes=total)
            if size<asset['bytes']:
                with open_official(asset['url'],{'Range':f'bytes={size}-'} if size else {}) as response:
                    if size and getattr(response,'status',200)==206:
                        require(response.headers.get('Content-Range','').startswith(f'bytes {size}-'),'CLI_DOWNLOAD_RANGE','组件断点范围不符，原组件保留。')
                    elif size:
                        size=0;hashed=hashlib.sha256()
                    with archive.open('ab' if size else 'wb') as target:
                        while True:
                            self.check_cancelled()
                            require(time.monotonic()-start<1800,'CLI_DOWNLOAD_TIMEOUT','组件下载超时，原组件保留。重新确认后可继续未完成下载。')
                            chunk=response.read(1024*1024)
                            if not chunk:break
                            size+=len(chunk);require(size<=asset['bytes'],'CLI_DOWNLOAD_SIZE','组件文件大小不符。')
                            target.write(chunk);hashed.update(chunk)
                            self.save(downloaded_bytes=completed+size,total_bytes=total)
            valid=size==asset['bytes'] and hashed.hexdigest()==asset['sha256']
            if not valid:archive.unlink(missing_ok=True)
            require(valid,'CLI_DOWNLOAD_HASH','官方组件SHA256校验失败，原组件保留。')
            with tarfile.open(archive,'r:gz') as packed:
                members=packed.getmembers()
                require(len(members)<=8 and not any(m.issym() or m.islnk() for m in members),'CLI_ARCHIVE','组件归档格式不安全。')
                matches=[m for m in members if m.isfile() and Path(m.name).name in (asset['name'],asset['archive_name'][:-7])]
                require(len(matches)==1 and 0<matches[0].size<600*1024*1024,'CLI_ARCHIVE','组件归档文件不正确。')
                with packed.extractfile(matches[0]) as source,(folder/asset['name']).open('xb') as out:
                    shutil.copyfileobj(source,out,1024*1024)
            with (folder/asset['name']).open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
            files.append({'name':asset['name'],'sha256':digest,'bytes':(folder/asset['name']).stat().st_size})
            completed+=size
        return files

    async def probe(self,exe,home):
        from .provider import Rpc
        from .account_storage import cli_environment,cli_options
        schemas=home/'protocol-schema'
        proc=await asyncio.create_subprocess_exec(str(exe),*cli_options(home),'app-server','generate-json-schema','--out',str(schemas),'--experimental',
            env=cli_environment(home),stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL,
            creationflags=0x08000000 if os.name=='nt' else 0)
        try: code=await asyncio.wait_for(proc.wait(),30)
        finally:
            if proc.returncode is None:proc.kill();await proc.wait()
        require(code==0,'CLI_PROTOCOL','新组件无法导出接口定义，原组件保留。')
        for filename,fields in [('TurnStartParams',{'threadId','input','model','effort','outputSchema','serviceTierForTurn'}),
                                ('ThreadStartParams',{'model','modelProvider','ephemeral','approvalPolicy','sandbox','serviceTier','allowProviderModelFallback'})]:
            schema=load(schemas/'v2'/(filename+'.json'),{})
            require(fields<=set(schema.get('properties',{})), 'CLI_PROTOCOL','新组件提交接口不兼容，原组件保留。')
        rpc=Rpc(home,executable_path=str(exe))
        try:
            await rpc.start()
            identity=await rpc.request('account/read',{'refreshToken':False},timeout=15)
            require(identity.get('account') is None,'CLI_PROBE_ISOLATION','组件接口测试意外发现账户，已拒绝更新。')
            models=await rpc.request('model/list',{'includeHidden':False,'limit':100},timeout=20)
            self.validate_models(models)
            # No turn/start: this creates only an ephemeral local thread, never inference.
            thread=await rpc.request('thread/start',{'model':self.settings.data['model'],'modelProvider':'openai','approvalPolicy':'never',
                'sandbox':'read-only','ephemeral':True,'experimentalRawEvents':False,'serviceTier':'default','environments':[],
                'selectedCapabilityRoots':[],'allowProviderModelFallback':False,'config':{'project_doc_max_bytes':0,'web_search':'disabled'}},timeout=15)
            require((thread.get('thread') or {}).get('id'),'CLI_PROTOCOL','新组件会话接口不兼容，原组件保留。')
        finally:await rpc.close()

    def validate_models(self,models):
        require(not models.get('nextCursor'),'CLI_PROTOCOL','组件目录未完整返回，原组件保留。')
        for key,effort in [('model','effort'),('reference_model','reference_effort')]:
            model=next((m for m in models.get('data',[]) if (m.get('model') or m.get('id'))==self.settings.data[key]),None)
            require(model and 'image' in model.get('inputModalities',[]) and self.settings.data[effort] in
                    [e.get('reasoningEffort') for e in model.get('supportedReasoningEfforts',[])],'CLI_PROTOCOL','新组件不支持当前模型、图片或推理设置，原组件保留。')
            if self.settings.data.get('speed')=='fast':
                require(any(t.get('id') in ('fast','priority') or 'fast' in t.get('name','').lower() for t in model.get('serviceTiers',[])),
                        'CLI_PROTOCOL','新组件未提供当前快速模式，原组件保留。')

    async def _prepare(self,candidate):
        folder=self.root/'versions'/(candidate['version']+'-'+uid()[:8]);folder.mkdir(parents=True)
        self.save(phase='downloading',message='正在下载并校验官方组件…',downloaded_bytes=0,total_bytes=candidate['bytes'],error_code=None)
        try:
            files=await asyncio.to_thread(self.download,candidate,folder);self.check_cancelled()
            self.save(phase='testing',message='正在测试组件接口，不调用模型…')
            proc=await asyncio.create_subprocess_exec(str(folder/'codex.exe'),'--version',stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,creationflags=0x08000000 if os.name=='nt' else 0)
            try:out,_=await asyncio.wait_for(proc.communicate(),10)
            finally:
                if proc.returncode is None:proc.kill();await proc.wait()
            require(out.decode().strip()=='codex-cli '+candidate['version'],'CLI_VERSION','下载组件的实际版本不符。')
            with tempfile.TemporaryDirectory(prefix='autoreview-cli-probe-') as temporary:
                await self.probe(folder/'codex.exe',Path(temporary))
            self.check_cancelled()
            atomic_write(folder/'manifest.json',dumps({'version':candidate['version'],'files':files,'source':candidate['source']}).encode())
            entry={'version':candidate['version'],'directory':folder.name}
            self.save(phase='waiting_idle',message='组件已验证，等待队列、采集及登录操作空闲后切换。',prepared=entry)
            for asset in candidate['files']:
                (self.root/'downloads'/(asset['sha256']+'.part')).unlink(missing_ok=True)
        except asyncio.CancelledError: raise
        except Exception as error:
            self.save(phase='cancelled' if self.cancelled.is_set() else 'failed',message=getattr(error,'message','组件下载或测试失败，原组件保留。'),error_code=getattr(error,'code','CLI_UPDATE_FAILED'),prepared=None)

    async def apply_ready(self):
        if self.state.get('phase')!='waiting_idle' or self.closed:return
        try:
            self.idle()
        except Problem:return
        previous=load(self.root/'active.json',{});entry=self.state.get('prepared')
        try:
            async with self.switch_gate():
                self.check_cancelled();exe=checked_executable(self.root,entry)
                self.save(phase='switching',message='正在切换已验证的组件…')
                await self.provider.close()
                atomic_write(self.root/'active.json',dumps({'current':entry,'previous':previous.get('current'),'activation_owner':_activation_owner}).encode())
                from .provider import Rpc
                profile=self.settings.data.get('active_profile_id')
                # Read identity through the official CLI only, in its original home.
                if profile:
                    client=await self.provider.client(profile)
                    identity=await self.provider.read_identity(client)
                    require((identity.get('account') or {}).get('type')=='chatgpt','CLI_ACCOUNT_COMPATIBILITY','新组件无法复用本副本登录，已回退。')
                    self.validate_models(await client.request('model/list',{'includeHidden':False,'limit':100},timeout=20))
                else:
                    with tempfile.TemporaryDirectory(prefix='autoreview-cli-activation-') as temporary:
                        await self.probe(exe,Path(temporary))
                atomic_write(self.root/'active.json',dumps({'current':entry,'previous':previous.get('current')}).encode())
                self.save(phase='updated',message='Codex CLI组件已更新；账户、作业与模型设置保持。',current_version='codex-cli '+entry['version'],prepared=None,error_code=None)
        except BaseException as error:
            await self.provider.close()
            atomic_write(self.root/'active.json',dumps(previous).encode())
            self.save(phase='rolled_back',message='新组件切换未完成，已回退原组件。账户与作业保持。',error_code=getattr(error,'code','CLI_SWITCH_FAILED'),prepared=None)
            if isinstance(error,asyncio.CancelledError):raise

    async def cancel(self):
        require(self.state.get('phase')!='switching','CLI_SWITCH_ACTIVE','正在完成组件切换，请稍候。',409)
        self.cancelled.set()
        return self.save(phase='cancelled',message='已取消组件更新，原组件保留。',prepared=None,offer_id=None)

    async def close(self):
        self.closed=True;self.cancelled.set()
        if self.check_job:self.check_job.cancel()
        if self.job:self.job.cancel()
        await asyncio.gather(*(t for t in (self.check_job,self.job) if t),return_exceptions=True)
