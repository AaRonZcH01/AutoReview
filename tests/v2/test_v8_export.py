import zipfile
from pathlib import Path
from fastapi.testclient import TestClient
from autoreview.v2.api import create_app
from autoreview.v2.core import MachineSettings, uid, Problem
from autoreview.v2.reference_pack import export_pack
from test_workflow import service


def test_export_uses_explicit_chosen_filename_and_directory(service, tmp_path):
    task=service.create_task({'name':'导出路径验证','custom':'1班','request_id':uid()})
    selected=tmp_path/'chosen';selected.mkdir();path=selected/'自选范本包.zip'
    result=export_pack(service,task['id'],str(path))
    assert Path(result['path'])==path
    with zipfile.ZipFile(path) as z: assert 'reference.json' in z.namelist()
    assert not list(Path(service.settings.data['export_directory']).glob('*.zip'))


def test_native_save_picker_receives_default_and_cancel_does_not_export(tmp_path, monkeypatch):
    import tkinter as tk
    from tkinter import filedialog
    settings=MachineSettings(tmp_path/'machine')
    settings.save(library_location=str(tmp_path/'library'),export_directory=str(tmp_path/'exports'))
    events=[]
    class Root:
        def withdraw(self): events.append('withdraw')
        def attributes(self,*a): events.append(a)
        def destroy(self): events.append('destroy')
    monkeypatch.setattr(tk,'Tk',Root)
    captured={}
    def choose(**kwargs):captured.update(kwargs);return ''
    monkeypatch.setattr(filedialog,'asksaveasfilename',choose)
    app=create_app(settings,nonce='test-only',start_worker=False)
    with TestClient(app,headers={'X-AutoReview-Session':'test-only'}) as c:
        r=c.post('/api/native/save-file',json={'filename':'范本答案.zip'})
        assert r.status_code==200 and r.json()['path']==''
    assert captured['initialdir']==settings.data['export_directory']
    assert captured['initialfile']=='范本答案.zip' and captured['confirmoverwrite'] is True
    assert events[-1]=='destroy'
    assert not (tmp_path/'exports').exists()
