import hashlib
import io
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest
from PIL import Image
from fastapi.testclient import TestClient
from autoreview.v2 import imaging
from autoreview.v2.api import create_app
from autoreview.v2.core import Problem, uid
from autoreview.v2.image_jobs import enqueue, process, recover
from test_workflow import service, setup, intake, FakeProvider


@pytest.mark.parametrize('operations', [
    [],
    [{'kind':'rotate','angle':2.7}, {'kind':'crop','box':{'x1':.05,'y1':.07,'x2':.93,'y2':.95}}],
    [{'kind':'perspective','points':[[.04,.09],[.92,.03],[.97,.94],[.08,.91]]},
     {'kind':'crop','box':{'x1':.08,'y1':.08,'x2':.92,'y2':.93}}, {'kind':'rotate','angle':-90}],
])
def test_exact_quarter_rotation_preserves_corrected_pixels_geometry_and_history(service,operations,monkeypatch):
    rng=np.random.default_rng(72)
    pixels=rng.integers(0,256,(179,241,3),dtype=np.uint8)
    source=Image.fromarray(pixels);buffer=io.BytesIO();source.save(buffer,'PNG')
    monkeypatch.setattr(imaging,'auto_operations',lambda image,**kwargs:([],[]))
    asset=service.save_asset(buffer.getvalue(),'corners.png')
    original_sha=hashlib.sha256(service.asset(asset['id'],'original').read_bytes()).hexdigest()
    for operation in operations:
        asset=service.prepare_asset(asset['id'],{'operation':operation})
    before=np.array(Image.open(service.asset(asset['id'])))
    turned=service.prepare_asset(asset['id'],{'operation':{'kind':'rotate','angle':90}})
    assert np.array_equal(np.array(Image.open(service.asset(turned['id']))),np.rot90(before))
    replay,replay_geometry=imaging.render(source,turned['operations'])
    assert np.array_equal(np.array(replay),np.array(Image.open(service.asset(turned['id']))))
    assert np.allclose(turned['geometry']['forward'],replay_geometry['forward'],atol=1e-12)
    points=[[.12,.16],[.71,.84]]
    assert np.allclose(imaging.project(imaging.project(points,turned['geometry']['forward']),turned['geometry']['inverse']),points)
    # A second exact turn returns every edge pixel; the original and prior asset
    # are immutable and the complete operation chain remains auditable.
    restored=service.prepare_asset(turned['id'],{'operation':{'kind':'rotate','angle':-90}})
    assert np.array_equal(np.array(Image.open(service.asset(restored['id']))),before)
    assert np.allclose(restored['geometry']['forward'],asset['geometry']['forward'])
    assert hashlib.sha256(service.asset(restored['id'],'original').read_bytes()).hexdigest()==original_sha
    assert len(restored['operations'])==len(asset['operations'])+2
    assert len(restored['operation_log'])==len(asset['operation_log'])+2
    monkeypatch.setattr(imaging,'render',lambda *args:pytest.fail('exact quick turn must not replay correction'))
    service.prepare_asset(restored['id'],{'operation':{'kind':'rotate','angle':180}})


def test_legacy_quarter_rotation_semantics_are_not_silently_rewritten():
    source=Image.new('RGB',(51,39),'red')
    legacy,geometry=imaging.render(source,[{'kind':'rotate','angle':90}])
    # The historical boundary-shift remains only in the explicitly legacy chain.
    assert np.any(np.all(np.array(legacy)==255,axis=2))
    exact,_=imaging.render(source,[{'kind':'rotate','angle':90,'pixel_preserving':True}])
    assert np.array_equal(np.array(exact),np.rot90(np.array(source)))
    assert geometry['size']==[39,51]


