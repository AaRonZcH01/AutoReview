from fastapi.testclient import TestClient
from autoreview.v2.core import MachineSettings
from autoreview.v2.api import create_app
from test_workflow import FakeProvider

def test_create_library_returns_switch_result_without_awaiting_dict(tmp_path):
    settings=MachineSettings(tmp_path/'machine');settings.save(library_location=str(tmp_path/'original'))
    app=create_app(settings,FakeProvider(),nonce='test',start_worker=False)
    with TestClient(app) as client:
        client.headers['X-AutoReview-Session']='test'
        result=client.post('/api/library/create',json={'path':str(tmp_path)})
        assert result.status_code==200,result.text
        assert result.json()['restart_required'] is True
        assert settings.data['library_location']!=str(tmp_path/'original')
        assert (tmp_path/'original'/'database'/'autoreview.sqlite').exists()

def test_parallel_and_conservative_settings_are_validated(tmp_path):
    settings=MachineSettings(tmp_path/'machine');settings.save(library_location=str(tmp_path/'library'))
    app=create_app(settings,FakeProvider(),nonce='test',start_worker=False)
    with TestClient(app) as client:
        client.headers['X-AutoReview-Session']='test'
        for value in [0,14,True,'4',1.5]:
            assert client.post('/api/settings',json={'parallelism':value}).status_code==422
        for value in (1,4,7,10,13):
            saved=client.post('/api/settings',json={'parallelism':value})
            assert saved.status_code==200 and saved.json()['parallelism']==value
        result=client.post('/api/settings',json={'parallelism':3,'conservative_review':False,'auto_orient_images':False,'verification_effort':'medium'})
        assert result.status_code==200
        assert result.json()['parallelism']==3
        assert result.json()['conservative_review'] is False
        assert result.json()['auto_orient_images'] is False
        for value in [-1,61,True,'3',2.5]:
            assert client.post('/api/settings',json={'capture_countdown_seconds':value}).status_code==422
        for value in (0,3,17,60):
            response=client.post('/api/settings',json={'capture_countdown_seconds':value})
            assert response.status_code==200
            assert response.json()['capture_countdown_seconds']==value

def test_shutdown_blocks_new_mutations_but_keeps_receipt_queries_available(tmp_path):
    settings=MachineSettings(tmp_path/'machine');settings.save(library_location=str(tmp_path/'library'))
    app=create_app(settings,FakeProvider(),nonce='test',start_worker=False)
    with TestClient(app) as client:
        client.headers['X-AutoReview-Session']='test'
        task=client.post('/api/entities/tasks',json={'name':'退出边界'}).json()
        assert client.post('/api/shutdown').status_code==200
        response=client.post('/api/entities/tasks',json={'name':'不得在退出时创建'})
        assert response.status_code==409
        assert response.json()['code']=='APPLICATION_CLOSING'
        assert client.post('/api/settings',json={'parallelism':2}).status_code==409
        assert client.get('/api/tasks/'+task['id']).status_code==200
        assert client.post('/api/shutdown').status_code==200
        with app.state.service.store.connection() as db:
            assert len(app.state.service.store.list(db,'tasks'))==1
