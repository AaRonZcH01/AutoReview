import asyncio,hashlib,io,json,tarfile
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
import pytest
from autoreview.v2 import cli_updates as module
from autoreview.v2.core import MachineSettings,Problem,atomic_write,dumps


def release(v='0.163.1'):
    tag='rust-v'+v
    return {'tag_name':tag,'assets':[{'name':n+'-x86_64-pc-windows-msvc.exe.tar.gz','size':12,'digest':'sha256:'+'a'*64,
        'browser_download_url':f'https://github.com/openai/codex/releases/download/{tag}/{n}-x86_64-pc-windows-msvc.exe.tar.gz'} for n in module.FILES]}


def updater(tmp_path):
    settings=MachineSettings(tmp_path/'private')
    class Provider:
        pending_profiles={};active_turns={}
        async def close(self):pass
    @asynccontextmanager
    async def gate():yield
    return module.CliUpdater(settings,Provider(),lambda:None,gate)


def prepared(u,version='0.163.1',suffix='abcd1234'):
    directory=version+'-'+suffix;folder=u.root/'versions'/directory;folder.mkdir(parents=True)
    files=[]
    for n in module.FILES:
        data=b'fake executable, never run';(folder/(n+'.exe')).write_bytes(data)
        files.append({'name':n+'.exe','sha256':hashlib.sha256(data).hexdigest()})
    atomic_write(folder/'manifest.json',dumps({'version':version,'files':files}).encode())
    return {'version':version,'directory':directory},folder


def test_release_selection_only_supported_stable_official_complete():
    good=release()
    assert module.release_candidate([good],'codex-cli 0.163.0-alpha.10.2')[0]['version']=='0.163.1'
    assert module.release_candidate([release('0.164.0')],'0.163.0')==(None,True)
    for bad in [{**good,'prerelease':True},{**good,'draft':True},release('0.162.9')]:
        assert module.release_candidate([bad],'0.163.0')[0] is None
    for field,value in [('digest','bad'),('browser_download_url','https://evil.invalid/codex.exe'),('size',module.MAX_DOWNLOAD+1)]:
        bad=release();bad['assets'][0][field]=value
        assert module.release_candidate([bad],'0.163.0')[0] is None


def test_default_off_startup_no_network_and_manual_always_checks(tmp_path,monkeypatch):
    u=updater(tmp_path);calls=[]
    async def check():calls.append(True);return {'phase':'available'}
    u._check=check
    async def scenario():
        assert not u.settings.data['cli_check_on_startup']
        await u.check('startup');assert not calls
        await u.check('manual');assert len(calls)==1
        u.settings.data['cli_check_on_startup']=True
        await u.check('startup');await u.check('startup');assert len(calls)==2
    asyncio.run(scenario())


def test_failed_check_preserves_component_and_does_not_download(tmp_path,monkeypatch):
    u=updater(tmp_path)
    async def current():return 'codex-cli 0.163.0'
    u.current_version=current
    monkeypatch.setattr(module,'open_official',lambda *_:(_ for _ in ()).throw(OSError('network')))
    assert asyncio.run(u.check())['phase']=='check_failed'
    assert not (u.root/'active.json').exists()


def test_confirmation_is_required_stale_and_double_click_is_idempotent(tmp_path):
    u=updater(tmp_path);u.save(phase='available',offer_id='offer',candidate=release())
    async def scenario():
        with pytest.raises(Problem):await u.confirm('offer',False)
        with pytest.raises(Problem):await u.confirm('old',True)
        entered=0;hold=asyncio.Event()
        async def prepare(_):
            nonlocal entered;entered+=1;await hold.wait()
        u._prepare=prepare
        await u.confirm('offer',True);await u.confirm('offer',True);await asyncio.sleep(0)
        assert entered==1;hold.set();await u.job
    asyncio.run(scenario())


def test_wait_for_idle_then_switch_without_inference(tmp_path):
    u=updater(tmp_path);entry,folder=prepared(u);u.save(phase='waiting_idle',prepared=entry)
    def busy():raise Problem('RUN_ACTIVE','busy')
    u.idle=busy
    async def probe(*_):pass
    u.probe=probe
    async def scenario():
        await u.apply_ready();assert u.state['phase']=='waiting_idle'
        assert not (u.root/'active.json').exists()
        u.idle=lambda:None;await u.apply_ready()
        assert u.state['phase']=='updated'
        assert module.active_executable(u.settings.home)==str(folder/'codex.exe')
        assert not module.active_executable(tmp_path/'another-copy')
    asyncio.run(scenario())


def test_bad_candidate_rolls_back_and_does_not_touch_original(tmp_path):
    u=updater(tmp_path);previous,old=prepared(u,'0.163.0');entry,new=prepared(u)
    atomic_write(u.root/'active.json',dumps({'current':previous}).encode());u.save(phase='waiting_idle',prepared=entry)
    async def fail(*_):raise Problem('CLI_PROTOCOL','bad')
    u.probe=fail;asyncio.run(u.apply_ready())
    assert u.state['phase']=='rolled_back'
    assert module.active_executable(u.settings.home)==str(old/'codex.exe')


