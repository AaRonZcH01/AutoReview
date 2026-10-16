import asyncio
import json
from pathlib import Path
import pytest
from autoreview.v2.core import MachineSettings, Problem
from autoreview.v2.account_storage import instance_home, cli_environment, cli_options
from autoreview.v2 import provider as module


def test_fresh_copy_does_not_inherit_legacy_or_other_copy(tmp_path, monkeypatch):
    legacy = tmp_path/'local/AutoReviewDesktop'
    legacy.mkdir(parents=True)
    (legacy/'settings.json').write_text(json.dumps({'profiles':[{'id':'old','display_email':'private@example.test'}]}))
    monkeypatch.delenv('AUTOREVIEW_HOME', raising=False)
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path/'local'))
    monkeypatch.setenv('AUTOREVIEW_INSTANCE_DIR', str(tmp_path/'copy-one'))
    one=MachineSettings();one.save(profiles=[{'id':'one','display_email':'one@example.test'}], active_profile_id='one')
    monkeypatch.setenv('AUTOREVIEW_INSTANCE_DIR', str(tmp_path/'copy-two'))
    two=MachineSettings()
    assert two.data['profiles']==[] and two.data['active_profile_id'] is None
    assert one.home != two.home
    assert MachineSettings(one.home).data['profiles'][0]['id']=='one'
    assert b'one@example.test' not in (one.home/'accounts.dpapi').read_bytes()
    assert 'profiles' not in json.loads(one.path.read_text('utf-8'))
    assert (legacy/'settings.json').exists()


def test_official_child_does_not_inherit_host_routing_or_keys(tmp_path, monkeypatch):
    for key in ['CODEX_SESSION_ID','CODEX_INTERNAL_ORIGINATOR_OVERRIDE','CODEX_API_KEY','OPENAI_API_KEY','OPENAI_BASE_URL']:
        monkeypatch.setenv(key,'private-do-not-inherit')
    monkeypatch.setenv('HTTPS_PROXY','http://localhost:1234')
    env=cli_environment(tmp_path)
    assert env['CODEX_HOME']==str(tmp_path) and env['HTTPS_PROXY']=='http://localhost:1234'
    assert not any(v=='private-do-not-inherit' for v in env.values())
    options=cli_options(tmp_path)
    assert 'cli_auth_credentials_store="keyring"' in options and 'auth_keyring_backend="secrets"' in options


class FakeRpc:
    identity=None
    fail=False
    def __init__(self,home):
        self.home=home;self.closed=False;self.calls=[]
    async def start(self):pass
    async def close(self):self.closed=True
    async def request(self,method,params=None,timeout=45):
        self.calls.append((method,params))
        if method=='account/login/start':
            if self.fail:raise Problem('OFFLINE','offline')
            return {'loginId':'pending','authUrl':'https://auth.openai.com/test'}
        if method=='account/read':return {'account':self.identity}
        if method=='account/login/cancel':return {}
        raise AssertionError(method)


@pytest.fixture
def provider(tmp_path, monkeypatch):
    monkeypatch.setattr(module,'Rpc',FakeRpc)
    FakeRpc.identity=None;FakeRpc.fail=False
    p=module.Provider(MachineSettings(tmp_path/'private'))
    async def forget(profile_id):
        client=p.clients.pop(profile_id,None)
        if client:await client.close()
        p.login_urls.pop(profile_id,None)
    monkeypatch.setattr(p,'forget',forget)
    return p


def test_login_is_staged_and_only_success_commits(provider):
    async def scenario():
        p=provider;login=await p.add_profile('new')
        assert p.settings.data['profiles']==[] and p.settings.data['active_profile_id'] is None
        assert not (await p.login_status(login['profile_id']))['complete']
        FakeRpc.identity={'type':'chatgpt','email':'teacher@example.test'}
        assert (await p.login_status(login['profile_id']))['complete']
        assert len(p.settings.data['profiles'])==1
        assert MachineSettings(p.settings.home).data['active_profile_id']==login['profile_id']
        assert p.settings.data['pending_profiles']==[]
        await p.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure',[False,True])
def test_cancel_or_failure_leaves_no_ghost(provider,failure):
    async def scenario():
        FakeRpc.fail=failure
        if failure:
            with pytest.raises(Problem):await provider.add_profile('new')
        else:
            login=await provider.add_profile('new')
            await provider.cancel_login(login['profile_id'],'pending')
        assert provider.settings.data['profiles']==[]
        assert provider.settings.data['pending_profiles']==[]
        assert provider.pending_profiles=={}
        await provider.close()
    asyncio.run(scenario())


