from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Literal
from uuid import uuid4

from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field

from .codex import CodexUnavailable, capabilities, recognize
from .db import Database, now_iso
from .grading import graded_items


def default_data_dir() -> Path:
    root = os.environ.get('LOCALAPPDATA')
    if root:
        return Path(root) / 'AutoReview'
    return Path.home() / '.local' / 'share' / 'AutoReview'


class CreateBatch(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    class_name: str = Field(default='', max_length=100)


class AnswerItem(BaseModel):
    question_number: int = Field(ge=1)
    english: str = Field(min_length=1, max_length=200)
    chinese: str = Field(default='', max_length=100)
    variants: list[str] = Field(default_factory=list, max_length=8)


class SetAnswers(BaseModel):
    expected_version: int = Field(ge=0)
    items: list[AnswerItem] = Field(min_length=1, max_length=200)


class PageUploadMetadata(BaseModel):
    tag: str = Field(min_length=1, max_length=30)
    model: str = Field(min_length=1, max_length=100)
    effort: str = Field(min_length=1, max_length=30)
    client_request_id: str = Field(min_length=1, max_length=80)


class ReviewItem(BaseModel):
    question_number: int = Field(ge=1)
    status: Literal['correct', 'incorrect', 'missing', 'unknown']
    raw_english: str = Field(default='', max_length=200)
    note: str = Field(default='', max_length=300)


class SaveReview(BaseModel):
    expected_revision: int = Field(ge=0)
    items: list[ReviewItem]
    confirmed: bool = False


class Feedback(BaseModel):
    done: bool


class Finalize(BaseModel):
    expected_version: int = Field(ge=0)


def public_error(error: CodexUnavailable) -> tuple[int, str]:
    value = str(error)
    if value == 'chatgpt_login_required':
        return 401, '请先在本机 Codex CLI 中登录 ChatGPT 账户，然后刷新模型列表。'
    if value == 'codex_cli_not_found':
        return 503, '没有找到 Codex CLI。请确认 codex 命令可用后重启原型。'
    if value == 'no_available_image_model':
        return 503, '当前账户没有报告可用的图片模型。'
    return 503, 'Codex 图像识别暂不可用。请检查登录、网络和 CLI 状态后重试。'


def batch_detail(db: Database, batch_id: str) -> dict[str, Any]:
    with db.connection() as conn:
        batch = conn.execute('SELECT * FROM batches WHERE id=?', (batch_id,)).fetchone()
        if not batch:
            raise HTTPException(404, '未找到这个批次。')
        answers_row = conn.execute(
            'SELECT * FROM answer_versions WHERE batch_id=? AND version=?',
            (batch_id, batch['answer_version']),
        ).fetchone()
        answers = db.load(answers_row['items_json'], []) if answers_row else []
        submissions = conn.execute('SELECT * FROM submissions WHERE batch_id=? ORDER BY created_at,id', (batch_id,)).fetchall()
        pages: list[dict[str, Any]] = []
        for submission in submissions:
            page_rows = conn.execute('SELECT * FROM pages WHERE submission_id=? ORDER BY input_seq', (submission['id'],)).fetchall()
            for page in page_rows:
                attempts = conn.execute('SELECT * FROM attempts WHERE page_id=? ORDER BY queued_at,id', (page['id'],)).fetchall()
                cells = db.load(page['machine_json'], [])
                review = db.load(page['review_json'], {})
                recognized = graded_items(answers, cells, review) if answers else []
                page_attempts = [{
                    'id': attempt['id'], 'status': attempt['status'], 'requested_model': attempt['requested_model'],
                    'requested_effort': attempt['requested_effort'], 'requested_fast': bool(attempt['requested_fast']),
                    'answer_version': attempt['answer_version'], 'queued_at': attempt['queued_at'],
                    'started_at': attempt['started_at'], 'finished_at': attempt['finished_at'],
                    'elapsed_ms': attempt['elapsed_ms'], 'usage': db.load(attempt['usage_json']),
                    'quota_before': db.load(attempt['quota_before_json']),
                    'quota_after': db.load(attempt['quota_after_json']),
                    'turn_id': attempt['turn_id'], 'error_code': attempt['error_code'],
                } for attempt in attempts]
                pages.append({
                    'id': page['id'], 'submission_id': submission['id'], 'tag': submission['tag'],
                    'input_seq': page['input_seq'], 'file_name': page['file_name'],
                    'width': page['width'], 'height': page['height'], 'created_at': page['created_at'],
                    'status': page['status'], 'image_url': f"/media/{page['id']}",
                    'revision': page['revision'], 'reviewed': bool(page['reviewed']),
                    'paper_feedback': submission['paper_feedback'], 'answers': recognized,
                    'extra_cells': [cell for cell in cells if cell['question_number'] not in {answer['question_number'] for answer in answers}],
                    'error_code': page['error_code'], 'attempts': page_attempts,
                })
        events = conn.execute('SELECT COALESCE(MAX(id),0) as last_id FROM events WHERE batch_id=?', (batch_id,)).fetchone()
    counts = {
        'pages': len(pages),
        'queued': sum(page['status'] in ('queued', 'processing') for page in pages),
        'needs_review': sum(page['status'] == 'needs_review' and not page['reviewed'] for page in pages),
        'reviewed': sum(page['reviewed'] for page in pages),
        'failed': sum(page['status'] == 'failed' for page in pages),
        'paper_feedback_pending': sum(page['paper_feedback'] != 'done' for page in pages),
    }
    usage = [attempt['usage'] for page in pages for attempt in page['attempts'] if attempt['usage']]
    totals = {key: sum(item[key] for item in usage if isinstance(item.get(key), (int, float))) for key in ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens', 'output_tokens', 'reasoning_output_tokens')}
    totals['attempts_with_usage'] = len(usage)
    totals['attempts'] = sum(len(page['attempts']) for page in pages)
    return {
        'id': batch['id'], 'name': batch['name'], 'class_name': batch['class_name'],
        'created_at': batch['created_at'], 'status': batch['status'],
        'answer_version': batch['answer_version'], 'answers': answers,
        'counts': counts, 'usage': totals, 'latest_event_id': events['last_id'], 'pages': pages,
    }


def create_app(data_dir: Path | None = None) -> FastAPI:
    db = Database(data_dir or default_data_dir())
    worker_wakeup = asyncio.Event()
    stop_worker = asyncio.Event()

    async def worker_loop() -> None:
        while not stop_worker.is_set():
            job = None
            with db.connection() as conn:
                conn.execute('BEGIN IMMEDIATE')
                page = conn.execute('''SELECT p.*, s.batch_id, s.id AS submission_id, b.answer_version
                    FROM pages p JOIN submissions s ON s.id=p.submission_id
                    JOIN batches b ON b.id=s.batch_id
                    WHERE p.status='queued' AND b.status='collecting'
                      AND b.answer_version > 0 ORDER BY p.input_seq LIMIT 1''').fetchone()
                if page:
                    attempt_id = db.new_id()
                    config = db.load(page['config_json'], {})
                    queued = now_iso()
                    conn.execute("UPDATE pages SET status='processing',error_code=NULL WHERE id=?", (page['id'],))
                    conn.execute('''INSERT INTO attempts(id,page_id,status,requested_model,requested_effort,requested_fast,
                        answer_version,queued_at,started_at) VALUES(?,?,?,?,?,?,?,?,?)''', (
                        attempt_id, page['id'], 'processing', config['model'], config['effort'],
                        int(config.get('fast', False)), page['answer_version'], queued, queued,
                    ))
                    db.add_event(conn, page['batch_id'], 'page.processing', page['id'], {'attempt_id': attempt_id})
                    job = dict(page) | {'attempt_id': attempt_id, 'config': config}
            if not job:
                worker_wakeup.clear()
                try:
                    await asyncio.wait_for(worker_wakeup.wait(), timeout=1)
                except asyncio.TimeoutError:
                    pass
                continue
            started = time.monotonic()
            result = None
            failure = None
            usage = None
            events: list[dict[str, Any]] = []
            turn_id = None
            quota_before = None
            quota_after = None
            try:
                available = await capabilities()
                quota_before = available.get('quota')
                selected_model = next((model for model in available['models'] if model['id'] == job['config']['model']), None)
                if selected_model is None:
                    raise CodexUnavailable('requested_model_unavailable')
                if job['config']['effort'] not in selected_model['reasoning_efforts']:
                    raise CodexUnavailable('requested_effort_unavailable')
                try:
                    result = await recognize(Path(job['file_path']), db.temp_dir / job['id'], job['config'])
                    usage, events, turn_id = result['usage'], result['events'], result['turn_id']
                    answer = batch_detail(db, job['batch_id'])['answers']
                    returned_questions = {cell['question_number'] for cell in result['cells']}
                    # A missing model response is never auto-filled as correct.
                    result['missing_questions'] = [item['question_number'] for item in answer if item['question_number'] not in returned_questions]
                except CodexUnavailable as error:
                    failure = str(error)[:160]
                    usage, events, turn_id = error.usage, error.events, error.turn_id
                finally:
                    try:
                        quota_after = (await capabilities()).get('quota')
                    except CodexUnavailable:
                        pass
            except CodexUnavailable as error:
                failure = str(error)[:160]
            except Exception:
                failure = 'recognition_internal_error'
            elapsed = max(0, round((time.monotonic() - started) * 1000))
            with db.connection() as conn:
                conn.execute('BEGIN IMMEDIATE')
                current = conn.execute('SELECT * FROM pages WHERE id=?', (job['id'],)).fetchone()
                if not current:
                    continue
                # An old result is kept as a candidate if a teacher already changed the page.
                if result is not None:
                    conn.execute("UPDATE pages SET machine_json=?,status='needs_review',error_code=NULL WHERE id=?", (db.dump(result['cells']), job['id']))
                    conn.execute("UPDATE attempts SET status='succeeded',finished_at=?,elapsed_ms=?,usage_json=?,events_json=?,turn_id=?,quota_before_json=?,quota_after_json=? WHERE id=?", (
                        now_iso(), elapsed, db.dump(usage) if usage is not None else None,
                        db.dump(events) if events else None, turn_id,
                        db.dump(quota_before) if quota_before is not None else None,
                        db.dump(quota_after) if quota_after is not None else None, job['attempt_id'],
                    ))
                    db.add_event(conn, job['batch_id'], 'page.ready', job['id'], {'attempt_id': job['attempt_id'], 'usage_known': usage is not None})
                else:
                    conn.execute("UPDATE pages SET status='failed',error_code=? WHERE id=?", (failure, job['id']))
                    conn.execute("UPDATE attempts SET status='failed',finished_at=?,elapsed_ms=?,error_code=?,usage_json=?,events_json=?,turn_id=?,quota_before_json=?,quota_after_json=? WHERE id=?", (
                        now_iso(), elapsed, failure, db.dump(usage) if usage is not None else None,
                        db.dump(events) if events else None, turn_id,
                        db.dump(quota_before) if quota_before is not None else None,
                        db.dump(quota_after) if quota_after is not None else None, job['attempt_id'],
                    ))
                    db.add_event(conn, job['batch_id'], 'page.failed', job['id'], {'attempt_id': job['attempt_id'], 'error_code': failure, 'usage_known': usage is not None})

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # Keep an interrupted charged call visible; require a deliberate teacher retry.
        with db.connection() as conn:
            interrupted = conn.execute("SELECT * FROM attempts WHERE status='processing'").fetchall()
            for attempt in interrupted:
                conn.execute("UPDATE attempts SET status='interrupted',finished_at=?,error_code='process_interrupted' WHERE id=?", (now_iso(), attempt['id']))
                conn.execute("UPDATE pages SET status='failed',error_code='process_interrupted' WHERE id=?", (attempt['page_id'],))
                batch = conn.execute('SELECT s.batch_id FROM pages p JOIN submissions s ON s.id=p.submission_id WHERE p.id=?', (attempt['page_id'],)).fetchone()
                if batch:
                    db.add_event(conn, batch['batch_id'], 'page.failed', attempt['page_id'], {'attempt_id': attempt['id'], 'error_code': 'process_interrupted', 'usage_known': attempt['usage_json'] is not None})
        stop_worker.clear()
        task = asyncio.create_task(worker_loop(), name='autoreview-serial-queue')
        yield
        stop_worker.set()
        worker_wakeup.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    app = FastAPI(title='AutoReview Local Prototype', version='0.1.0', lifespan=lifespan)
    app.state.db = db
    app.state.worker_wakeup = worker_wakeup

    @app.get('/api/health')
    def health() -> dict[str, str]:
        return {'status': 'ok', 'storage': 'local'}

    @app.get('/api/capabilities')
    async def get_capabilities() -> dict[str, Any]:
        try:
            return await capabilities()
        except CodexUnavailable as error:
            status_code, message = public_error(error)
            raise HTTPException(status_code, message) from error

    @app.get('/api/batches')
    def list_batches() -> list[dict[str, Any]]:
        with db.connection() as conn:
            rows = conn.execute('SELECT id,name,class_name,created_at,status,answer_version FROM batches ORDER BY created_at DESC').fetchall()
        return [dict(row) for row in rows]

    @app.post('/api/batches', status_code=201)
    def create_batch(payload: CreateBatch) -> dict[str, Any]:
        batch_id = db.new_id()
        created = now_iso()
        with db.connection() as conn:
            conn.execute('INSERT INTO batches(id,name,class_name,created_at,updated_at) VALUES(?,?,?,?,?)', (batch_id, payload.name.strip(), payload.class_name.strip(), created, created))
            db.add_event(conn, batch_id, 'batch.created', batch_id, {})
        return batch_detail(db, batch_id)

    @app.get('/api/batches/{batch_id}')
    def get_batch(batch_id: str) -> dict[str, Any]:
        return batch_detail(db, batch_id)

    @app.put('/api/batches/{batch_id}/answers')
    def set_answers(batch_id: str, payload: SetAnswers) -> dict[str, Any]:
        items = [item.model_dump() for item in payload.items]
        numbers = [item['question_number'] for item in items]
        if len(set(numbers)) != len(numbers):
            raise HTTPException(422, '题号重复，请检查参考答案。')
        if numbers != sorted(numbers):
            raise HTTPException(422, '请按题号从小到大录入参考答案。')
        with db.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            batch = conn.execute('SELECT * FROM batches WHERE id=?', (batch_id,)).fetchone()
            if not batch:
                raise HTTPException(404, '未找到这个批次。')
            if batch['status'] == 'finalized':
                raise HTTPException(409, '批次已归档，请先重新打开。')
            if batch['answer_version'] != payload.expected_version:
                raise HTTPException(409, '参考答案已在其他窗口更新，请刷新后再保存。')
            next_version = batch['answer_version'] + 1
            conn.execute('INSERT INTO answer_versions(batch_id,version,items_json,created_at) VALUES(?,?,?,?)', (batch_id, next_version, db.dump(items), now_iso()))
            conn.execute('UPDATE batches SET answer_version=?,updated_at=? WHERE id=?', (next_version, now_iso(), batch_id))
            # Existing transcripts are regraded against the new immutable key; they require fresh teacher review.
            existing = conn.execute("SELECT p.id FROM pages p JOIN submissions s ON s.id=p.submission_id WHERE s.batch_id=? AND p.status='needs_review'", (batch_id,)).fetchall()
            for page in existing:
                conn.execute('UPDATE pages SET reviewed=0 WHERE id=?', (page['id'],))
            db.add_event(conn, batch_id, 'answers.updated', batch_id, {'version': next_version})
        worker_wakeup.set()
        return batch_detail(db, batch_id)

    @app.post('/api/batches/{batch_id}/pages', status_code=202)
    async def upload_page(batch_id: str, request: Request, tag: str, model: str, effort: str, file: UploadFile = File(...), client_request_id: str = Header(alias='X-Client-Request-Id')) -> dict[str, Any]:
        db = request.app.state.db
        if len(tag.strip()) > 30 or not tag.strip():
            raise HTTPException(422, '请输入不超过30个字符的纸签编号。')
        if len(model) > 100 or len(effort) > 30:
            raise HTTPException(422, '模型配置无效，请刷新模型列表。')
        with db.connection() as conn:
            previous = conn.execute('SELECT page_id FROM idempotency WHERE request_id=?', (client_request_id,)).fetchone()
            if previous:
                return {'page_id': previous['page_id'], 'duplicate_request': True}
            batch = conn.execute('SELECT * FROM batches WHERE id=?', (batch_id,)).fetchone()
            if not batch:
                raise HTTPException(404, '未找到这个批次。')
            if batch['status'] == 'finalized':
                raise HTTPException(409, '批次已归档，请先重新打开。')
            if batch['answer_version'] == 0:
                raise HTTPException(409, '请先录入并确认参考答案，再提交作业图片。')
        content = await file.read(25 * 1024 * 1024 + 1)
        if not content or len(content) > 25 * 1024 * 1024:
            raise HTTPException(413, '图片不能为空，单张大小上限为25 MB。')
        try:
            with Image.open(content := __import__('io').BytesIO(content)) as image:
                image.verify()
            with Image.open(content) as image:
                fmt = image.format
                width, height = image.size
        except (UnidentifiedImageError, OSError, ValueError) as error:
            raise HTTPException(415, '请选择清晰可读的 JPEG 或 PNG 图片。') from error
        if fmt not in ('JPEG', 'PNG') or width < 120 or height < 120:
            raise HTTPException(415, '首版支持宽高至少120像素的 JPEG 或 PNG 图片。')
        suffix = '.jpg' if fmt == 'JPEG' else '.png'
        image_id = db.new_id()
        target = db.image_dir / f'{image_id}{suffix}'
        temp = db.image_dir / f'{image_id}.tmp'
        digest = hashlib.sha256(content.getvalue()).hexdigest()
        try:
            temp.write_bytes(content.getvalue())
            temp.replace(target)
            with db.connection() as conn:
                conn.execute('BEGIN IMMEDIATE')
                batch = conn.execute('SELECT * FROM batches WHERE id=?', (batch_id,)).fetchone()
                if not batch or batch['status'] == 'finalized' or batch['answer_version'] == 0:
                    raise HTTPException(409, '批次状态已变更，请刷新后重试。')
                submission = conn.execute('SELECT * FROM submissions WHERE batch_id=? AND tag=?', (batch_id, tag.strip())).fetchone()
                if not submission:
                    submission_id = db.new_id()
                    conn.execute('INSERT INTO submissions(id,batch_id,tag,created_at) VALUES(?,?,?,?)', (submission_id, batch_id, tag.strip(), now_iso()))
                else:
                    submission_id = submission['id']
                sequence = conn.execute('SELECT COALESCE(MAX(input_seq),0)+1 AS next FROM pages').fetchone()['next']
                page_config = {'model': model, 'effort': effort, 'fast': False}
                conn.execute('''INSERT INTO pages(id,submission_id,input_seq,file_name,file_path,sha256,width,height,created_at,status,config_json)
                    VALUES(?,?,?,?,?,?,?,?,?,'queued',?)''', (
                    image_id, submission_id, sequence, Path(file.filename or '作业图片').name[:200], str(target), digest,
                    width, height, now_iso(), db.dump(page_config),
                ))
                conn.execute('INSERT INTO idempotency(request_id,page_id) VALUES(?,?)', (client_request_id, image_id))
                db.add_event(conn, batch_id, 'page.queued', image_id, {'input_seq': sequence, 'tag': tag.strip()})
        except BaseException:
            with suppress(OSError):
                temp.unlink()
            with suppress(OSError):
                target.unlink()
            raise
        worker_wakeup.set()
        return {'page_id': image_id, 'submission_id': submission_id, 'input_seq': sequence, 'status': 'queued'}

    @app.get('/media/{page_id}')
    def page_media(page_id: str, request: Request) -> FileResponse:
        db = request.app.state.db
        with db.connection() as conn:
            row = conn.execute('SELECT file_path,file_name FROM pages WHERE id=?', (page_id,)).fetchone()
        if not row or not Path(row['file_path']).is_file():
            raise HTTPException(404, '图片已不可用。')
        suffix = Path(row['file_path']).suffix.lower()
        mime = 'image/jpeg' if suffix in ('.jpg', '.jpeg') else 'image/png'
        return FileResponse(row['file_path'], media_type=mime, filename=row['file_name'], content_disposition_type='inline')

    @app.patch('/api/pages/{page_id}/review')
    def save_review(page_id: str, payload: SaveReview, request: Request) -> dict[str, Any]:
        db = request.app.state.db
        numbers = [item.question_number for item in payload.items]
        if len(set(numbers)) != len(numbers):
            raise HTTPException(422, '同一道题不能重复提交。')
        with db.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('''SELECT p.*,s.batch_id,s.paper_feedback,b.status AS batch_status
                FROM pages p JOIN submissions s ON s.id=p.submission_id JOIN batches b ON b.id=s.batch_id WHERE p.id=?''', (page_id,)).fetchone()
            if not row:
                raise HTTPException(404, '未找到这页作业。')
            if row['batch_status'] == 'finalized':
                raise HTTPException(409, '批次已归档，请先重新打开。')
            if row['revision'] != payload.expected_revision:
                raise HTTPException(409, '这页复核已在其他窗口更新，请刷新后重试。')
            if row['status'] != 'needs_review':
                raise HTTPException(409, '请等这页图片处理完成后再复核。')
            previous = db.load(row['review_json'], {})
            before_reviewed = row['reviewed']
            revision = row['revision'] + 1
            snapshot = dict(previous)
            for item in payload.items:
                snapshot[str(item.question_number)] = item.model_dump()
            if payload.items and row['paper_feedback'] == 'done':
                conn.execute("UPDATE submissions SET paper_feedback='needs_update' WHERE id=?", (row['submission_id'],))
            conn.execute('UPDATE pages SET review_json=?,revision=?,reviewed=? WHERE id=?', (db.dump(snapshot), revision, int(payload.confirmed), page_id))
            conn.execute('INSERT INTO review_history(page_id,revision,payload_json,reviewed,action,created_at) VALUES(?,?,?,?,?,?)', (page_id, revision, db.dump(snapshot), int(payload.confirmed), 'confirm' if payload.confirmed else 'edit', now_iso()))
            db.add_event(conn, row['batch_id'], 'page.reviewed' if payload.confirmed else 'page.review_changed', page_id, {'revision': revision})
        return batch_detail(db, row['batch_id'])

    @app.post('/api/pages/{page_id}/undo')
    def undo_review(page_id: str, expected_revision: int, request: Request) -> dict[str, Any]:
        db = request.app.state.db
        with db.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('''SELECT p.*,s.batch_id,b.status AS batch_status FROM pages p
                JOIN submissions s ON s.id=p.submission_id JOIN batches b ON b.id=s.batch_id WHERE p.id=?''', (page_id,)).fetchone()
            if not row:
                raise HTTPException(404, '未找到这页作业。')
            if row['batch_status'] == 'finalized':
                raise HTTPException(409, '批次已归档，请先重新打开。')
            if row['revision'] != expected_revision or row['revision'] == 0:
                raise HTTPException(409, '没有可撤销的复核操作，或复核已更新。')
            previous = conn.execute('SELECT * FROM review_history WHERE page_id=? AND revision=?', (page_id, row['revision'] - 1)).fetchone()
            revision = row['revision'] + 1
            snapshot = previous['payload_json'] if previous else '{}'
            confirmed = previous['reviewed'] if previous else 0
            conn.execute('UPDATE pages SET review_json=?,revision=?,reviewed=? WHERE id=?', (snapshot, revision, confirmed, page_id))
            conn.execute('INSERT INTO review_history(page_id,revision,payload_json,reviewed,action,created_at) VALUES(?,?,?,?,?,?)', (page_id, revision, snapshot, confirmed, 'undo', now_iso()))
            db.add_event(conn, row['batch_id'], 'page.review_changed', page_id, {'revision': revision, 'action': 'undo'})
        return batch_detail(db, row['batch_id'])

    @app.post('/api/pages/{page_id}/retry')
    def retry_page(page_id: str, request: Request) -> dict[str, Any]:
        db = request.app.state.db
        with db.connection() as conn:
            row = conn.execute('''SELECT p.*,s.batch_id,b.status AS batch_status FROM pages p
                JOIN submissions s ON s.id=p.submission_id JOIN batches b ON b.id=s.batch_id WHERE p.id=?''', (page_id,)).fetchone()
            if not row:
                raise HTTPException(404, '未找到这页作业。')
            if row['batch_status'] == 'finalized':
                raise HTTPException(409, '批次已归档，请先重新打开。')
            if row['status'] != 'failed':
                raise HTTPException(409, '只有处理失败的作业可以重试。')
            conn.execute("UPDATE pages SET status='queued',error_code=NULL WHERE id=?", (page_id,))
            db.add_event(conn, row['batch_id'], 'page.queued', page_id, {'retry': True})
        worker_wakeup.set()
        return batch_detail(db, row['batch_id'])

    @app.post('/api/submissions/{submission_id}/paper-feedback')
    def paper_feedback(submission_id: str, payload: Feedback, request: Request) -> dict[str, Any]:
        db = request.app.state.db
        with db.connection() as conn:
            row = conn.execute('''SELECT s.*,b.status FROM submissions s JOIN batches b ON b.id=s.batch_id WHERE s.id=?''', (submission_id,)).fetchone()
            if not row:
                raise HTTPException(404, '未找到这份作业。')
            if row['status'] == 'finalized':
                raise HTTPException(409, '批次已归档，请先重新打开。')
            value = 'done' if payload.done else 'pending'
            conn.execute('UPDATE submissions SET paper_feedback=? WHERE id=?', (value, submission_id))
            db.add_event(conn, row['batch_id'], 'paper_feedback.changed', submission_id, {'status': value})
        return batch_detail(db, row['batch_id'])

    @app.post('/api/batches/{batch_id}/finalize')
    def finalize_batch(batch_id: str, payload: Finalize) -> dict[str, Any]:
        with db.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            batch = conn.execute('SELECT * FROM batches WHERE id=?', (batch_id,)).fetchone()
            if not batch:
                raise HTTPException(404, '未找到这个批次。')
            if batch['answer_version'] != payload.expected_version:
                raise HTTPException(409, '批次答案版本已更新，请刷新后重试。')
            detail = batch_detail(db, batch_id)
            if not detail['pages'] or detail['counts']['queued'] or detail['counts']['failed'] or detail['counts']['needs_review']:
                raise HTTPException(409, '请先处理失败项，并完成人工复核，再结束批次。')
            snapshot = db.dump({'answer_version': batch['answer_version'], 'pages': [{
                'id': page['id'], 'tag': page['tag'], 'revision': page['revision'], 'reviewed': page['reviewed'],
            } for page in detail['pages']], 'created_at': now_iso()})
            conn.execute("UPDATE batches SET status='finalized',final_snapshot=?,updated_at=? WHERE id=?", (snapshot, now_iso(), batch_id))
            db.add_event(conn, batch_id, 'batch.finalized', batch_id, {'pages': len(detail['pages'])})
        return batch_detail(db, batch_id)

    @app.post('/api/batches/{batch_id}/reopen')
    def reopen_batch(batch_id: str) -> dict[str, Any]:
        with db.connection() as conn:
            batch = conn.execute('SELECT * FROM batches WHERE id=?', (batch_id,)).fetchone()
            if not batch:
                raise HTTPException(404, '未找到这个批次。')
            conn.execute("UPDATE batches SET status='collecting',updated_at=? WHERE id=?", (now_iso(), batch_id))
            db.add_event(conn, batch_id, 'batch.reopened', batch_id, {})
        return batch_detail(db, batch_id)

    @app.get('/api/batches/{batch_id}/events')
    async def batch_events(batch_id: str, request: Request, after_id: int = 0, last_event_id: str | None = Header(default=None, alias='Last-Event-ID')) -> StreamingResponse:
        with db.connection() as conn:
            exists = conn.execute('SELECT 1 FROM batches WHERE id=?', (batch_id,)).fetchone()
        if not exists:
            raise HTTPException(404, '未找到这个批次。')
        try:
            cursor = max(after_id, int(last_event_id or '0'))
        except ValueError:
            cursor = after_id

        async def stream():
            nonlocal cursor
            heartbeat = 0
            while not await request.is_disconnected():
                with db.connection() as conn:
                    rows = conn.execute('SELECT * FROM events WHERE batch_id=? AND id>? ORDER BY id LIMIT 100', (batch_id, cursor)).fetchall()
                if rows:
                    for row in rows:
                        cursor = row['id']
                        payload = db.load(row['payload_json'], {})
                        record = {'id': row['id'], 'type': row['event_type'], 'entity_id': row['entity_id'], 'payload': payload, 'created_at': row['created_at']}
                        yield f"id: {row['id']}\nevent: {row['event_type']}\ndata: {json.dumps(record, ensure_ascii=False)}\n\n"
                    continue
                heartbeat += 1
                if heartbeat >= 20:
                    heartbeat = 0
                    yield ': keep-alive\n\n'
                await asyncio.sleep(0.75)

        return StreamingResponse(stream(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    return app


app = create_app()