def test_missing_tampered_and_interrupted_activation_revert(tmp_path):
    for fault in ('tamper','missing','interrupted'):
        u=updater(tmp_path/fault);entry,folder=prepared(u)
        pointer={'current':entry}
        if fault=='tamper':(folder/'codex.exe').write_bytes(b'tampered')
        elif fault=='missing':(folder/'codex.exe').unlink()
        else:pointer['activation_owner']='prior-process'
        atomic_write(u.root/'active.json',dumps(pointer).encode())
        assert module.active_executable(u.settings.home) is None
        assert module.load(u.root/'state.json')['phase']=='rolled_back'


def test_download_checks_hash_and_never_extracts_unsafe_names(tmp_path,monkeypatch):
    u=updater(tmp_path);folder=tmp_path/'download';folder.mkdir()
    data=b'executable test bytes';packed=io.BytesIO()
    with tarfile.open(fileobj=packed,mode='w:gz') as tf:
        member=tarfile.TarInfo('../codex.exe');member.size=len(data);tf.addfile(member,io.BytesIO(data))
    content=packed.getvalue()
    monkeypatch.setattr(module,'open_official',lambda *_:io.BytesIO(content))
    candidate={'bytes':len(content),'files':[{'name':'codex.exe','archive_name':'codex.exe.tar.gz','url':'unused','bytes':len(content),'sha256':'0'*64}]}
    with pytest.raises(Problem) as error:u.download(candidate,folder)
    assert error.value.code=='CLI_DOWNLOAD_HASH' and not (tmp_path/'codex.exe').exists()


def test_cancelled_prepared_intent_does_not_apply_after_restart(tmp_path):
    u=updater(tmp_path);entry,_=prepared(u);u.save(phase='waiting_idle',prepared=entry)
    asyncio.run(u.cancel());reopened=updater(tmp_path)
    asyncio.run(reopened.apply_ready())
    assert not (u.root/'active.json').exists()


def test_resumes_confirmed_waiting_only_and_never_partial_download(tmp_path):
    u=updater(tmp_path);entry,_=prepared(u);u.save(phase='waiting_idle',prepared=entry)
    assert updater(tmp_path).state['phase']=='waiting_idle'
    u.save(phase='downloading');assert updater(tmp_path).state['phase']=='interrupted'


