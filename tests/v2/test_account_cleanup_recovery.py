import asyncio
from pathlib import Path
from types import SimpleNamespace
import pytest
from autoreview.v2.core import MachineSettings, Problem
from autoreview.v2 import provider as module


def provider(tmp_path):
    settings=MachineSettings(tmp_path/'copy')
    settings.save(profiles=[{'id':'one'},{'id':'two'}],active_profile_id='one')
    return module.Provider(settings)


class SignedOut:
    def __init__(self,home):self.home=home
    async def start(self):self.home.mkdir(parents=True,exist_ok=True)
    async def close(self):pass
    async def request(self,method,params,timeout=15):
        assert method=='account/read' and params=={'refreshToken':False}
        return {'account':None}


def mock_logout(monkeypatch,stderr,code=1):
    calls=[]
    class Process:
        returncode=code
        async def communicate(self):return b'',stderr.encode()
    async def spawn(exe,*args,**kwargs):
        calls.append((exe,args,kwargs))
        return Process()
    monkeypatch.setattr(module.asyncio,'create_subprocess_exec',spawn)
    monkeypatch.setattr(module,'executable',lambda home=None:str(home/'official-cli.exe'))
    monkeypatch.setattr(module,'Rpc',SignedOut)
    return calls


def test_damaged_store_cleanup_does_not_require_decryption_and_preserves_other_profile(tmp_path,monkeypatch):
    p=provider(tmp_path);one=p.settings.home/'profiles/one';two=p.settings.home/'profiles/two'
    one.mkdir(parents=True);two.mkdir();(one/'broken.age').write_bytes(b'synthetic invalid ciphertext')
    (two/'keep.txt').write_text('other account')
    calls=mock_logout(monkeypatch,'Error logging out: failed to decrypt secrets file: Decryption failed')
    result=asyncio.run(p.remove_profile('one'))
    assert result=={'ok':True,'cleanup_mode':'damaged_store_reset'}
    assert not one.exists() and (two/'keep.txt').read_text()=='other account'
    assert p.settings.data['profiles']==[{'id':'two'}] and p.settings.data['active_profile_id'] is None
    assert calls[0][0]==str(p.settings.home/'official-cli.exe')


def test_unknown_logout_failure_is_not_treated_as_corrupt_store(tmp_path,monkeypatch):
    p=provider(tmp_path);one=p.settings.home/'profiles/one';one.mkdir(parents=True)
    calls=mock_logout(monkeypatch,'Access denied to a credential manager entry')
    with pytest.raises(Problem) as error:asyncio.run(p.remove_profile('one'))
    assert error.value.code=='ACCOUNT_CLEANUP_FAILED' and error.value.details['stage']=='official_logout'
    assert one.exists() and len(p.settings.data['profiles'])==2
    assert 'Access denied' not in str(error.value.details)


def test_file_cleanup_failure_keeps_index_and_can_retry(tmp_path,monkeypatch):
    from autoreview.v2 import account_storage
    p=provider(tmp_path);one=p.settings.home/'profiles/one';one.mkdir(parents=True)
    mock_logout(monkeypatch,'Not logged in',code=0)
    deleted_keys=[]
    monkeypatch.setattr(account_storage,'remove_keyring_keys',lambda home:deleted_keys.append(home))
    original=module.shutil.rmtree
    def blocked(*args,**kwargs):raise PermissionError('file in use')
    monkeypatch.setattr(module.shutil,'rmtree',blocked)
    with pytest.raises(Problem) as error:asyncio.run(p.remove_profile('one'))
    assert error.value.details['stage']=='profile_files' and len(p.settings.data['profiles'])==2
    assert deleted_keys==[]
    monkeypatch.setattr(module.shutil,'rmtree',original)
    assert asyncio.run(p.remove_profile('one'))['ok']
    assert deleted_keys==[one]


def test_windows_long_cache_path_cleanup_and_no_key_delete_before_files(tmp_path,monkeypatch):
    import os
    from autoreview.v2 import account_storage
    if os.name!='nt':pytest.skip('Windows MAX_PATH regression')
    p=provider(tmp_path);home=p.settings.home/'profiles/one';home.mkdir(parents=True)
    nested=home.joinpath(*['plugin-cache-'+('x'*35)]*7)/'reference.xlsx'
    extended=Path('\\\\?\\'+str(nested.resolve()))
    extended.parent.mkdir(parents=True);extended.write_bytes(b'public synthetic long-path cache')
    assert len(str(nested))>320
    mock_logout(monkeypatch,'Not logged in',code=0)
    def keys_after_files(value):assert not home.exists()
    monkeypatch.setattr(account_storage,'remove_keyring_keys',keys_after_files)
    assert asyncio.run(p.remove_profile('one'))['ok'] and not extended.exists()


