import pytest
from fastapi.testclient import TestClient
from autoreview.v2.core import MachineSettings,Problem
from autoreview.v2.camera_profiles import update
from autoreview.v2.api import create_app
from test_workflow import FakeProvider

def test_camera_profiles_persist_without_changing_execution_or_other_device(tmp_path):
    settings=MachineSettings(tmp_path/'machine')
    settings.save(parallelism=7,capture_parallelism=10,conservative_review=True,capture_countdown_seconds=9)
    controls={'focusMode':'manual','focusDistance':210,'exposureMode':'continuous','contrast':20,'sharpness':20}
    update(settings,{'camera_label':'Web Camera','controls':controls,'settle_frames':1})
    update(settings,{'camera_label':'Integrated Camera','controls':{'focusMode':'continuous'}})
    reopened=MachineSettings(tmp_path/'machine')
    assert reopened.data['camera_profiles']['Web Camera']['controls']==controls
    assert reopened.data['camera_profiles']['Web Camera']['settle_frames']==1
    assert reopened.data['parallelism']==7 and reopened.data['capture_parallelism']==10
    assert reopened.data['conservative_review'] and reopened.data['capture_countdown_seconds']==9
    update(reopened,{'camera_label':'Web Camera','reset':True})
    assert set(reopened.data['camera_profiles'])=={'Integrated Camera'}

@pytest.mark.parametrize('data',[
    {'camera_label':''},{'camera_label':[]},{'camera_label':'x','controls':{'width':3840}},
    {'camera_label':'x','controls':{'focusMode':'single-shot'}},
    {'camera_label':'x','controls':{'focusDistance':123}},
    {'camera_label':'x','controls':{'exposureTime':78.125}},
    {'camera_label':'x','controls':{'contrast':True}},
    {'camera_label':'x','controls':{'contrast':float('nan')}},
    {'camera_label':'x','controls':{'contrast':float('inf')}},
    {'camera_label':'x','controls':{'contrast':10**500}},
    {'camera_label':'x','settle_frames':True},{'camera_label':'x','settle_frames':2},
    {'camera_label':'x','settle_frames':.5},
])
def test_invalid_camera_parameters_do_not_write(tmp_path,data):
    settings=MachineSettings(tmp_path/'machine')
    with pytest.raises(Problem):update(settings,data)
    assert settings.data['camera_profiles']=={}

def test_camera_profiles_api_local_and_reset(tmp_path):
    settings=MachineSettings(tmp_path/'machine');settings.save(library_location=str(tmp_path/'library'))
    app=create_app(settings,provider=FakeProvider(),nonce='local-camera',start_worker=False)
    with TestClient(app,headers={'X-AutoReview-Session':'local-camera'}) as client:
        assert client.get('/api/camera-profiles').json()=={}
        result=client.post('/api/camera-profiles',json={'camera_label':'USB camera','controls':{'exposureMode':'manual','exposureTime':156.25},'settle_frames':1})
        assert result.status_code==200 and result.json()['profile']['controls']['exposureTime']==156.25
        assert client.get('/api/state').json()['settings']['camera_profiles']['USB camera']['settle_frames']==1
        assert client.post('/api/camera-profiles',json={'camera_label':'USB camera','reset':True}).status_code==200
        assert client.get('/api/camera-profiles').json()=={}