def test_api_toggle_validation_and_startup_skip(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from autoreview.v2.api import create_app
    from test_workflow import FakeProvider
    settings=MachineSettings(tmp_path/'machine');settings.save(library_location=str(tmp_path/'library'))
    app=create_app(settings,FakeProvider(),nonce='local-update',start_worker=False)
    with TestClient(app,headers={'X-AutoReview-Session':'local-update'}) as client:
        assert client.get('/api/cli-update').json()['enabled'] is False
        assert client.post('/api/settings',json={'cli_check_on_startup':'true'}).status_code==422
        assert client.post('/api/cli-update/check',json={'trigger':'startup'}).json()['phase']=='idle'
        assert client.post('/api/settings',json={'cli_check_on_startup':True}).status_code==200
        calls=[]
        async def check():calls.append(1);return {'phase':'up_to_date'}
        app.state.cli_updater._check=check
        assert client.post('/api/cli-update/check',json={'trigger':'startup'}).status_code==200
        assert client.post('/api/cli-update/check',json={'trigger':'startup'}).status_code==200
        assert len(calls)==1
        assert client.post('/api/settings',json={'cli_check_on_startup':False}).status_code==200
        assert client.post('/api/cli-update/check',json={'trigger':'manual'}).status_code==200
        assert len(calls)==2
        assert client.post('/api/cli-update/confirm',json={'confirm':False}).status_code==422


def test_redirect_rejects_remote_untrusted_hosts():
    redirect=module.SafeRedirect()
    with pytest.raises(Problem):redirect.redirect_request(None,None,302,'',{},'http://github.com/unsafe')
    with pytest.raises(Problem):redirect.redirect_request(None,None,302,'',{},'https://evil.invalid/a.exe')


def test_verified_archive_writes_fixed_filename_and_rejects_links(tmp_path,monkeypatch):
    u=updater(tmp_path)
    for symlink in (False,True):
        content=io.BytesIO();data=b'binary bytes'
        with tarfile.open(fileobj=content,mode='w:gz') as tf:
            member=tarfile.TarInfo('../codex.exe');member.size=len(data)
            if symlink:member.type=tarfile.SYMTYPE;member.linkname='outside'
            tf.addfile(member,io.BytesIO(data) if not symlink else None)
        blob=content.getvalue();folder=tmp_path/str(symlink);folder.mkdir()
        monkeypatch.setattr(module,'open_official',lambda *_:io.BytesIO(blob))
        item={'name':'codex.exe','archive_name':'codex.exe.tar.gz','url':'unused','bytes':len(blob),'sha256':hashlib.sha256(blob).hexdigest()}
        if symlink:
            with pytest.raises(Problem) as error:u.download({'bytes':len(blob),'files':[item]},folder)
            assert error.value.code=='CLI_ARCHIVE'
        else:
            u.download({'bytes':len(blob),'files':[item]},folder)
            assert (folder/'codex.exe').read_bytes()==data
        assert not (tmp_path/'codex.exe').exists()


def test_completed_settings_or_account_change_is_rechecked_before_activation(tmp_path):
    u=updater(tmp_path);entry,_=prepared(u);u.save(phase='waiting_idle',prepared=entry)
    async def fail(*_):raise Problem('CLI_ACCOUNT_COMPATIBILITY','fail')
    u.probe=fail
    asyncio.run(u.apply_ready())
    assert u.state['phase']=='rolled_back'
    assert not module.load(u.root/'active.json').get('current')


def test_cli_handshake_failure_reverts_before_retry_without_inference(tmp_path):
    from autoreview.v2.provider import Rpc,executable
    u=updater(tmp_path);entry,folder=prepared(u)
    atomic_write(u.root/'active.json',dumps({'current':entry}).encode())
    rpc=Rpc(u.settings.home/'profiles/test-profile');paths=[]
    async def start_once():
        rpc.executable_path=executable(u.settings.home);paths.append(rpc.executable_path)
        if len(paths)==1:raise OSError('unstartable managed binary')
    rpc.start_once=start_once
    asyncio.run(rpc.start())
    assert len(paths)==2 and paths[0]==str(folder/'codex.exe') and paths[1]!=paths[0]
    assert not module.load(u.root/'active.json').get('current')


def test_remote_check_has_total_deadline_and_reports_healthy_component(tmp_path,monkeypatch):
    import time
    u=updater(tmp_path)
    async def current():return 'codex-cli 0.163.0-alpha.10.2'
    u.current_version=current
    class Slow(io.BytesIO):
        def read(self,*args):time.sleep(.03);return super().read(*args)
    monkeypatch.setattr(module,'open_official',lambda *_:Slow(json.dumps(release()).encode()))
    original=asyncio.wait_for;deadlines=[]
    async def short(awaitable,timeout):deadlines.append(timeout);return await original(awaitable,.001)
    monkeypatch.setattr(module.asyncio,'wait_for',short)
    result=asyncio.run(u.check())
    assert deadlines==[20] and result['phase']=='check_failed' and result['error_code']=='TimeoutError'
    assert not (u.root/'active.json').exists()


def test_partial_download_resumes_with_range_and_full_official_hash(tmp_path,monkeypatch):
    u=updater(tmp_path);payload=b'executable fixture';buf=io.BytesIO()
    with tarfile.open(fileobj=buf,mode='w:gz') as tf:
        member=tarfile.TarInfo('codex.exe');member.size=len(payload);tf.addfile(member,io.BytesIO(payload))
    data=buf.getvalue();digest=hashlib.sha256(data).hexdigest();split=len(data)//2
    cache=u.root/'downloads';cache.mkdir(parents=True);(cache/(digest+'.part')).write_bytes(data[:split])
    class Response(io.BytesIO):
        status=206;headers={'Content-Range':f'bytes {split}-{len(data)-1}/{len(data)}'}
    calls=[]
    def network(url,headers):calls.append(headers);return Response(data[split:])
    monkeypatch.setattr(module,'open_official',network)
    folder=tmp_path/'resumed';folder.mkdir()
    item={'name':'codex.exe','archive_name':'codex.exe.tar.gz','url':'unused','bytes':len(data),'sha256':digest}
    u.download({'bytes':len(data),'files':[item]},folder)
    assert calls==[{'Range':f'bytes={split}-'}] and (folder/'codex.exe').read_bytes()==payload


def test_api_switch_guard_blocks_work_capture_login_and_library_writes(tmp_path):
    from autoreview.v2.api import create_app
    from test_workflow import FakeProvider,setup,config
    from autoreview.v2 import stream_capture as capture
    settings=MachineSettings(tmp_path/'settings');settings.save(library_location=str(tmp_path/'library'))
    provider=FakeProvider();app=create_app(settings,provider,nonce='test',start_worker=False)
    service=app.state.service;u=app.state.cli_updater
    try:
        app.state.data_users=1
        with pytest.raises(Problem,match='保存'):u.idle()
        app.state.data_users=0;app.state.runner.active_work['one']='owned'
        with pytest.raises(Problem,match='停止'):u.idle()
        app.state.runner.active_work.clear();provider.pending_profiles={'pending':{}}
        with pytest.raises(Problem,match='空闲'):u.idle()
        provider.pending_profiles={};task,groups,_=setup(service,1)
        session=capture.start(service,task['id'],{'group_id':groups[0]['id'],'source':'files','cloud_consent':True,'request_id':'test-capture'},config())
        with pytest.raises(Problem,match='采集'):u.idle()
        capture.stop(service,session['id']);u.idle()
    finally:service.close()