def test_cleanup_never_accepts_link_to_other_copy(tmp_path,monkeypatch):
    p=provider(tmp_path);outside=tmp_path/'other-copy';outside.mkdir()
    (p.settings.home/'profiles').mkdir()
    try:(p.settings.home/'profiles/one').symlink_to(outside,target_is_directory=True)
    except OSError:pytest.skip('symlink privilege unavailable')
    with pytest.raises(Problem) as error:asyncio.run(p.remove_profile('one'))
    assert error.value.code=='ACCOUNT_PATH' and outside.exists()


def test_cleanup_rejects_profiles_parent_junction_to_other_copy(tmp_path):
    import os,subprocess
    if os.name!='nt':pytest.skip('Windows junction regression')
    p=provider(tmp_path);outside=tmp_path/'other-copy';(outside/'one').mkdir(parents=True)
    (outside/'one/keep.txt').write_text('other account untouched')
    link=p.settings.home/'profiles'
    made=subprocess.run(['cmd','/c','mklink','/J',str(link),str(outside)],capture_output=True)
    assert made.returncode==0
    try:
        with pytest.raises(Problem) as error:asyncio.run(p.remove_profile('one'))
        assert error.value.code=='ACCOUNT_PATH' and (outside/'one/keep.txt').read_text()=='other account untouched'
    finally:os.rmdir(link)


def test_repair_retains_profile_identity_and_other_accounts(tmp_path,monkeypatch):
    p=provider(tmp_path);deleted=[]
    async def forget(profile_id):deleted.append(profile_id)
    async def login(profile_id,device=False):return {'profile_id':profile_id,'authUrl':'https://auth.openai.com/synthetic'}
    monkeypatch.setattr(p,'forget',forget);monkeypatch.setattr(p,'login',login)
    before=[dict(item) for item in p.settings.data['profiles']]
    result=asyncio.run(p.repair_login('one'))
    assert deleted==['one'] and result['profile_id']=='one'
    assert p.settings.data['profiles']==before and p.settings.data['active_profile_id']=='one'


@pytest.mark.parametrize('case',['missing_identity','damaged','missing_quota','quota_available'])
def test_identity_and_quota_states_are_distinct(tmp_path,case):
    p=provider(tmp_path)
    class Client:
        executable_path='official-cli.exe';server_info={}
        async def request(self,method,params=None,timeout=15):
            if method=='account/read':
                if case=='damaged':raise Problem('CODEX_RPC_ERROR','failed to decrypt secrets file: Decryption failed')
                return {'account':None if case=='missing_identity' else {'type':'chatgpt'}}
            if method=='account/rateLimits/read':
                if case=='missing_quota':raise TimeoutError()
                return {'rateLimits':{'primary':{'usedPercent':30}}}
            if method=='account/tokenUsage/read':return {}
            if method=='model/list':return {'data':[],'nextCursor':None}
            raise AssertionError(method)
    result=asyncio.run(p._account(Client(),'one'))
    if case in ('missing_identity','damaged'):
        assert result['account'] is None and result['rate_limits'] is None and result['quota_status']=='not_connected'
        assert result['warning'] and result['connection_state']==('credentials_unreadable' if case=='damaged' else 'not_connected')
    elif case=='missing_quota':
        assert result['account'] and result['quota_status']=='unavailable' and result['quota_warning']
    else:assert result['quota_status']=='available' and 'quota_warning' not in result


def test_account_reads_finish_before_delete_preventing_profile_resurrection(tmp_path,monkeypatch):
    p=provider(tmp_path)
    async def scenario():
        started=asyncio.Event();release=asyncio.Event();order=[]
        async def reading(*args):
            order.append('read');started.set();await release.wait();order.append('read-end');return {}
        async def forget(pid):order.append('delete')
        monkeypatch.setattr(p,'_account_current',reading);monkeypatch.setattr(p,'forget',forget)
        task=asyncio.create_task(p.account());await started.wait()
        deletion=asyncio.create_task(p.remove_profile('one'));await asyncio.sleep(0)
        assert not deletion.done();release.set();await task;await deletion
        assert order==['read','read-end','delete'] and p.settings.data['profiles']==[{'id':'two'}]
    asyncio.run(scenario())


def test_repair_api_requires_confirmation_and_idle(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from autoreview.v2.api import create_app
    p=provider(tmp_path);p.settings.save(library_location=str(tmp_path/'library'))
    called=[]
    async def repair(pid):called.append(pid);return {'profile_id':pid,'loginId':'synthetic'}
    monkeypatch.setattr(p,'repair_login',repair)
    app=create_app(p.settings,p,nonce='synthetic-repair',start_worker=False)
    with TestClient(app,headers={'x-autoreview-session':'synthetic-repair'}) as client:
        assert client.post('/api/account/repair-login',json={'profile_id':'one'}).status_code==422
        app.state.runner.active_work['synthetic-work']='busy'
        assert client.post('/api/account/repair-login',json={'profile_id':'one','confirm':True}).status_code==409
        assert not called
        app.state.runner.active_work.clear()
        assert client.post('/api/account/repair-login',json={'profile_id':'one','confirm':True}).status_code==200
        assert called==['one']