def test_batch_stages_identify_current_paper_and_atomic_failure(service,monkeypatch):
    task,groups,_=setup(service,2);intake(service,groups[0],2)
    pages=service.detail(task['id'])['pages'];original=service.prepare_asset;stages=[];calls=0
    def checked(identifier,data,status=None):
        nonlocal calls
        calls+=1
        if calls==2:raise Problem('TEST_WRITE','测试保存失败')
        return original(identifier,data,status)
    monkeypatch.setattr(service,'prepare_asset',checked)
    request={'request_id':uid(),'angle':90,'pages':[{'id':p['id'],'revision':p['revision']} for p in pages]}
    job,_=enqueue(service,request)
    with pytest.raises(Problem):
        service.batch_images(request,status=lambda **s:stages.append(s))
    assert {'reading','processing','saving'} <= {s['stage'] for s in stages}
    assert stages[0]['current_page_id']==pages[0]['id']
    assert '第1页' in stages[0]['current_label']
    assert [p['image_version_id'] for p in service.detail(task['id'])['pages']]==[p['image_version_id'] for p in pages]
    done=process(service,job['id'])
    assert done['state']=='completed' and done['stage']=='completed'
    assert done['completed_count']==done['total_count']==2 and done['finished_at']
    with service.store.connection(write=True) as db:
        service.store.put(db,'local_image_jobs',{**done,'state':'running','stage':'applying','completed_count':0})
    recover(service)
    with service.store.connection() as db:
        recovered=service.store.get(db,'local_image_jobs',done['id'])
    assert recovered['state']=='completed' and recovered['completed_count']==2
    assert recovered['finished_at'] and recovered['current_page_id'] is None


def test_position_preview_job_is_queryable_and_retains_completed_previews_on_failure(service,monkeypatch):
    task,groups,_=setup(service,2);intake(service,groups[0],2)
    pages=service.detail(task['id'])['pages'];original=service.prepare_page;calls=0
    def checked(identifier,data,*,job_id=None):
        nonlocal calls
        calls+=1
        if calls==2:raise Problem('TEST_ALIGN','第二张定位失败')
        return original(identifier,data,job_id=job_id)
    monkeypatch.setattr(service,'prepare_page',checked)
    job,_=enqueue(service,{'request_id':uid(),'action':'prepare','pages':[{'id':p['id'],'revision':p['revision']} for p in pages]})
    done=process(service,job['id'])
    assert done['state']=='failed' and done['completed_count']==1
    assert done['current_page_id']==pages[1]['id'] and '第2页' in done['current_label']
    assert service.detail(task['id'])['pages'][0]['preparation']
    assert not service.detail(task['id'])['pages'][1].get('preparation')
    assert [p['asset_id'] for p in service.detail(task['id'])['pages']]==[p['asset_id'] for p in pages]
    assert enqueue(service,{'request_id':uid(),'action':'prepare','pages':[{'id':p['id'],'revision':p['revision']} for p in service.detail(task['id'])['pages']]})[1]


def test_slow_position_job_keeps_http_status_and_other_requests_responsive(service,monkeypatch):
    service.close()
    app=create_app(service.settings,FakeProvider(),nonce='test-secret',start_worker=False)
    local=app.state.service;task,groups,_=setup(local,2);intake(local,groups[0],2)
    pages=local.detail(task['id'])['pages'];original=local.prepare_page
    def slow(identifier,data,*,job_id=None):
        time.sleep(.3);return original(identifier,data,job_id=job_id)
    monkeypatch.setattr(local,'prepare_page',slow)
    with TestClient(app) as client:
        client.headers['X-AutoReview-Session']='test-secret'
        started=time.monotonic()
        job=client.post('/api/images/jobs',json={'request_id':uid(),'action':'prepare','pages':[{'id':p['id'],'revision':p['revision']} for p in pages]}).json()
        assert time.monotonic()-started<.4
        t=time.monotonic();assert client.get('/api/state').status_code==200;assert time.monotonic()-t<.3
        deadline=time.monotonic()+6;labels=set()
        while job['state'] in ('queued','running') and time.monotonic()<deadline:
            if job.get('current_label'):labels.add(job['current_label'])
            time.sleep(.02);job=client.get('/api/images/jobs/'+job['id']).json()
        assert job['state']=='completed' and job['completed_count']==2
        assert len(labels)==2


def test_landmark_cache_reuses_files_but_respects_mask_and_file_changes(tmp_path):
    rng=np.random.default_rng(32)
    source=Image.fromarray(rng.integers(0,256,(240,320,3),dtype=np.uint8))
    a=tmp_path/'template.png';b=tmp_path/'student.png';source.save(a);source.save(b)
    imaging._landmarks.cache_clear()
    first=imaging.align(a,b,[]);stats=imaging._landmarks.cache_info()
    second=imaging.align(a,b,[])
    assert np.allclose(first['matrix'],second['matrix'])
    assert imaging._landmarks.cache_info().hits==stats.hits+2
    imaging.align(a,b,[{'x1':.1,'y1':.1,'x2':.2,'y2':.2}])
    assert imaging._landmarks.cache_info().misses==stats.misses+1
    source.transpose(Image.Transpose.ROTATE_180).save(b)
    imaging.align(a,b,[])
    assert imaging._landmarks.cache_info().misses==stats.misses+2