def test_delete_does_not_auto_select_other_account(provider):
    async def scenario():
        provider.settings.save(profiles=[{'id':'one'},{'id':'two'}],active_profile_id='one')
        await provider.remove_profile('one')
        assert provider.settings.data['active_profile_id'] is None
        assert provider.settings.data['profiles']==[{'id':'two'}]
    asyncio.run(scenario())


def test_routing_timeout_retries_identity_but_never_infers(provider):
    class Routing:
        calls=[]
        async def request(self,method,params,timeout):
            self.calls.append((method,params))
            if not params['refreshToken']:raise Problem('CODEX_RPC_ERROR','workspace routing discovery timed out')
            return {'account':{'type':'chatgpt'}}
    rpc=Routing()
    assert asyncio.run(provider.read_identity(rpc))['account']['type']=='chatgpt'
    assert [m for m,_ in rpc.calls]==['account/read','account/read']


def test_cleanup_failure_keeps_registered_account(provider, monkeypatch):
    async def fail(pid):raise Problem('ACCOUNT_CLEANUP_FAILED','not yet removed')
    monkeypatch.setattr(provider,'forget',fail)
    provider.settings.save(profiles=[{'id':'one'}],active_profile_id='one')
    with pytest.raises(Problem):asyncio.run(provider.remove_profile('one'))
    assert MachineSettings(provider.settings.home).data['profiles']==[{'id':'one'}]


def test_delete_api_requires_confirmation_and_idle_task(provider,tmp_path):
    from fastapi.testclient import TestClient
    from autoreview.v2.api import create_app
    provider.settings.save(library_location=str(tmp_path/'library'),profiles=[{'id':'one'}],active_profile_id='one')
    app=create_app(provider.settings,provider=provider,nonce='local-auth',start_worker=False)
    with TestClient(app,headers={'X-AutoReview-Session':'local-auth'}) as client:
        assert client.post('/api/account/delete',json={'profile_id':'one'}).status_code==422
        app.state.runner.active_work['owned-work']='owned-running-task'
        assert client.post('/api/account/delete',json={'profile_id':'one','confirm':True}).status_code==409
        app.state.runner.active_work.clear()
        assert client.post('/api/account/delete',json={'profile_id':'one','confirm':True}).status_code==200
        assert client.get('/api/state').json()['settings']['profiles']==[]


def test_windows_key_cleanup_is_scoped_and_deletes_without_reading_values(tmp_path):
    import os,ctypes,hashlib
    if os.name!='nt':pytest.skip('Windows credential API')
    from ctypes import wintypes as w
    from autoreview.v2.account_storage import remove_keyring_keys
    class FileTime(ctypes.Structure):_fields_=[('lo',w.DWORD),('hi',w.DWORD)]
    class Credential(ctypes.Structure):
        _fields_=[('Flags',w.DWORD),('Type',w.DWORD),('TargetName',w.LPWSTR),('Comment',w.LPWSTR),
                  ('LastWritten',FileTime),('CredentialBlobSize',w.DWORD),('CredentialBlob',ctypes.c_void_p),
                  ('Persist',w.DWORD),('AttributeCount',w.DWORD),('Attributes',ctypes.c_void_p),('TargetAlias',w.LPWSTR),('UserName',w.LPWSTR)]
    api=ctypes.WinDLL('advapi32',use_last_error=True)
    api.CredWriteW.argtypes=[ctypes.POINTER(Credential),w.DWORD]
    api.CredReadW.argtypes=[w.LPCWSTR,w.DWORD,w.DWORD,ctypes.POINTER(ctypes.c_void_p)]
    api.CredFree.argtypes=[ctypes.c_void_p]
    homes=[tmp_path/'one',tmp_path/'two'];targets=[]
    def exists(target):
        pointer=ctypes.c_void_p()
        result=api.CredReadW(target,1,0,ctypes.byref(pointer))
        if result:api.CredFree(pointer)  # No credential blob is inspected.
        return bool(result)
    try:
        for home in homes:
            home.mkdir()
            suffix=hashlib.sha256(('\\\\?\\'+str(home.resolve())).encode()).hexdigest()[:16]
            target=f'secrets|{suffix}.codex';targets.append(target)
            blob=ctypes.create_string_buffer(b'public-synthetic-encryption-key-test')
            value=Credential(Type=1,TargetName=target,CredentialBlobSize=len(blob),CredentialBlob=ctypes.addressof(blob),Persist=2,UserName='synthetic-only')
            assert api.CredWriteW(ctypes.byref(value),0)
        remove_keyring_keys(homes[0])
        assert not exists(targets[0]) and exists(targets[1])
    finally:
        for home in homes:
            if home.exists():remove_keyring_keys(home)
