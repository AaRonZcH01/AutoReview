from __future__ import annotations
import asyncio
import io
import json
import os
import secrets
import sqlite3
import hashlib
import shutil
import tempfile
import zipfile
from contextlib import asynccontextmanager, closing
from pathlib import Path
from fastapi import FastAPI, Request, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from .core import MachineSettings, Store, Problem, require, now, uid, dumps, atomic_write
from .service import Service
from .provider import Provider
from .runner import Runner
from .reports import statistics, metrics, annotate, export_report, export_diagnostics
from .contracts import MatchingRule, MatchingEdge
from .grading import grade_matching


def create_app(settings=None, provider=None, nonce=None, start_worker=True, static_dir=None, manage_lifetime=False, parent_pid=None):
    settings = settings or MachineSettings()
    service = Service(settings)
    provider = provider or Provider(settings)
    runner = Runner(service, provider)
    secret = nonce or secrets.token_urlsafe(32)
    from .lifetime import UiLease, watch
    instance_id = uid()
    cookie_name = 'autoreview_' + hashlib.sha256(secret.encode()).hexdigest()[:16]
    diagnostic_jobs=set()
    def lifecycle(event, **fields):
        def record():
            service.diagnostics.record('LIFECYCLE',event,level='info',component='lifetime',instance_id=instance_id,pid=os.getpid(),**fields)
        try:
            loop=asyncio.get_running_loop()
        except RuntimeError:
            record();return
        job=loop.run_in_executor(None,record);diagnostic_jobs.add(job);job.add_done_callback(diagnostic_jobs.discard)
    lease = UiLease(record=lifecycle)
    @asynccontextmanager
    async def lifespan(app):
        from .power import power
        power.register()
        lifecycle('startup', native_parent=bool(parent_pid))
        from .image_jobs import recover
        await asyncio.to_thread(recover,service)
        from .review_jobs import recover as recover_reviews
        await asyncio.to_thread(recover_reviews,service)
        library_id = service.store.manifest['library_id']
        activation = settings.data['activations'].get(library_id)
        await asyncio.to_thread(runner.recover, allow=activation == str(service.store.root) and not os.environ.get('AUTOREVIEW_RECOVERY_PAUSED'))
        settings.save(activations={**settings.data['activations'], library_id: str(service.store.root)})
        if start_worker:
            runner.start()
        watcher = asyncio.create_task(watch(app, lease, parent_pid, manage_lifetime)) if manage_lifetime or os.environ.get('AUTOREVIEW_WATCHDOG_FILE') else None
        async def cleanup():
            while True:
                await asyncio.sleep(2 if not getattr(app.state, 'swept_once', False) else 300)
                if not getattr(app.state, 'maintenance', False) and not getattr(app.state, 'shutting_down', False):
                    app.state.swept_once = True
                    try:
                        await maintain(service.trash.sweep)
                    except Exception as error:
                        if isinstance(error, Problem) and error.code in ('RUN_ACTIVE', 'LIBRARY_BUSY'):
                            continue
                        await asyncio.to_thread(service.diagnostics.record,'TRASH_CLEANUP_ERROR',str(error),component='trash')
        cleaner = asyncio.create_task(cleanup()) if start_worker else None
        async def apply_component_when_idle():
            while True:
                await asyncio.sleep(1)
                if not getattr(app.state,'shutting_down',False):
                    await cli_updater.apply_ready()
        component_watcher = asyncio.create_task(apply_component_when_idle()) if start_worker else None
        try:
            yield
        finally:
            if component_watcher:
                component_watcher.cancel()
                await asyncio.gather(component_watcher,return_exceptions=True)
            await cli_updater.close()
            if cleaner:
                cleaner.cancel()
                await asyncio.gather(cleaner, return_exceptions=True)
            if watcher:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            if image_tasks:
                await asyncio.gather(*image_tasks,return_exceptions=True)
            if review_tasks:
                await asyncio.gather(*review_tasks,return_exceptions=True)
            await runner.close()
            await provider.close()
            lifecycle('shutdown_completed')
            if diagnostic_jobs: await asyncio.gather(*diagnostic_jobs,return_exceptions=True)
            power.close()
            service.close()
    app = FastAPI(title='AutoReview', version='1.4.2', lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.service, app.state.runner, app.state.nonce = service, runner, secret
    backup_directories = set()
    app.state.data_users = 0
    @asynccontextmanager
    async def cli_switch_gate():
        library_idle()
        require(not getattr(provider,'pending_profiles',{}), 'LOGIN_PENDING', '请完成或取消登录后更新组件。',409)
        app.state.maintenance = runner.maintenance = app.state.cli_switching = True
        try:
            yield
        finally:
            app.state.maintenance = runner.maintenance = app.state.cli_switching = False

    def cli_idle():
        library_idle()
        require(not getattr(provider,'pending_profiles',{}) and not getattr(provider,'active_turns',{}), 'CLI_BUSY', '组件正在使用中，等待空闲。',409)

    from .cli_updates import CliUpdater
    cli_updater = CliUpdater(settings, provider, cli_idle, cli_switch_gate)
    app.state.cli_updater = cli_updater
    image_tasks=set()
    review_tasks=set()

    async def data_work(fn, *args):
        require(not getattr(app.state, 'maintenance', False), 'LIBRARY_MAINTENANCE', '资料库维护中，请稍候。', 409)
        app.state.data_users += 1
        pending = asyncio.create_task(asyncio.to_thread(fn, *args))
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            await pending
            raise
        finally:
            app.state.data_users -= 1

    async def maintain(fn, *args):
        library_idle()
        app.state.maintenance = runner.maintenance = True
        pending = asyncio.create_task(asyncio.to_thread(fn, *args))
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            await pending
            raise
        finally:
            app.state.maintenance = runner.maintenance = False


    @app.middleware('http')
    async def local_only(request, call_next):
        host = request.headers.get('host', '').split(':')[0]
        if host not in ('127.0.0.1', 'localhost', '[::1]', 'testserver'):
            return JSONResponse({'code': 'HOST_REJECTED', 'message': '仅允许本机访问。'}, status_code=403)
        if request.url.path.startswith('/api'):
            origin = request.headers.get('origin')
            if origin and origin not in (f'http://{request.headers.get("host")}', f'https://{request.headers.get("host")}'):
                return JSONResponse({'code': 'ORIGIN_REJECTED', 'message': '来源无效。'}, status_code=403)
            token = request.headers.get('x-autoreview-session') or request.cookies.get(cookie_name) or ''
            if not secrets.compare_digest(token, secret):
                return JSONResponse({'code': 'SESSION_REQUIRED', 'message': '请通过应用启动器打开。'}, status_code=401)
            if request.method not in ('GET','HEAD'):
                expected_instance=request.headers.get('x-autoreview-instance')
                expected_library=request.headers.get('x-autoreview-library')
                if (expected_instance and expected_instance != instance_id) or (expected_library and expected_library != service.store.manifest['library_id']):
                    return JSONResponse({'code':'CONTEXT_CHANGED','message':'本地服务或资料库已改变，已阻止旧窗口写入。请重新打开当前入口；本机恢复稿仍保留。'},status_code=409)
            if getattr(app.state, 'maintenance', False) and request.method not in ('GET', 'HEAD') and request.url.path != '/api/shutdown':
                return JSONResponse({'code': 'LIBRARY_MAINTENANCE', 'message': '资料库维护中，请稍候。'}, status_code=409)
            if getattr(app.state, 'shutting_down', False) and request.method not in ('GET', 'HEAD') and request.url.path not in ('/api/shutdown', '/api/session'):
                return JSONResponse({'code': 'APPLICATION_CLOSING', 'message': '程序正在退出，已阻止新的操作。已保存的照片、草稿和处理记录会保留。'}, status_code=409)
            if getattr(app.state, 'restart_required', False) and request.method not in ('GET', 'HEAD') and request.url.path not in ('/api/shutdown', '/api/session'):
                return JSONResponse({'code': 'RESTART_REQUIRED', 'message': '资料库位置已变更，请重启后继续操作。'}, status_code=409)
        request.state.request_id = request.headers.get('x-request-id', '')[:80] or uid()
        if request.url.path.startswith('/api') and not request.url.path.startswith('/api/assets/'):
            lease.touch(request.headers.get('x-autoreview-tab'))
        response = await call_next(request)
        response.headers['X-Request-ID'] = request.state.request_id
        response.headers['X-AutoReview-Instance'] = instance_id
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        if 'Cache-Control' not in response.headers:
            response.headers['Cache-Control'] = 'no-store'
        response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; connect-src 'self'; script-src 'self'; frame-ancestors 'none'"
        return response

    @app.exception_handler(Problem)
    async def problem(request, error):
        # Diagnostic disk/database writes must not block recovery requests or
        # the lifecycle heartbeat while another review transaction is saving.
        await asyncio.to_thread(service.diagnostics.record,error.code,error.message,
                                level='info' if error.status < 500 else 'error',
                                component='http',method=request.method,route=request.url.path)
        return JSONResponse({'code': error.code, 'message': error.message, 'details': error.details, 'request_id': getattr(request.state, 'request_id', None), 'instance_id': instance_id}, status_code=error.status)

    @app.exception_handler(ValidationError)
    async def validation(request, error):
        return JSONResponse({'code': 'VALIDATION', 'message': '字段格式不正确。', 'details': str(error)}, status_code=422)

    @app.exception_handler(Exception)
    async def unexpected(request, error):
        trace = uid()
        await asyncio.to_thread(service.diagnostics.record,'UNEXPECTED_ERROR',str(error) or type(error).__name__,
                                component='http',trace_id=trace,route=request.url.path)
        return JSONResponse({'code': 'UNEXPECTED_ERROR', 'message': '操作未完成，详情已记录在日志。', 'trace_id': trace}, status_code=500)

    @app.post('/api/session')
    def session():
        response = JSONResponse({'ok': True, 'instance_id': instance_id})
        response.set_cookie(cookie_name, secret, httponly=True, samesite='strict')
        return response

    @app.websocket('/api/lifetime')
    async def lifetime(socket: WebSocket):
        origin = socket.headers.get('origin')
        host = socket.headers.get('host', '')
        if origin != 'http://' + host or not secrets.compare_digest(socket.cookies.get(cookie_name, ''), secret):
            await socket.close(code=1008)
            return
        await socket.accept()
        identifier = lease.join(socket.query_params.get('tab', '')[:80] or None)
        disconnect_reason = 'ui_closed'
        try:
            while True:
                await socket.send_text('ping')
                try:
                    await asyncio.wait_for(socket.receive_text(), timeout=90)
                    lease.touch(lease.tabs[identifier])
                except TimeoutError:
                    if lease.recently_active(identifier):
                        lifecycle('ui_transport_waiting_http_active')
                        continue
                    raise
                await asyncio.sleep(10)
        except WebSocketDisconnect as error:
            disconnect_reason = 'ui_closed' if error.code in (1000, 1001, 1005) else 'ui_connection_lost'
        except TimeoutError:
            disconnect_reason = 'ui_heartbeat_timeout'
        except RuntimeError:
            disconnect_reason = 'ui_connection_lost'
        finally:
            lease.leave(identifier, disconnect_reason)

    @app.get('/api/state')
    async def state():
        return {**await data_work(service.snapshot),'instance_id':instance_id}

    @app.get('/api/entities/{table}/{identifier}/trash-preview')
    def move_preview(table: str, identifier: str):
        from datetime import datetime, timedelta, timezone
        from .trash import KINDS
        require(table in KINDS, 'INVALID_ENTITY', '此对象不能移入回收站。')
        with service.store.connection() as db:
            service.require_available(db, table, identifier)
            return {**service.trash.describe(db, table, identifier),
                    'expires_at': (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()}

    @app.post('/api/trash/preview')
    def trash_batch_preview(data: dict):
        return service.trash.preview_many(data.get('ids', []))

    @app.post('/api/trash/purge')
    async def trash_batch_purge(data: dict):
        require(data.get('confirm'), 'CONFIRM_REQUIRED', '请确认彻底删除，无法撤销。')
        return await maintain(service.trash.purge_many, data.get('ids', []))

    @app.get('/api/trash')
    def trash():
        return service.trash.rows()

    @app.get('/api/trash/{entry_id}/preview')
    def trash_preview(entry_id: str):
        return service.trash.preview(entry_id)

    @app.post('/api/trash/{entry_id}/restore')
    def trash_restore(entry_id: str, data: dict):
        return service.trash.restore(entry_id, data.get('destination'))

    @app.post('/api/trash/{entry_id}/purge')
    async def trash_purge(entry_id: str, data: dict):
        require(data.get('confirm'), 'CONFIRM_REQUIRED', '请确认彻底删除，无法撤销。')
        return await maintain(service.trash.purge, entry_id)

    @app.post('/api/tasks/new')
    def create_task(data: dict):
        return service.create_task(data)

    @app.post('/api/tasks/{task_id}/groups')
    def add_groups(task_id: str, data: dict):
        return service.add_groups(task_id, data)

    @app.get('/api/tasks/{task_id}/intake-draft')
    def read_intake_draft(task_id: str):
        return service.intake_draft(task_id)

    @app.put('/api/tasks/{task_id}/intake-draft')
    def save_intake_draft(task_id: str, data: dict):
        return service.intake_draft(task_id, data)

    @app.get('/api/camera-profiles')
    def camera_profiles_read():
        return settings.data.get('camera_profiles', {})

    @app.post('/api/camera-profiles')
    def camera_profiles_update(data: dict):
        from .camera_profiles import update
        return update(settings, data)

    @app.post('/api/settings')
    def settings_update(data: dict):
        allowed = {k: v for k, v in data.items() if k in ('export_directory', 'last_import_directory',
            'last_intake_mode', 'onboarding_step', 'onboarding_complete', 'model', 'effort', 'speed',
            'reference_model', 'reference_effort', 'gallery_mode', 'parallelism', 'capture_parallelism', 'conservative_review', 'verification_effort', 'auto_orient_images', 'capture_countdown_seconds', 'cli_check_on_startup')}
        from .execution_config import validate_settings
        validate_settings(allowed)
        for key in ('export_directory', 'last_import_directory'):
            if allowed.get(key):
                path = Path(allowed[key]).expanduser().resolve()
                path.mkdir(parents=True, exist_ok=True)
                probe = path / ('.autoreview-' + uid())
                atomic_write(probe, b'check')
                probe.unlink()
                allowed[key] = str(path)
        return settings.save(**allowed)

    @app.post('/api/shutdown')
    async def shutdown(reason: str = 'application_exit'):
        if getattr(app.state, 'shutting_down', False):
            return {'ok': True}
        lifecycle('shutdown_requested', reason=reason)
        app.state.shutting_down = True
        runner.closing = True
        from .lifetime import pulse
        loop=asyncio.get_running_loop()
        # Once exit has been accepted, cleanup failure must never strand the
        # application in shutting_down=True without scheduling server shutdown.
        fallback=loop.call_later(8,app.state.shutdown) if hasattr(app.state,'shutdown') else None
        try:
            try:
                pulse('closing')
            except OSError:
                lifecycle('shutdown_heartbeat_unavailable')
            def active_runs():
                # A writer barrier includes an enqueue that passed its closing
                # check before exit was requested but has not committed yet.
                with service.store.connection(write=True) as db:
                    return [r for r in service.store.list(db,'runs') if r['state'] in ('queued','running')
                            or r['state']=='paused' and runner.has_inflight(db,r['id'])]
            async with asyncio.timeout(6):
                for run in await asyncio.to_thread(active_runs):
                    await runner.offload(runner.stop,run['id'],reason=reason,interrupt=False)
                await asyncio.wait_for(runner.interrupt_active(), 2)
        except Exception as error:
            lifecycle('shutdown_cleanup_incomplete',error_type=type(error).__name__)
        finally:
            if fallback:fallback.cancel()
            if hasattr(app.state,'shutdown'):loop.call_later(.2,app.state.shutdown)
        return {'ok': True}

    app.state.request_shutdown = shutdown

    @app.post('/api/entities/{table}')
    def create(table: str, data: dict):
        return service.create(table, data)

    @app.patch('/api/entities/{table}/{identifier}')
    async def edit(table: str, identifier: str, data: dict):
        return await maintain(service.edit, table, identifier, data) if data.get('state') == 'archived' or (table == 'folders' and data.get('state') == 'active') else await data_work(service.edit, table, identifier, data)

    @app.get('/api/tasks/{task_id}')
    async def detail(task_id: str, known_revision: int | None = None, known_token: str | None = None):
        if known_revision is not None and known_token is None:
            return await data_work(service.detail,task_id,known_revision)
        from .read_cache import detail as cached_detail
        return await data_work(cached_detail,service,task_id,known_token)

    @app.get('/api/tasks/{task_id}/scoring')
    async def scoring_get(task_id: str):
        from .scoring import manage
        return await data_work(manage, service, task_id)

    @app.post('/api/tasks/{task_id}/scoring')
    async def scoring_post(task_id: str, data: dict):
        from .scoring import manage
        return await data_work(manage, service, task_id, data)

    @app.post('/api/tasks/{task_id}/scoring/check')
    async def scoring_check(task_id: str, data: dict):
        from .scoring import check
        return await data_work(check,service,task_id,data)

    @app.post('/api/tasks/{task_id}/versions/{version_id}')
    async def versions_manage(task_id: str, version_id: str, data: dict):
        from .reference_versions import manage
        return await data_work(manage, service, task_id, version_id, data)

    @app.put('/api/tasks/{task_id}/reference')
    async def draft(task_id: str, data: dict):
        return await data_work(service.draft, task_id, data)

    @app.post('/api/tasks/{task_id}/reference/export')
    async def reference_export(task_id: str, data: dict):
        from .reference_pack import export_pack
        return await data_work(export_pack, service, task_id, data.get('path'))

    @app.post('/api/tasks/{task_id}/reference/import')
    async def reference_import(task_id: str, file: UploadFile = File(), preview: bool = False):
        from .reference_pack import import_pack
        return await data_work(import_pack, service, task_id, await file.read(), preview)

    @app.post('/api/tasks/{task_id}/reference/validate')
    async def reference_validate(task_id: str, data: dict):
        from .reference_review import issues
        return {'validation_issues': await data_work(issues, data), 'sequence': data.get('sequence')}

    @app.get('/api/tasks/{task_id}/reference/operations/{request_id}')
    async def reference_operation(task_id: str, request_id: str, operation: str = "review"):
        require(operation in ("review","save"), "OPERATION", "不支持此操作查询。")
        with service.store.connection() as db:
            row = db.execute('SELECT response FROM idempotency WHERE key=?', (operation+':'+task_id+':'+request_id,)).fetchone()
        stored = json.loads(row[0]) if row else None
        value = await data_work(service.draft, task_id) if stored and stored.get('operation') in ('save','review') else stored
        return {'found': bool(row), 'result': value, 'committed_revision':stored.get('committed_revision') if stored else None}

    @app.post('/api/tasks/{task_id}/reference/review')
    async def reference_review(task_id: str, data: dict):
        from .reference_review import review
        return await data_work(review, service, task_id, data)

    @app.post('/api/tasks/{task_id}/reference/confirm')
    async def confirm(task_id: str, data: dict):
        return await data_work(service.confirm_reference, task_id, data['revision'])

    @app.post('/api/assets')
    async def upload(file: UploadFile = File()):
        return await data_work(service.save_asset, await file.read(40 * 1024 * 1024 + 1), file.filename or 'image')

    @app.get('/api/assets/{asset_id}')
    def asset_info(asset_id: str):
        with service.store.connection() as db:
            return service.store.get(db, 'assets', asset_id)

    @app.post('/api/assets/{asset_id}/prepare')
    async def asset_prepare(asset_id: str, data: dict):
        return await data_work(service.prepare_asset, asset_id, data)

    @app.post('/api/tasks/{task_id}/preparation/batch')
    async def preparation_batch(task_id: str, data: dict):
        from .preparation_batch import batch
        return await data_work(batch, service, task_id, data)

    @app.post('/api/images/batch-prepare')
    async def batch_prepare(data: dict):
        return await data_work(service.batch_images, data)

    @app.post('/api/images/jobs')
    async def image_job_start(data: dict):
        from .image_jobs import enqueue, process
        job,created=await data_work(enqueue,service,data)
        if created:
            task=asyncio.create_task(data_work(process,service,job['id']))
            image_tasks.add(task);task.add_done_callback(image_tasks.discard)
        return job

    @app.get('/api/images/jobs/{identifier}')
    async def image_job_status(identifier: str):
        def status():
            with service.store.connection() as db: return service.store.get(db,'local_image_jobs',identifier)
        return await data_work(status)

    @app.get('/api/tasks/{task_id}/image-jobs')
    async def image_job_list(task_id: str):
        def listing():
            with service.store.connection() as db: return service.store.list(db,'local_image_jobs',task_id=task_id)
        return await data_work(listing)

    @app.post('/api/pages/{page_id}/prepare')
    async def page_prepare(page_id: str, data: dict):
        return await data_work(service.prepare_page, page_id, data)

    @app.get('/api/assets/{asset_id}/{variant}')
    async def asset(asset_id: str, variant: str):
        path=await data_work(service.asset,asset_id,variant)
        return FileResponse(path,headers={'Cache-Control':'private, max-age=31536000, immutable'})

    @app.post('/api/groups/{group_id}/intake')
    def intake(group_id: str, data: dict):
        return service.intake(group_id, data)

    @app.post('/api/pages/{page_id}/replace')
    async def replace(page_id: str, data: dict):
        # The durable generation fence is committed before best-effort remote interruption.
        result = await data_work(service.replace_page, page_id, data)
        if not data.get('preview'):
            import asyncio
            try:
                await asyncio.wait_for(runner.interrupt_active(work_ids=set(result.get('interrupted_work_ids', []))), 3)
            except asyncio.TimeoutError:
                pass  # Durable generations already reject late replies.
        return result

    @app.post('/api/submissions/{submission_id}/review')
    async def submission_review(submission_id: str, data: dict):
        from .submission_review import review
        return await data_work(review, service, submission_id, data)

    @app.get('/api/submissions/{submission_id}/review-state')
    async def submission_review_state(submission_id: str):
        from .submission_review import state
        return await data_work(state, service, submission_id)

    @app.post('/api/pages/{page_id}/review')
    async def review(page_id: str, data: dict):
        return await data_work(service.review, page_id, data)

    @app.post('/api/tasks/{task_id}/batch-review')
    async def batch_review(task_id: str, data: dict):
        from .batch_review import batch
        return await data_work(batch,service,task_id,data)

    async def start_review_job(task_id, data, history_action=False):
        from .review_jobs import enqueue, process
        job, created = await data_work(enqueue, service, task_id, data, history_action)
        if created:
            pending = asyncio.create_task(data_work(process, service, job['id']))
            review_tasks.add(pending)
            pending.add_done_callback(review_tasks.discard)
        return job

    @app.post('/api/tasks/{task_id}/batch-review-jobs')
    async def enqueue_review_job(task_id: str, data: dict):
        return await start_review_job(task_id, data)

    @app.get('/api/tasks/{task_id}/batch-review-jobs/{job_id}')
    async def get_review_job(task_id: str, job_id: str):
        from .review_jobs import status
        return await data_work(status, service, task_id, job_id)

    @app.get('/api/tasks/{task_id}/batch-review-history')
    async def get_review_history(task_id: str):
        from .review_jobs import history
        return await data_work(history, service, task_id)

    @app.post('/api/tasks/{task_id}/batch-review-history')
    async def apply_review_history(task_id: str, data: dict):
        return await start_review_job(task_id, data, True)

    @app.post('/api/pages/{page_id}/retry-region')
    async def retry_region(page_id: str, data: dict):
        require(data.get('cloud_consent'), 'CLOUD_CONSENT', '局部复看将重新发送图片并使用当前账户额度。')
        from .execution_config import freeze
        config, _ = await provider.validate(freeze(settings.data))
        return await data_work(runner.retry_region,page_id,data,config)

    @app.get('/api/pages/{page_id}/annotated')
    async def annotated(page_id: str, r: int | None = None, thumbnail: bool = False):
        def render():
            with service.store.connection() as db:
                page=service.store.get(db,'pages',page_id)
            require(r is None or r==page['revision'],'REVISION_CONFLICT','作业复核已变化，请读取当前图片。',409)
            key=hashlib.sha256(dumps([page['id'],page['image_version_id'],page['revision'],page.get('reference_version_id'),thumbnail]).encode()).hexdigest()
            folder=(service.store.root/'cache'/'annotated'/page['id']).resolve()
            require(folder.parent==(service.store.root/'cache'/'annotated').resolve(),'INVALID_CACHE_PATH','缓存路径无效。')
            folder.mkdir(parents=True,exist_ok=True)
            path=folder/(key+'.png')
            if not path.exists():
                image=annotate(service,page_id,None,page['revision'])
                try:
                    if thumbnail: image.thumbnail((460,460))
                    output=io.BytesIO();image.save(output,'PNG',compress_level=2)
                    atomic_write(path,output.getvalue())
                finally: image.close()
            return path
        path=await data_work(render)
        return FileResponse(path,media_type='image/png',headers={'Cache-Control':'private, max-age=31536000, immutable' if r is not None else 'no-store'})

    @app.post('/api/tasks/{task_id}/runs')
    async def start(task_id: str, data: dict):
        profile = settings.data.get('active_profile_id')
        purpose = data.get('purpose', 'student')
        from .execution_config import freeze
        config = freeze(settings.data, data, purpose)
        account = None
        if data.get('reuse_transcription'):
            config['service_tier'] = None
        else:
            require(data.get('cloud_consent'), 'CLOUD_CONSENT', '需确认发送图片到当前账户。')
            config, account = await provider.validate(config)
        return await data_work(runner.create,task_id,data,config,account)

    @app.get('/api/tasks/{task_id}/capture-sessions')
    def capture_sessions(task_id: str):
        from .stream_capture import list_sessions
        return list_sessions(service, task_id)

    @app.post('/api/tasks/{task_id}/capture-sessions')
    async def capture_start(task_id: str, data: dict):
        from .execution_config import freeze
        from .stream_capture import start as start_capture
        config, account = {}, None
        if data.get('delivery_mode', 'immediate') != 'local':
            require(data.get('cloud_consent'), 'CLOUD_CONSENT', '请确认核对后的照片会发送到当前账户。')
            config, account = await provider.validate(freeze(settings.data, purpose='capture'))
        return await data_work(start_capture, service, task_id, data, config, account,
            lambda: require(not runner.closing, 'APPLICATION_CLOSING', '程序正在退出，请下次打开后开始采集。', 409))

    @app.get('/api/capture-sessions/{session_id}')
    def capture_read(session_id: str):
        from .stream_capture import read
        return read(service, session_id)

    @app.post('/api/capture-sessions/{session_id}/frames')
    async def capture_stage(session_id: str, data: dict):
        from .stream_capture import stage
        return await data_work(stage, service, session_id, data)

    @app.get('/api/capture-frames/{frame_id}')
    def capture_frame(frame_id: str):
        from .stream_capture import frame
        return frame(service, frame_id)

    @app.put('/api/capture-frames/{frame_id}')
    async def capture_edit(frame_id: str, data: dict):
        from .stream_capture import edit
        return await data_work(edit, service, frame_id, data)

    @app.post('/api/capture-frames/{frame_id}/commit')
    async def capture_commit(frame_id: str, data: dict):
        from .stream_capture import commit
        return await data_work(commit, service, frame_id, data, runner.enqueue_capture)

    @app.post('/api/capture-frames/{frame_id}/recover')
    async def capture_recover(frame_id: str, data: dict):
        from .execution_config import freeze
        from .stream_capture import recover_draft
        config, account = {}, None
        if data.get('delivery_mode', 'immediate') != 'local':
            require(data.get('cloud_consent'), 'CLOUD_CONSENT', '请确认重新核对后的照片会发送到当前账户。')
            config, account = await provider.validate(freeze(settings.data, purpose='capture'))
        require(not runner.closing, 'APPLICATION_CLOSING', '程序正在退出，保留的照片可在下次打开后继续检查。', 409)
        return await data_work(recover_draft, service, frame_id, data, config, account,
            lambda: require(not runner.closing, 'APPLICATION_CLOSING', '程序正在退出，保留的照片可在下次打开后继续检查。', 409))

    @app.post('/api/capture-frames/{frame_id}/discard')
    async def capture_discard(frame_id: str, data: dict):
        from .stream_capture import discard_draft
        return await data_work(discard_draft, service, frame_id, data)

    @app.post('/api/capture-sessions/{session_id}/stop')
    def capture_stop(session_id: str, data: dict):
        from .stream_capture import stop
        return stop(service, session_id, data)

    @app.post('/api/capture-sessions/{session_id}/withdraw-last')
    async def capture_withdraw(session_id: str, data: dict):
        from .stream_capture import withdraw_last
        result = await data_work(withdraw_last, service, session_id, data)
        if not data.get('preview'):
            # Fence the durable page first. Interrupt only its calls/dependencies,
            # never the shared run containing other students' photographs.
            import asyncio
            try:
                await asyncio.wait_for(runner.interrupt_active(work_ids=set(result.get('interrupted_work_ids', []))), 3)
                result['interruption_requested'] = True
            except asyncio.TimeoutError:
                result['interruption_requested'] = False
        return result

    @app.post('/api/pages/{page_id}/capture-remedy')
    async def capture_remedy(page_id: str, data: dict):
        from .capture_remedy import remedy
        if data.get('action') == 'resume' and not data.get('preview'):
            with service.store.connection() as db:
                page = service.store.get(db, 'pages', page_id)
                session = service.store.get(db, 'capture_sessions', page['capture_session_id'])
            if session.get('delivery_mode', 'immediate') != 'local':
                require(data.get('cloud_consent') is True, 'CLOUD_CONSENT', '请确认重新发送图片到原冻结账户。')
                require(session['config']['profile_id'] == settings.data.get('active_profile_id'), 'CAPTURE_ACCOUNT_CHANGED', '请切回原冻结账户再重新提交。')
                await provider.validate(session['config'])
        result = await data_work(remedy, service, runner, page_id, data)
        if not data.get('preview') and data['action'] != 'resume':
            try:
                await asyncio.wait_for(runner.interrupt_active(work_ids=set(result.get('interrupted_work_ids', []))), 3)
                result['interruption_requested'] = True
            except asyncio.TimeoutError:
                result['interruption_requested'] = False
        return result

    @app.post('/api/runs/{run_id}/visibility')
    def run_visibility(run_id: str, data: dict):
        with service.store.connection(write=True) as db:
            run = service.store.get(db, 'runs', run_id)
            require(run['state'] not in ('running', 'queued', 'stopping'), 'RUN_ACTIVE', '执行中的轮次不能隐藏。')
            return service.store.put(db, 'runs', {**run, 'hidden': bool(data.get('hidden'))})

    @app.post('/api/runs/{run_id}/stop')
    async def stop(run_id: str):
        await runner.offload(runner.stop,run_id)
        return {'ok': True}

    @app.post('/api/runs/{run_id}/resume')
    async def resume(run_id: str, data: dict):
        with service.store.connection() as db:
            run = service.store.get(db, 'runs', run_id)
        config = run['config']
        if data.get('use_current_account'):
            config = {**config, 'profile_id': settings.data.get('active_profile_id')}
        if not run.get('reuse_transcription'):
            config, _ = await provider.validate(config)
        return await data_work(runner.resume,run_id,config)

    @app.get('/api/account')
    async def account(refresh_models: bool = False):
        require(not getattr(app.state,'cli_switching',False),'CLI_SWITCHING','正在切换CLI组件，请稍后刷新。',409)
        if not settings.data.get('active_profile_id'):
            return {'account': None, 'models': [], 'rate_limits': None, 'usage': None}
        return await provider.account(refresh_models=refresh_models)

    @app.get('/api/cli-update')
    def cli_update_status():
        return cli_updater.status()

    @app.post('/api/cli-update/check')
    async def cli_update_check(data: dict):
        return await cli_updater.check(data.get('trigger','manual'))

    @app.post('/api/cli-update/confirm')
    async def cli_update_confirm(data: dict):
        return await cli_updater.confirm(data.get('offer_id'),data.get('confirm'))

    @app.post('/api/cli-update/cancel')
    async def cli_update_cancel():
        return await cli_updater.cancel()

    @app.post('/api/account/login')
    async def login(data: dict):
        await asyncio.to_thread(account_idle)
        if data.get('profile_id'):
            return await provider.login(data['profile_id'], data.get('device', False))
        return await provider.add_profile(data.get('name'), data.get('device', False))

    @app.post('/api/account/login-status')
    async def login_status(data: dict):
        await asyncio.to_thread(account_idle)
        return await provider.login_status(data.get('profile_id'))

    @app.post('/api/account/delete')
    async def delete_account(data: dict):
        require(data.get('confirm') is True, 'CONFIRM_REQUIRED', '请先确认删除账户。')
        await asyncio.to_thread(account_idle)
        return await provider.remove_profile(data.get('profile_id'))

    @app.post('/api/account/repair-login')
    async def repair_login(data: dict):
        require(data.get('confirm') is True, 'CONFIRM_REQUIRED', '请先确认清除本账户的失效登录并重新登录。')
        await asyncio.to_thread(account_idle)
        return await provider.repair_login(data.get('profile_id'))

    @app.post('/api/account/clear')
    async def clear_accounts(data: dict):
        require(data.get('confirm') is True, 'CONFIRM_REQUIRED', '请先确认清除本副本全部账户。')
        await asyncio.to_thread(account_idle)
        for profile in list(settings.data['profiles']):
            await provider.remove_profile(profile['id'])
        await provider.clean_pending()
        return {'ok': True}

    @app.post('/api/account/switch')
    async def switch(data: dict):
        await asyncio.to_thread(account_idle)
        require(any(p['id'] == data.get('profile_id') for p in settings.data['profiles']), 'PROFILE_NOT_FOUND', '账户配置不存在。')
        settings.save(active_profile_id=data['profile_id'])
        return await provider.account()

    @app.post('/api/account/open-login')
    def open_login(data: dict):
        from urllib.parse import urlparse
        import webbrowser
        address = provider.login_urls.get(data.get('profile_id'))
        require(address and urlparse(address).scheme == 'https' and
                (urlparse(address).hostname or '').endswith(('.openai.com', '.chatgpt.com')),
                'LOGIN_URL', '请重新发起官方登录。')
        webbrowser.open(address)
        return {'ok': True}

    @app.post('/api/account/logout')
    async def logout():
        await asyncio.to_thread(account_idle)
        profile_id = settings.data.get('active_profile_id')
        if profile_id:
            await provider.forget(profile_id)
            settings.save(active_profile_id=None)
        return {'ok': True}

    def account_idle():
        with service.store.connection() as db:
            runs = service.store.list(db, 'runs')
            captures = service.store.list(db, 'capture_sessions')
        require(not runner.current_run and not any(r['state'] in ('queued','running') for r in runs),
                'RUN_ACTIVE', '请先停止排队或处理中的任务，再切换账户或退出登录。', 409)
        require(not any(s['state'] == 'active' for s in captures), 'CAPTURE_ACTIVE',
                '请先结束连续采集，再切换账户或退出登录。', 409)

    @app.post('/api/account/cancel')
    async def cancel(data: dict):
        return await provider.cancel_login(data.get('profile_id'), data.get('login_id'))

    @app.get('/api/tasks/{task_id}/statistics')
    def stats(task_id: str, group_id: str | None = None):
        from .read_cache import statistics as cached_statistics
        return cached_statistics(service,task_id,group_id)

    @app.get('/api/metrics')
    def performance(task_id: str | None = None):
        return metrics(service, task_id)

    @app.get('/api/logs')
    def logs(code: str | None = None, task_id: str | None = None):
        with service.store.connection() as db:
            events = [dict(r) for r in db.execute('SELECT * FROM events WHERE (? IS NULL OR task_id=?) ORDER BY event_id DESC LIMIT 500', (task_id, task_id))]
        return {'diagnostics': service.diagnostics.rows(code=code), 'events': events}

    @app.post('/api/logs/export')
    async def log_export(data: dict):
        return await data_work(export_diagnostics, service, data.get('task_id'))

    @app.post('/api/tasks/{task_id}/export')
    async def report_export(task_id: str, data: dict):
        return await data_work(export_report, service, task_id, data)

    @app.post('/api/activity')
    def activity(data: dict):
        with service.store.connection(write=True) as db:
            service.store.get(db, 'tasks', data['task_id'])
            old = service.store.get(db, 'activity_sessions', data['id'], optional=True)
            increment = min(max(float(data.get('active_ms', 0)), 0), 15000)
            if old:
                require(old['task_id'] == data['task_id'], 'ACTIVITY_SCOPE', '活动会话不匹配。')
                # Client sequence deduplicates delayed/retried heartbeats.
                if data.get('sequence', 0) <= old.get('heartbeat_sequence', 0):
                    return old
            return service.store.put(db, 'activity_sessions', {**(old or {}), 'id': data['id'],
                'parent_id': data['task_id'], 'task_id': data['task_id'],
                'active_ms': (old or {}).get('active_ms', 0) + increment,
                'heartbeat_sequence': data.get('sequence', 0), 'measurement': 'foreground_recent_input_estimate'})

    @app.post('/api/submissions/{submission_id}/paper-feedback')
    def paper_feedback(submission_id: str, data: dict):
        require(data.get('state') in ('pending', 'done', 'needs_update'), 'PAPER_FEEDBACK_STATE', '纸面反馈状态无效。')
        with service.store.connection(write=True) as db:
            sub = service.store.get(db, 'submissions', submission_id)
            require(sub['state'] == 'reviewed' or data['state'] != 'done', 'REVIEW_REQUIRED', '请先完成整份复核。')
            result = service.store.put(db, 'submissions', {**sub, 'paper_feedback': data['state'], 'paper_feedback_at': now()})
            service.store.event(db, 'paper_feedback.updated', result, state=data['state'])
            return result

    @app.post('/api/manual-timer')
    def manual_timer(data: dict):
        from .reports import interval_ms
        require(data.get('action') in ('start', 'stop'), 'TIMER_ACTION', '计时操作无效。')
        with service.store.connection(write=True) as db:
            service.store.get(db, 'tasks', data['task_id'])
            timers = [s for s in service.store.list(db, 'activity_sessions', task_id=data['task_id'])
                      if s.get('measurement') == 'paper_manual_timer' and s['state'] == 'running']
            if data['action'] == 'start':
                require(not timers, 'TIMER_RUNNING', '已有纸本处理计时正在运行。')
                timer = service.store.put(db, 'activity_sessions', {'parent_id': data['task_id'], 'task_id': data['task_id'],
                    'measurement': 'paper_manual_timer', 'state': 'running', 'started_at': now(), 'active_ms': 0})
                service.store.event(db, 'timer.started', timer)
                return timer
            require(timers, 'TIMER_NOT_RUNNING', '没有运行中的计时。')
            timer = timers[0]
            ended = now()
            result = service.store.put(db, 'activity_sessions', {**timer, 'state': 'completed', 'ended_at': ended,
                'active_ms': interval_ms(timer['started_at'], ended)})
            service.store.event(db, 'timer.stopped', result, elapsed_ms=result['active_ms'])
            return result

    @app.post('/api/adapters/matching/evaluate')
    def matching(data: dict):
        return grade_matching(MatchingRule.model_validate(data['rule']), [MatchingEdge.model_validate(e) for e in data['edges']])

    @app.get('/api/capture/capabilities')
    def capture():
        return {'local_batch': True, 'camera': False, 'camera_contract_version': 1,
            'frame_contract': ['capture_id', 'asset_id', 'session_id', 'slot', 'captured_at'],
            'reason': '摄像头拍摄接口已预留，v0.2 通过本地图片导入。'}

    @app.post('/api/native/directory')
    async def directory(data: dict):
        def choose():
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes('-topmost', True)
            try:
                return filedialog.askdirectory(initialdir=data.get('initial') or str(Path.home()), parent=root, title=data.get('title') or '选择保存目录')
            finally:
                root.destroy()
        return {'path': await asyncio.to_thread(choose)}

    @app.post('/api/native/save-file')
    async def save_file(data: dict):
        def choose():
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes('-topmost', True)
            try:
                return filedialog.asksaveasfilename(parent=root, title=data.get('title') or '保存范本答案包',
                    initialdir=data.get('initial') or settings.data['export_directory'],
                    initialfile=Path(data.get('filename') or 'AutoReview-reference.zip').name,
                    defaultextension='.zip', filetypes=[('范本答案包 ZIP', '*.zip')], confirmoverwrite=True)
            finally:
                root.destroy()
        return {'path': await asyncio.to_thread(choose)}

    @app.post('/api/native/backup-file')
    async def backup_file():
        def choose():
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes('-topmost', True)
            try:
                return filedialog.askopenfilename(parent=root, title='选择 AutoReview 作业资料备份',
                    initialdir=settings.data['export_directory'], filetypes=[('AutoReview 备份 ZIP', '*.zip')])
            finally:
                root.destroy()
        return {'path': await asyncio.to_thread(choose)}

    @app.post('/api/native/images')
    async def native_images():
        def choose():
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes('-topmost', True)
            try:
                return filedialog.askopenfilenames(parent=root, title='导入本地作业图片',
                    initialdir=settings.data.get('last_import_directory') or str(Path.home()),
                    filetypes=[('图片', '*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff')])
            finally:
                root.destroy()
        chosen = await asyncio.to_thread(choose)
        if chosen:
            settings.save(last_import_directory=str(Path(chosen[0]).parent))
        return {'assets': [await data_work(service.save_asset, Path(p).read_bytes(), Path(p).name) for p in chosen]}

    def library_idle():
        require(not app.state.data_users and not getattr(app.state, 'maintenance', False), 'LIBRARY_BUSY', '正在保存、导出或维护资料，请完成后再试。', 409)
        with service.store.connection() as db:
            active = [r for r in service.store.list(db, 'runs') if r['state'] in ('queued', 'running')]
            captures = [s for s in service.store.list(db, 'capture_sessions') if s['state'] == 'active']
        require(not captures, 'CAPTURE_ACTIVE', '请先结束连续采集，再维护资料库。', 409)
        require(not runner.current_run and not active, 'RUN_ACTIVE', '请先停止所有排队或运行中的处理，再维护资料库。', 409)

    @app.post('/api/library/inspect')
    async def library_inspect(data: dict):
        from .libraries import inspect_library
        return await asyncio.to_thread(inspect_library, data['path'])

    @app.post('/api/library/switch')
    def library_switch(data: dict):
        from .libraries import inspect_library
        from .core import LibraryLock
        library_idle()
        info = inspect_library(data['path'])
        target = Path(info['path'])
        require(target != service.store.root, 'SAME_LIBRARY', '当前已打开这个资料库。')
        lock = LibraryLock(target)
        try:
            settings.save(library_location=str(target), activations={k: v for k, v in settings.data['activations'].items() if k != info['library_id']})
        finally:
            lock.close()
        app.state.restart_required = True
        runner.closing = True
        from .lifetime import pulse
        pulse('closing')
        return {**info, 'restart_required': True, 'auto_resume': False, 'copied': False}

    @app.post('/api/library/create')
    async def library_create(data: dict):
        library_idle()
        parent = Path(data['path']).resolve()
        require(parent.is_dir(), 'LIBRARY_PARENT', '请选择新资料库的保存文件夹。')
        target = parent / ('AutoReview-Library-' + uid()[:8])
        created = Store(target)
        created.close()
        return await asyncio.to_thread(library_switch, {'path': str(target)})

    @app.post('/api/library/merge')
    async def library_merge(data: dict):
        from .libraries import merge_library
        library_idle()
        app.state.maintenance = True
        try:
            return await asyncio.to_thread(merge_library, service, data['path'])
        finally:
            app.state.maintenance = False

    def restore_files(data: dict):
        source = Path(data['archive']).resolve()
        target = Path(data['path']).resolve()
        if data.get('create_subdirectory'):
            require(target.is_dir(), 'RESTORE_PARENT', '请选择恢复资料的保存文件夹。')
            target = target / f'AutoReview-Restored-{now()[:10]}-{uid()[:8]}'
        require(source.is_file() and source.suffix.lower() == '.zip', 'BACKUP_FILE', '请选择有效的完整备份 ZIP。')
        require(zipfile.is_zipfile(source), 'BACKUP_FILE', '所选文件不是有效的 ZIP 备份。')
        require(not target.is_relative_to(service.store.root) and not service.store.root.is_relative_to(target),
                'RESTORE_SCOPE', '恢复目录必须与当前资料库分离。')
        require(not target.exists() or not any(target.iterdir()), 'RESTORE_NOT_EMPTY', '恢复目标必须为空目录。')
        with zipfile.ZipFile(source) as archive:
            require('backup-manifest.json' in archive.namelist(), 'BACKUP_FORMAT', '这不是 AutoReview 创建的作业资料备份。请选择“创建作业资料备份”生成的 ZIP，而非程序安装包。')
            try:
                manifest = json.loads(archive.read('backup-manifest.json'))
                checksums = manifest['checksums']
                require(isinstance(checksums, dict), 'BACKUP_FORMAT', '备份清单格式无效。')
            except (ValueError, KeyError, TypeError):
                raise Problem('BACKUP_FORMAT', '备份清单损坏，无法恢复。')
            require('library.json' in checksums and 'database/autoreview.sqlite' in checksums,
                    'BACKUP_INCOMPLETE', '备份缺少数据库或资料库信息。')
            require(sum(x.file_size for x in archive.infolist()) <= 50 * 1024**3,
                    'BACKUP_SIZE', '备份展开超过50GB，请拆分归档。')
            for relative, expected_hash in checksums.items():
                resolved = (target / relative).resolve()
                require(resolved.is_relative_to(target) and not Path(relative).is_absolute() and
                        (relative.startswith(('assets/', 'attempt-artifacts/', 'legacy-archives/', 'archives/')) or relative in ('library.json', 'database/autoreview.sqlite')),
                        'BACKUP_PATH', '备份包含无效路径。')
                require(relative in archive.namelist(), 'BACKUP_INCOMPLETE', '备份文件不完整，请重新选择备份。')
                content = archive.read(relative)
                require(hashlib.sha256(content).hexdigest() == expected_hash, 'BACKUP_CHECKSUM', '备份校验不通过，未启用。')
            for relative in checksums:
                atomic_write(target / relative, archive.read(relative))
        with closing(sqlite3.connect(target / 'database/autoreview.sqlite')) as database:
            require(database.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', 'BACKUP_DATABASE', '恢复的数据库完整性检查失败。')
        library = json.loads((target / 'library.json').read_text('utf-8'))
        require(library.get('schema_version') in (2,3,4,5), 'LIBRARY_VERSION', '备份资料库版本不兼容，未切换当前资料库。')
        atomic_write(target / 'library.json', dumps({**library, 'library_id': uid(), 'restored_from': library['library_id'],
                    'restored_at': now(), 'auto_resume_allowed': False}).encode())
        settings.save(library_location=str(target))
        app.state.restart_required = True
        runner.closing = True
        from .lifetime import pulse
        pulse('closing')
        return {'path': str(target), 'restart_required': True, 'auto_resume': False, 'credentials_restored': False}

    @app.post('/api/library/restore')
    async def restore(data: dict):
        return await maintain(restore_files, data)

    @app.post('/api/native/open')
    def open_path(data: dict):
        path = Path(data['path']).resolve()
        roots = [service.store.root, Path(settings.data['export_directory']).resolve(), *backup_directories]
        require(any(path.is_relative_to(p) for p in roots) and path.exists(), 'PATH_SCOPE', '仅可打开当前资料库和导出目录。')
        if os.name == 'nt':
            os.startfile(path)
        return {'ok': True}

    @app.post('/api/library/backup')
    async def backup(data: dict):
        directory = Path(data.get('directory') or settings.data['export_directory']).resolve()
        destination = directory / f'AutoReview-backup-{now()[:10]}-{uid()[:8]}.zip'
        destination.parent.mkdir(parents=True, exist_ok=True)
        def build():
            with tempfile.TemporaryDirectory() as temp:
                copy = Path(temp) / 'autoreview.sqlite'
                with closing(sqlite3.connect(service.store.path)) as source, closing(sqlite3.connect(copy)) as target:
                    source.backup(target)
                checksums = {}
                with zipfile.ZipFile(Path(temp) / 'backup.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
                    files = [(copy, 'database/autoreview.sqlite'), (service.store.root / 'library.json', 'library.json')]
                    for asset_directory in ('assets', 'attempt-artifacts', 'legacy-archives', 'archives'):
                        files.extend((p, p.relative_to(service.store.root).as_posix()) for p in (service.store.root / asset_directory).rglob('*') if p.is_file())
                    for path, relative in files:
                        content = path.read_bytes()
                        checksums[relative] = hashlib.sha256(content).hexdigest()
                        archive.writestr(relative, content)
                    archive.writestr('backup-manifest.json', dumps({'version': 1, 'checksums': checksums,
                        'credentials_included': False, 'created_at': now()}))
                # Publish only a complete archive, including across filesystem volumes.
                partial = destination.with_suffix('.partial')
                try:
                    shutil.copyfile(Path(temp) / 'backup.zip', partial)
                    os.replace(partial, destination)
                finally:
                    partial.unlink(missing_ok=True)
            backup_directories.add(directory)
            return {'path': str(destination), 'directory': str(directory), 'bytes': destination.stat().st_size,
                    'credentials_included': False}
        return await data_work(build)

    @app.post('/api/library/legacy')
    async def legacy_import(data: dict):
        from .legacy import inspect_legacy, import_legacy
        require(not runner.current_run, 'RUN_ACTIVE', '请先停止处理。')
        if data.get('preview', True):
            info = await asyncio.to_thread(inspect_legacy, data['path'])
            return {'batches': len(info['batches']), 'images': len(info['pages']), 'missing_page_ids': info['missing_page_ids'],
                    'note': '旧库只读导入；历史答案与复核结论保留，跨页对应关系须重新确认。'}
        return await data_work(import_legacy, service, data['path'], data.get('request_id', ''))

    def relocate_files(data: dict):
        target = Path(data['path']).expanduser().resolve()
        require(not target.is_relative_to(service.store.root) and not service.store.root.is_relative_to(target),
                'LIBRARY_NESTED', '新目录不能与现有资料库重叠。')
        require(not target.exists() or not any(target.iterdir()), 'LIBRARY_NOT_EMPTY', '请选择空目录，避免覆盖资料。')
        target.mkdir(parents=True, exist_ok=True)
        (target / 'database').mkdir(exist_ok=True)
        with closing(sqlite3.connect(service.store.path)) as source, closing(sqlite3.connect(target / 'database/autoreview.sqlite')) as destination:
            source.backup(destination)
        shutil.copy2(service.store.root / 'library.json', target / 'library.json')
        for directory in ('assets', 'attempt-artifacts', 'legacy-archives', 'archives'):
            if (service.store.root / directory).exists():
                shutil.copytree(service.store.root / directory, target / directory)
            for original in (service.store.root / directory).rglob('*'):
                if original.is_file():
                    require(hashlib.sha256(original.read_bytes()).digest() == hashlib.sha256((target / original.relative_to(service.store.root)).read_bytes()).digest(),
                            'COPY_VERIFICATION', '复制校验未通过，原资料库保持不变。')
        settings.save(library_location=str(target))
        app.state.restart_required = True
        runner.closing = True
        from .lifetime import pulse
        pulse('closing')
        return {'path': str(target), 'restart_required': True, 'source_preserved': True}

    @app.post('/api/library/relocate')
    async def relocate(data: dict):
        return await maintain(relocate_files, data)

    if static_dir and Path(static_dir).is_dir():
        app.mount('/', StaticFiles(directory=static_dir, html=True), name='frontend')
    return app
