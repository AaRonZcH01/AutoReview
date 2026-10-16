from __future__ import annotations
import asyncio
import time
import hashlib
import contextvars
from pathlib import Path
from .core import require, Problem, now, uid, digest, atomic_write
from .contracts import prompt_record, validate_output_schema
from .telemetry import sanitize
from .verification import POLICY_VERSION as NEW_VERIFICATION_POLICY, LEGACY_POLICY_VERSION
from .execution_config import MAX_PARALLELISM

_bridges = contextvars.ContextVar('autoreview_worker_bridges', default=None)

# Shared authorization/environment failures must pause immediately. Other student
# page failures are isolated; three consecutive equal codes trip a durable fence.
GLOBAL_FAILURES = {'USAGE_LIMIT', 'LOGIN_REQUIRED', 'ACCOUNT_TYPE', 'PROFILE_REQUIRED',
    'MODEL_UNAVAILABLE', 'MODEL_CATALOGUE_UNAVAILABLE', 'IMAGE_INPUT_UNAVAILABLE', 'EFFORT_UNAVAILABLE', 'FAST_UNAVAILABLE',
    'CODEX_NOT_INSTALLED', 'UNEXPECTED_TOOL', 'OBJECT_DELETED', 'RUN_VERSION_CHANGED',
    'QUEUE_PAUSED', 'APPLICATION_CLOSING'}


class Runner:
    def __init__(self, service, provider):
        self.service, self.store, self.provider = service, service.store, provider
        self.job = None
        self.rpc_loop = None
        self.bridge_jobs = set()
        self.closing = False
        self.active_attempts = {}
        # Idle slots do not all poll SQLite. This is only a scheduling hint;
        # the transactional claim below remains the authority for every limit.
        self.worker_capacity = 1
        self.active_work = {}

    @property
    def current_attempt(self):
        return next(iter(tuple(self.active_attempts)), None)

    @property
    def current_run(self):
        return next(iter(tuple(self.active_work.values())), None)

    async def interrupt_active(self, *, run_ids=None, page_id=None, work_ids=None):
        identifiers = [key for key, value in tuple(self.active_attempts.items())
                       if (run_ids is None or value['run_id'] in run_ids)
                       and (page_id is None or value.get('page_id') == page_id)
                       and (work_ids is None or value.get('work_id') in work_ids)]
        await asyncio.gather(*(self.rpc('interrupt', key) for key in identifiers), return_exceptions=True)

    def enqueue_capture(self, db, session, candidate_page_ids):
        """Called inside capture commit: page creation and queue insertion are atomic."""
        require(not self.closing, 'APPLICATION_CLOSING', '程序正在退出，未新增云端任务；请重新打开后继续。', 409)
        require(session.get('delivery_mode', 'immediate') == 'immediate', 'CAPTURE_LOCAL_ONLY',
                '本轮仅保存本地，请在图库按批处理方式提交。', 409)
        from .reference_review import required_slots
        reference = self.store.get(db, 'reference_versions', session['reference_version_id'])
        require(all(a['kind'] != 'open_response' or a.get('excluded') for a in reference['items']),
                'QUESTION_ADAPTER_UNAVAILABLE', '开放作文评分尚未开放。')
        chain = self.store.get(db, 'continuation_groups', session['continuation_group_id'])
        require(chain.get('user_intent') == 'continue', 'CAPTURE_QUEUE_PAUSED', '处理队列已停止，请停止采集并在图库确认后重新开始。', 409)
        runs = self.store.list(db, 'runs', parent_id=chain['id'])
        require(not any(r['state'] == 'paused' for r in runs), 'CAPTURE_QUEUE_PAUSED', '处理已暂停，请先在图库解决问题并继续处理。', 409)
        run = next((r for r in runs if r['state'] in ('queued','running')), None)
        require(run is None or run['config'] == session['config'], 'CAPTURE_CONFIG_CHANGED',
                '队列账户或模型已变化，请结束当前采集并重新开始，重新确认发送账户。', 409)
        existing = self.store.list(db, 'work_items', task_id=session['task_id'])
        pages = [self.store.get(db, 'pages', key) for key in candidate_page_ids]
        ready, waiting = [], []
        for page in pages:
            if page.get('capture_delivery_mode') == 'local':
                # A later immediate session must not implicitly upload pages
                # explicitly captured for local batch submission.
                continue
            if page.get('capture_withheld'):
                waiting.append(page['id'])
                continue
            if any(w.get('page_id') == page['id'] and w.get('generation') == page['generation']
                   and w['state'] in ('pending','running','completed','failed') for w in existing):
                continue
            needed = {slot for a in reference['items'] if a['slot'] == page['slot'] and not a.get('excluded')
                      for slot in required_slots(a, reference)} - {page['slot']}
            siblings = [p for p in pages if p['parent_id'] == page['parent_id'] and p['slot'] in needed]
            if needed - {p['slot'] for p in siblings} or not all((p.get('preparation') or {}).get('confirmed') and not p.get('capture_withheld') and p.get('capture_delivery_mode') != 'local' for p in siblings):
                waiting.append(page['id'])
                continue
            ready.append((page, {p['id']:p['image_version_id'] for p in siblings}))
        if ready and run is None:
            run = self.store.put(db, 'runs', {'parent_id':chain['id'], 'task_id':session['task_id'],
                'state':'queued', 'purpose':'student', 'generation':1, 'config':session['config'],
                'verification_policy':NEW_VERIFICATION_POLICY,
                'reference_version_id':reference['id'], 'submitted_at':now(), 'recovery_count':0,
                'group_ids':[session['group_id']], 'source':'stream_capture', 'capture_session_id':session['id'],
                'prompt_ids':{p:prompt_record(p)['id'] for p in ('reference','student','writing','student_verify')},
                'seq':self.store.sequence(db,'runs')})
        sequence = max((w['seq'] for w in existing if run and w['parent_id'] == run['id']), default=0)
        for offset, (page, dependencies) in enumerate(ready, 1):
            self.store.put(db, 'work_items', {'parent_id':run['id'], 'task_id':session['task_id'],
                'group_id':session['group_id'], 'seq':sequence+offset, 'state':'pending', 'purpose':'student',
                'page_id':page['id'], 'image_version_id':page['image_version_id'], 'generation':page['generation'],
                'reference_version_id':reference['id'], 'preparation':page['preparation'], 'dependency_versions':dependencies})
        if ready:
            self.store.event(db, 'run.capture_enqueued', run, run_id=run['id'], page_count=len(ready))
        return {'queued_page_ids':[p['id'] for p,_ in ready], 'waiting_page_ids':waiting, 'runs':[run] if run else []}

    def retry_region(self, page_id, data, config):
        require(data.get('cloud_consent'), 'CLOUD_CONSENT', '局部复看是新调用，请确认发送图片并使用当前账户额度。')
        with self.store.connection(write=True) as db:
            cached = self.store.once(db, data.get('request_id',''), data)
            if cached:
                return cached
            require(not self.closing, 'APPLICATION_CLOSING', '程序正在退出，未新增云端任务；请重新打开后继续。', 409)
            page = self.store.get(db,'pages',page_id)
            self.service.require_available(db, 'tasks', page['task_id'])
            self.service.require_available(db, 'groups', page['group_id'])
            require(page['revision'] == data.get('revision'), 'REVISION_CONFLICT', '页面已变化，请刷新后重试。',409)
            require(page['state']=='processed' and page.get('observation_id'), 'PAGE_NOT_PROCESSED','请先完成首轮识别。')
            require(not any(r['state'] in ('queued','running') for r in self.store.list(db,'runs',task_id=page['task_id'])), 'RUN_ACTIVE','请先停止本任务的处理。')
            ref = self.store.get(db,'reference_versions',page['reference_version_id'])
            answers = [a for a in ref['items'] if a['id'] in data.get('question_ids',[]) and a['slot']==page['slot'] and not a.get('excluded')]
            require(answers and len(answers)==len(set(data['question_ids'])), 'QUESTION_SCOPE','请选择本页有效题目。')
            require(not any(len(__import__('autoreview.v2.reference_review',fromlist=['required_slots']).required_slots(a,ref))>1 for a in answers), 'RETRY_CROSS_PAGE','跨页题请使用整份重做，避免裁切必要上下文。')
            prep = page.get('preparation') or {}
            require(prep.get('confirmed') and all(prep['zones'].get(a['id']) for a in answers), 'REGION_REQUIRED','局部复看需要已确认的完整作答区域。')
            chain = self.store.put(db,'continuation_groups',{'parent_id':page['task_id'],'task_id':page['task_id'],'state':'active','user_intent':'continue','started_at':now()})
            run = self.store.put(db,'runs',{'parent_id':chain['id'],'task_id':page['task_id'],'purpose':'student','state':'queued','generation':1,'config':config,
                'verification_policy':NEW_VERIFICATION_POLICY,
                'reference_version_id':ref['id'],'submitted_at':now(),'seq':self.store.sequence(db,'runs'),'group_ids':[page['group_id']],
                'prompt_ids':{p:prompt_record(p)['id'] for p in ('reference','student','writing','student_verify')},'recovery_count':0,'region_retry':True})
            page = self.store.put(db,'pages',{**page,'state':'pending','generation':page['generation']+1,'reviewed':False})
            self.store.put(db,'work_items',{'parent_id':run['id'],'task_id':page['task_id'],'group_id':page['group_id'],'seq':1,'state':'pending','purpose':'student',
                'page_id':page_id,'image_version_id':page['image_version_id'],'generation':page['generation'],'reference_version_id':ref['id'],
                'preparation':prep,'retry_question_ids':[a['id'] for a in answers]})
            self.service.update_submission(db,page['parent_id'])
            self.store.event(db,'run.region_retry_queued',run,run_id=run['id'],question_ids=[a['id'] for a in answers])
            return self.store.remember(db,data['request_id'],data,run)

    def create(self, task_id, data, config, account=None):
        require(data.get('cloud_consent') or data.get('reuse_transcription'), 'CLOUD_CONSENT',
                '请确认图片将发送到所选 ChatGPT 账户。')
        with self.store.connection(write=True) as db:
            cached = self.store.once(db, data.get('request_id', ''), data)
            if cached:
                return cached
            require(not self.closing, 'APPLICATION_CLOSING', '程序正在退出，未新增云端任务；请重新打开后继续。', 409)
            task = self.store.get(db, 'tasks', task_id)
            self.service.require_available(db, 'tasks', task_id)
            purpose = data.get('purpose', 'student')
            require(purpose in ('student', 'reference'), 'PURPOSE_INVALID', '不支持此处理类型。')
            require(not any(r['state'] in ('queued', 'running') for r in self.store.list(db, 'runs', task_id=task_id)),
                    'RUN_ACTIVE', '该任务已有处理中轮次，请先等待或停止。', 409)
            chain = self.store.put(db, 'continuation_groups', {'parent_id': task_id, 'task_id': task_id,
                'state': 'active', 'user_intent': 'continue', 'started_at': now()})
            if purpose == 'reference':
                draft = self.store.get(db, 'reference_drafts', task_id)
                require(draft['asset_ids'] or data.get('scoring_only') and (data.get('rule_text') or data.get('rule_asset_ids')), 'REFERENCE_IMAGE_REQUIRED', '请先上传范本图片。')
                require(data.get('images_prepared'), 'IMAGE_PREVIEW_REQUIRED', '请检查范本处理图与操作记录后确认图片准备。')
                require(data.get('draft_revision', draft['revision']) == draft['revision'], 'REFERENCE_CHANGED', '范本已变化，请重新核对提交。', 409)
                for previous in self.store.list(db, 'runs', task_id=task_id):
                    if previous['purpose'] == 'reference' and previous['state'] == 'paused':
                        self.store.put(db, 'runs', {**previous, 'state': 'superseded'})
                draft = self.store.put(db, 'reference_drafts', {**draft, 'state': 'parsing'})
                scopes = [(None, [], {'draft_revision': draft['revision'], 'asset_ids': data.get('rule_asset_ids',[]) if data.get('scoring_only') else draft['asset_ids'], 'scoring_enabled':task.get('scoring_enabled',False), 'scoring_only':bool(data.get('scoring_only')), 'rule_text':data.get('rule_text',''), 'score_structure':[{'question_index':i, 'id':a['id'], 'slot':a['slot'], 'section':a.get('section',''), 'number':a['number'], 'kind':a.get('kind','dictation')} for i,a in enumerate(draft['items'])]})]
            else:
                from .scoring import gate
                gate(self.store, db, task_id)
                groups = [self.store.get(db, 'groups', identifier) for identifier in data.get('group_ids', [])]
                require(groups and all(g['task_id'] == task_id for g in groups), 'GROUP_SCOPE', '请选择当前任务中的班级。')
                selected = data.get('page_ids')
                require(selected is None or isinstance(selected,list) and selected and all(isinstance(p,str) for p in selected),
                        'PAGE_SCOPE', '请选择需要重新识别的替换照片。')
                selected = set(selected or [])
                if selected:
                    scoped = [self.store.get(db,'pages',key) for key in selected]
                    require(not data.get('regrade_all') and all(p['group_id'] in {g['id'] for g in groups}
                            and p['task_id']==task_id and p.get('replacement_pending') and not p.get('capture_session_id') for p in scoped),
                            'PAGE_SCOPE', '逐页重新提交仅适用于当前班级待重新识别的替换照片。')
                scopes = []
                grouped = {}
                for group in groups:
                    self.service.require_available(db, 'groups', group['id'])
                    require(group.get('reference_version_id'), 'REFERENCE_NOT_CONFIRMED', '参考答案尚未确认。')
                    ref_id = data.get('reference_version_id') or group['reference_version_id']
                    ref = self.store.get(db, 'reference_versions', ref_id)
                    require(ref['task_id'] == task_id, 'REFERENCE_SCOPE', '答案版本不属于当前任务。')
                    require(all(a['kind'] != 'open_response' or a['excluded'] for a in ref['items']), 'QUESTION_ADAPTER_UNAVAILABLE', '开放作文评分尚未开放，请使用明确答案的题型。')
                    regrade = bool(data.get('regrade_all'))
                    if ref_id != group['reference_version_id']:
                        require(regrade, 'REGRADE_REQUIRED', '更换答案版本必须对所选班级整体重做。')
                        # Per-run historical selection must not change the task official version.
                        pass
                    pages = self.store.list(db, 'pages', group_id=group['id'], include_deleted=False)
                    require(pages, 'NO_PAGES', '所选班级还没有作业。')
                    slot = data.get('slot')
                    if not slot and not selected:
                        subs = self.store.list(db, 'submissions', group_id=group['id'], include_deleted=False)
                        incomplete = [s['paper_tag'] for s in subs if len([p for p in pages if p['parent_id'] == s['id']]) != ref['page_count']]
                        require(not incomplete, 'INCOMPLETE_SUBMISSIONS', '整份处理前，每个编号必须具备全部页面。', missing_tags=incomplete)
                    require(not (regrade and slot), 'REGRADE_SCOPE', '答案整体重做应包含所选班级的所有页面。')
                    for page in pages:
                        if selected and page['id'] not in selected:
                            continue
                        if slot and page['slot'] != int(slot):
                            continue
                        require(not page.get('capture_withheld'), 'CAPTURE_WITHHELD', '有流式照片已撤回识别；请先在图库逐张重新提交或替换，批处理不会自动重发。')
                        if page['state'] == 'processed' and not regrade:
                            continue
                        preparation = page.get('preparation') or {}
                        stamp = digest({'image_version': page['image_version_id'], 'reference': ref_id, 'slot': page['slot'], 'seq': page['seq']})
                        require(preparation.get('confirmed') and preparation.get('stamp') == stamp,
                                'IMAGE_PREVIEW_REQUIRED', '请先在图片准备中核对处理图、页位和答题区域，再确认。')
                        needed = {dep for answer in ref['items'] if answer['slot'] == page['slot'] and not answer.get('excluded') for dep in __import__('autoreview.v2.reference_review',fromlist=['required_slots']).required_slots(answer, ref)} - {page['slot']}
                        siblings = [p for p in pages if p['parent_id'] == page['parent_id'] and p['slot'] in needed]
                        require(not any(p.get('capture_withheld') for p in siblings), 'CAPTURE_WITHHELD', '跨页关联照片已撤回识别，请先在图库处理。')
                        require(needed <= {p['slot'] for p in siblings}, 'DEPENDENCY_MISSING', '跨页题所需页面尚未收齐。')
                        require(all((p.get('preparation') or {}).get('confirmed') for p in siblings), 'DEPENDENCY_PREVIEW', '跨页依赖图片也需先完成图片准备确认。')
                        page['dependency_versions'] = {p['id']: p['image_version_id'] for p in siblings}
                        if regrade:
                            require(not data.get('reuse_transcription') or page.get('observation_id'),
                                    'TRANSCRIPTION_MISSING', '有页面没有可复用的转录，请选择重新识别。')
                        page = self.store.put(db, 'pages', {**page, 'state': 'pending',
                            'reference_version_id': ref_id, 'generation': page['generation'] + 1,
                            'overrides': {} if regrade else page.get('overrides', {}), 'reviewed': False,
                            'history': [] if regrade else page.get('history', []),
                            'history_cursor': -1 if regrade else page.get('history_cursor', -1)})
                        grouped.setdefault(ref_id, []).append(page)
                        for old_work in self.store.list(db, 'work_items', task_id=task_id):
                            if old_work.get('page_id') == page['id'] and old_work['state'] in ('pending', 'running'):
                                self.store.put(db, 'work_items', {**old_work, 'state': 'superseded'})
                        sub = self.store.get(db,'submissions',page['parent_id'])
                        if task.get('scoring_enabled'):
                            self.store.put(db,'submissions',{**sub,'scoring_version_id':task.get('scoring_version_id')})
                        self.service.update_submission(db, page['parent_id'])
                scopes = [(ref_id, pages, {}) for ref_id, pages in grouped.items()]
                require(scopes, 'NOTHING_PENDING', '没有待处理页面。')
                continuation_ids = {p.get('replacement_continuation_group_id') for _, pages, _ in scopes for p in pages}
                if len(continuation_ids) == 1 and None not in continuation_ids:
                    unused = chain
                    chain = self.store.get(db, 'continuation_groups', continuation_ids.pop())
                    chain = self.store.put(db, 'continuation_groups', {**chain, 'state': 'active', 'user_intent': 'continue'})
                    self.store.put(db, 'continuation_groups', {**unused, 'state': 'superseded', 'user_intent': 'stop'})
            runs = []
            for reference_id, pages, extra in scopes:
                run = self.store.put(db, 'runs', {'parent_id': chain['id'], 'task_id': task_id,
                    'state': 'queued', 'purpose': purpose, 'generation': 1, 'config': config,
                    'verification_policy':NEW_VERIFICATION_POLICY,
                    'reference_version_id': reference_id, 'submitted_at': now(), 'recovery_count': 0,
                    'group_ids': data.get('group_ids', []), 'reuse_transcription': bool(data.get('reuse_transcription')),
                    'scope_slot': data.get('slot'), 'quota_before': (account or {}).get('rate_limits'),
                    'prompt_ids': {p: prompt_record(p)['id'] for p in ('reference','student','writing','student_verify')},
                    'seq': self.store.sequence(db, 'runs'), **extra})
                if purpose == 'reference':
                    self.store.put(db, 'work_items', {'parent_id': run['id'], 'task_id': task_id,
                        'seq': 1, 'state': 'pending', 'purpose': purpose, **extra})
                for index, page in enumerate(pages):
                    self.store.put(db, 'work_items', {'parent_id': run['id'], 'task_id': task_id,
                        'group_id': page['group_id'], 'seq': index + 1, 'state': 'pending', 'purpose': purpose,
                        'page_id': page['id'], 'image_version_id': page['image_version_id'],
                        'generation': page['generation'], 'reference_version_id': reference_id,
                        'preparation': page['preparation'], 'dependency_versions': page.get('dependency_versions', {})})
                self.store.event(db, 'run.queued', run, run_id=run['id'], purpose=purpose,
                    page_count=len(pages), config=config, reference_version_id=reference_id)
                runs.append(run)
            return self.store.remember(db, data['request_id'], data, {'runs': runs, 'continuation_group_id': chain['id']})

    def has_inflight(self, db, run_id):
        """Paused runs can still own a sibling request when another worker fails."""
        return bool(db.execute("SELECT 1 FROM work_items WHERE parent_id=? AND state='running' LIMIT 1", (run_id,)).fetchone()
            or db.execute("SELECT 1 FROM attempts a JOIN work_items w ON a.parent_id=w.id WHERE w.parent_id=? AND a.state='running' LIMIT 1", (run_id,)).fetchone())

    def recover(self, allow=True):
        with self.store.connection(write=True) as db:
            for run in self.store.list(db, 'runs'):
                was_paused = run['state'] == 'paused'
                if run['state'] not in ('queued', 'running', 'interrupted') and not (was_paused and self.has_inflight(db, run['id'])):
                    continue
                chain = self.store.get(db, 'continuation_groups', run['parent_id'])
                try:
                    self.service.require_available(db, 'tasks', run['task_id'])
                    for g in run.get('group_ids', []):
                        self.service.require_available(db, 'groups', g)
                    for prompt_id in run.get('prompt_ids', {}).values():
                        validate_output_schema(self.store.get(db, 'prompt_versions', prompt_id)['schema'])
                    available = True
                except Problem:
                    available = False
                if run['purpose'] == 'reference' and run['state'] == 'interrupted':
                    draft = self.store.get(db, 'reference_drafts', run['task_id'])
                    available = available and draft['asset_ids'] == run['asset_ids'] and draft['revision'] == run.get('paused_draft_revision')
                recoverable = not was_paused and available and allow and chain.get('user_intent') == 'continue' and run.get('recovery_count', 0) < 3 and bool(run.get('prompt_ids'))
                if run['purpose'] == 'reference':
                    draft = self.store.get(db, 'reference_drafts', run['task_id'])
                    if recoverable and draft['state'] != 'parsing':
                        draft = self.store.put(db, 'reference_drafts', {**draft, 'state': 'parsing'})
                        run['draft_revision'] = draft['revision']
                    elif not recoverable and draft['state'] == 'parsing' and draft['revision'] == run['draft_revision']:
                        self.store.put(db, 'reference_drafts', {**draft, 'state': 'draft'})
                run = self.store.put(db, 'runs', {**run, 'state': 'queued' if recoverable else 'paused',
                    'generation': run['generation'] + 1, 'recovery_count': run.get('recovery_count', 0) + 1})
                for item in self.store.list(db, 'work_items', parent_id=run['id']):
                    if item['state'] == 'running':
                        self.store.put(db, 'work_items', {**item, 'state': 'pending'})
                        if item.get('page_id'):
                            page = self.store.get(db, 'pages', item['page_id'])
                            if page['state'] == 'running' and page['generation'] == item.get('generation'):
                                self.store.put(db, 'pages', {**page, 'state':'pending'})
                    for attempt in self.store.list(db, 'attempts', parent_id=item['id']):
                        if attempt['state'] == 'running':
                            self.store.put(db, 'attempts', {**attempt, 'state': 'interrupted', 'ended_at': now(),
                                'error_code': 'PROCESS_INTERRUPTED', 'elapsed_ms': None})
                self.store.event(db, 'run.recovered' if recoverable else 'run.recovery_paused', run, run_id=run['id'])

    async def stop(self, run_id, reason='user_stopped', interrupt=True):
        with self.store.connection(write=True) as db:
            run = self.store.get(db, 'runs', run_id)
            chain = self.store.get(db, 'continuation_groups', run['parent_id'])
            unexpected = reason in ('ui_connection_lost', 'ui_heartbeat_timeout', 'parent_lost')
            self.store.put(db, 'continuation_groups', {**chain, 'state': 'interrupted' if unexpected else 'paused', 'user_intent': 'continue' if unexpected else 'stop'})
            for sibling in self.store.list(db, 'runs', parent_id=chain['id']):
                was_paused = sibling['state'] == 'paused'
                if sibling['state'] not in (('queued', 'running') if unexpected else ('queued', 'running', 'paused')) and not (was_paused and self.has_inflight(db, sibling['id'])):
                    continue
                self.store.put(db, 'runs', {**sibling, 'state': 'interrupted' if unexpected and not was_paused else 'paused', 'generation': sibling['generation'] + 1,
                    'stopped_at': now(), 'stop_reason': reason,
                    'paused_draft_revision': self.store.get(db, 'reference_drafts', sibling['task_id'])['revision'] + 1 if sibling['purpose'] == 'reference' else None})
                for item in self.store.list(db, 'work_items', parent_id=sibling['id']):
                    if item['state'] == 'running':
                        self.store.put(db, 'work_items', {**item, 'state': 'pending'})
                        if item.get('page_id'):
                            page = self.store.get(db, 'pages', item['page_id'])
                            if page['state']=='running' and page['generation']==item.get('generation'):
                                self.store.put(db, 'pages', {**page, 'state': 'pending'})
                    for attempt in self.store.list(db, 'attempts', parent_id=item['id']):
                        if attempt['state']=='running':
                            self.store.put(db,'attempts',{**attempt,'state':'interrupted','ended_at':now(),
                                'error_code':'PROCESS_INTERRUPTED','elapsed_ms':None})
                if sibling['purpose'] == 'reference':
                    draft = self.store.get(db, 'reference_drafts', sibling['task_id'])
                    if draft['state'] == 'parsing':
                        self.store.put(db, 'reference_drafts', {**draft, 'state': 'draft'})
                self.store.event(db, 'run.' + reason, sibling, run_id=sibling['id'])
        if interrupt:
            with self.store.connection() as db:
                run_ids = {r['id'] for r in self.store.list(db, 'runs', parent_id=chain['id'])}
            await self.interrupt_active(run_ids=run_ids)

    def resume(self, run_id, new_config=None):
        with self.store.connection(write=True) as db:
            require(not self.closing, 'APPLICATION_CLOSING', '程序正在退出，未恢复云端任务；请重新打开后继续。', 409)
            run = self.store.get(db, 'runs', run_id)
            items = self.store.list(db, 'work_items', parent_id=run_id)
            require(run['state'] == 'paused' or run['state'] == 'completed' and any(w['state'] == 'failed' for w in items),
                    'RUN_NOT_PAUSED', '此轮次没有可继续的失败页面。')
            require(not self.has_inflight(db, run_id), 'RUN_DRAINING', '本轮仍在收尾，请稍后继续；已取得的结果会保留。', 409)
            self.service.require_available(db, 'tasks', run['task_id'])
            for group_id in run.get('group_ids', []):
                self.service.require_available(db, 'groups', group_id)
            require(not any(r['state'] in ('queued','running') for r in self.store.list(db, 'runs', task_id=run['task_id'])), 'RUN_ACTIVE', '当前任务已有处理，请等待或停止。', 409)
            for prompt_id in run.get('prompt_ids', {}).values():
                validate_output_schema(self.store.get(db, 'prompt_versions', prompt_id)['schema'])
            if run['purpose'] == 'reference':
                current_draft = self.store.get(db, 'reference_drafts', run['task_id'])
                require(current_draft['asset_ids'] == run['asset_ids'] and current_draft['revision'] == run.get('paused_draft_revision', run['draft_revision'] + 1), 'REFERENCE_CHANGED', '范本已经修改，请在范本页面重新提交新版识别。', 409)
            chain = self.store.get(db, 'continuation_groups', run['parent_id'])
            self.store.put(db, 'continuation_groups', {**chain, 'state': 'active', 'user_intent': 'continue',
                'failure_streak': 0, 'failure_code': None})
            # Older versions completed a page with synthetic unknown writing
            # after another page paused the run. Resume its missing stage using
            # the durable first-pass checkpoint, without overwriting teacher work.
            for item in items:
                if not item.get('page_id'):
                    continue
                page = self.store.get(db, 'pages', item['page_id'])
                observation = self.store.get(db, 'observations', page.get('observation_id') or '', optional=True)
                unfinished_writing = item['state'] == 'completed' and observation and any(
                    w.get('evidence') == '书写检查未完成，请教师核实。' for w in observation.get('writing', []))
                if item['state'] == 'failed' or unfinished_writing:
                    if (page['generation'] != item['generation'] or page['image_version_id'] != item['image_version_id']
                        or page.get('reviewed') or page.get('overrides') or page.get('history') or page.get('annotations')):
                        continue
                    self.store.put(db, 'work_items', {**item, 'state': 'pending', 'error_code': None, 'error_message': None})
                    self.store.put(db, 'pages', {**page, 'state': 'pending'})
            if new_config and new_config != run['config']:
                successor = self.store.put(db, 'runs', {**run, 'id': uid(), 'state': 'queued', 'config': new_config,
                    'generation': 1, 'recovery_count': 0, 'started_at': None, 'ended_at': None,
                    'submitted_at': now(), 'continues_run_id': run_id, 'seq': self.store.sequence(db, 'runs')})
                for item in self.store.list(db, 'work_items', parent_id=run_id):
                    if item['state'] == 'pending':
                        self.store.put(db, 'work_items', {**item, 'id': uid(), 'parent_id': successor['id']})
                        self.store.put(db, 'work_items', {**item, 'state': 'superseded'})
                self.store.put(db, 'runs', {**run, 'state': 'superseded', 'ended_at': now()})
                run = successor
            if run['purpose'] == 'reference':
                draft = self.store.get(db, 'reference_drafts', run['task_id'])
                draft = self.store.put(db, 'reference_drafts', {**draft, 'state': 'parsing'})
                run['draft_revision'] = draft['revision']
                run['asset_ids'] = draft['asset_ids']
            result = self.store.put(db, 'runs', {**run, 'state': 'queued', 'hidden': False, 'recovery_count': 0,
                'failure_streak': 0, 'failure_code': None, 'error_code': None, 'error_message': None, 'ended_at': None})
            self.store.event(db, 'run.resumed', result, run_id=run['id'])
            return result

    async def rpc(self, method, *args, **kwargs):
        call=getattr(self.provider,method)
        if self.rpc_loop is None or asyncio.get_running_loop() is self.rpc_loop:
            return await call(*args,**kwargs)
        future=asyncio.run_coroutine_threadsafe(call(*args,**kwargs),self.rpc_loop)
        self.bridge_jobs.add(future)
        owned = _bridges.get()
        if owned is not None: owned.add(future)
        try:return await asyncio.wrap_future(future)
        finally:
            self.bridge_jobs.discard(future)
            if owned is not None: owned.discard(future)

    async def offload(self, method, *args, **kwargs):
        self.rpc_loop=asyncio.get_running_loop()
        owned = set()
        token = _bridges.set(owned)
        future=asyncio.create_task(asyncio.to_thread(lambda:asyncio.run(method(*args,**kwargs))))
        _bridges.reset(token)
        try:return await asyncio.shield(future)
        except asyncio.CancelledError:
            for job in tuple(owned):job.cancel()
            await asyncio.gather(future,return_exceptions=True)
            raise

    def start(self):
        self.rpc_loop=asyncio.get_running_loop()
        self.job = asyncio.create_task(self.loop())

    async def close(self):
        self.closing = True
        await self.interrupt_active()
        if self.job:
            self.job.cancel()
            await asyncio.gather(self.job, return_exceptions=True)

    async def loop(self):
        # Worker slots are fixed and bounded; each atomic claim enforces the
        # frozen per-run and global limit, including first/second/writing passes.
        workers = [asyncio.create_task(self.worker(slot)) for slot in range(MAX_PARALLELISM * 2)]
        try:
            await asyncio.gather(*workers)
        finally:
            for worker in workers: worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    async def worker(self, slot=0):
        while not self.closing:
            try:
                if slot >= self.worker_capacity:
                    await asyncio.sleep(.5)
                    continue
                processed = await self.offload(self.tick)
                if not processed:
                    await asyncio.sleep(.5)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await asyncio.to_thread(self.service.diagnostics.record,'WORKER_ERROR', str(error), component='queue')
                await asyncio.sleep(1)

    async def tick(self):
        if getattr(self, 'maintenance', False):
            return False
        with self.store.connection(write=True) as db:
            all_runs = self.store.list(db, 'runs')
            runs = [r for r in all_runs if r['state'] in ('queued', 'running')]
            def lane(run):
                return 'capture' if run.get('source') == 'stream_capture' else 'batch'
            def parallel_limit(run):
                return max(1, min(MAX_PARALLELISM, int(run.get('config', {}).get('parallelism', 1))))
            if not runs:
                self.worker_capacity = 1
                return False
            all_work = self.store.list(db, 'work_items')
            running = [w for w in all_work if w['state'] == 'running']
            # Paused requests can still be in flight. They must retain worker
            # capacity in their own lane, without reducing the other lane's slots.
            capacity_runs = [r for r in all_runs if r in runs or any(w['parent_id']==r['id'] for w in running)]
            self.worker_capacity = max(1, sum(max([0, *(parallel_limit(r) for r in capacity_runs if lane(r) == kind)])
                                             for kind in ('batch', 'capture')))
            selected = None
            for candidate in sorted(runs, key=lambda r: r.get('seq', 0)):
                work = [w for w in all_work if w['parent_id'] == candidate['id']]
                pending = [w for w in work if w['state'] == 'pending']
                in_flight = [w for w in work if w['state'] == 'running']
                if not pending and not in_flight:
                    self.store.put(db, 'runs', {**candidate, 'state': 'completed', 'ended_at': now()})
                    self.store.event(db, 'run.completed', candidate, run_id=candidate['id'])
                    continue
                lane_runs = [r for r in all_runs if lane(r) == lane(candidate)]
                lane_ids = {r['id'] for r in lane_runs}
                lane_running = [w for w in running if w['parent_id'] in lane_ids]
                active_limits = [parallel_limit(r) for r in lane_runs
                                 if any(w['parent_id'] == r['id'] for w in lane_running)]
                limit = parallel_limit(candidate)
                if not pending or len(lane_running) >= min([limit, *active_limits]):
                    continue
                selected = candidate, pending
                break
            if selected is None:
                return False
            run, items = selected
            try:
                self.service.require_available(db, 'tasks', run['task_id'])
                for g in run.get('group_ids', []):
                    self.service.require_available(db, 'groups', g)
            except Problem:
                self.store.put(db, 'runs', {**run, 'state': 'paused', 'error_code': 'OBJECT_DELETED'})
                return True
            item = items[0]
            run = self.store.put(db, 'runs', {**run, 'state': 'running', 'started_at': run.get('started_at') or now()})
            item = self.store.put(db, 'work_items', {**item, 'state': 'running'})
            if item.get('page_id'):
                page = self.store.get(db, 'pages', item['page_id'])
                self.store.put(db, 'pages', {**page, 'state': 'running'})
        self.active_work[item['id']] = run['id']
        try:
            await self.process(run, item)
            with self.store.connection(write=True) as db:
                current = self.store.get(db, 'runs', run['id'])
                work = self.store.get(db, 'work_items', item['id'])
                if current['generation'] == run['generation'] and work['state'] == 'completed':
                    self.store.put(db, 'runs', {**current, 'failure_streak': 0, 'failure_code': None})
                    chain = self.store.get(db, 'continuation_groups', run['parent_id'])
                    self.store.put(db, 'continuation_groups', {**chain, 'failure_streak': 0, 'failure_code': None})
        except asyncio.CancelledError:
            raise
        except Exception as error:
            code = error.code if isinstance(error, Problem) else 'PROCESS_ERROR'
            interrupt_runs = set()
            with self.store.connection(write=True) as db:
                current = self.store.get(db, 'runs', run['id'])
                work = self.store.get(db, 'work_items', item['id'])
                if work.get('capture_withdrawn'):
                    self.store.event(db, 'result.withdrawn', work, run_id=run['id'])
                    return True
                if current['generation'] != run['generation']:
                    # A stop fence owns the current work/page state. Late errors
                    # must not restart it or count toward the next generation.
                    return True
                isolated = bool(item.get('page_id')) and code not in GLOBAL_FAILURES
                chain = self.store.get(db, 'continuation_groups', run['parent_id'])
                streak = chain.get('failure_streak', 0) + 1 if chain.get('failure_code') == code else 1
                tripped = isolated and streak >= 3
                if isolated:
                    self.store.put(db, 'runs', {**current, 'failure_code': code, 'failure_streak': streak})
                    self.store.put(db, 'continuation_groups', {**chain, 'failure_code': code, 'failure_streak': streak})
                if not isolated:
                    from .telemetry import sanitize
                    self.store.put(db, 'runs', {**current, 'state': 'paused', 'error_code': code,
                        'error_message': str(error), 'error_details': sanitize({'provider_error': error.details.get('provider_error')} if isinstance(error, Problem) else {}),
                        'paused_draft_revision': self.store.get(db, 'reference_drafts', run['task_id'])['revision'] + 1 if run['purpose'] == 'reference' else None})
                if work['state'] == 'running':
                    self.store.put(db, 'work_items', {**work, 'state': 'failed' if isolated else 'pending',
                        'error_code': code, 'error_message': str(error)})
                if run['purpose'] == 'reference':
                    draft = self.store.get(db, 'reference_drafts', run['task_id'])
                    if draft['state'] == 'parsing' and draft['revision'] == run['draft_revision']:
                        self.store.put(db, 'reference_drafts', {**draft, 'state': 'draft'})
                if item.get('page_id'):
                    page = self.store.get(db, 'pages', item['page_id'])
                    if page['state'] == 'running':
                        self.store.put(db, 'pages', {**page, 'state': 'failed' if isolated else 'pending'})
                if tripped:
                    message = f'同类错误 {code} 连续发生 {streak} 次，本轮已终止。照片和已完成结果已保留；请检查原因后手动继续。'
                    chain = self.store.get(db, 'continuation_groups', run['parent_id'])
                    self.store.put(db, 'continuation_groups', {**chain, 'state': 'paused', 'user_intent': 'stop'})
                    for sibling in self.store.list(db, 'runs', parent_id=chain['id']):
                        if sibling['state'] not in ('queued', 'running', 'paused'):
                            continue
                        interrupt_runs.add(sibling['id'])
                        self.store.put(db, 'runs', {**sibling, 'state': 'paused', 'generation': sibling['generation'] + 1,
                            'error_code': 'REPEATED_PAGE_ERROR', 'error_message': message,
                            'error_details': {'cause': code, 'consecutive_pages': streak}, 'stop_reason': 'repeated_page_error'})
                        for pending in self.store.list(db, 'work_items', parent_id=sibling['id']):
                            if pending['state'] == 'running':
                                self.store.put(db, 'work_items', {**pending, 'state': 'pending'})
                                p = self.store.get(db, 'pages', pending['page_id'])
                                if p['state'] == 'running':
                                    self.store.put(db, 'pages', {**p, 'state': 'pending'})
                    for session in self.store.list(db, 'capture_sessions', task_id=run['task_id']):
                        if session['continuation_group_id'] == chain['id'] and session['state'] == 'active':
                            self.store.put(db, 'capture_sessions', {**session, 'state': 'stopped', 'stop_reason': message})
                    self.store.event(db, 'run.error_limit_stopped', current, run_id=run['id'], cause=code, count=streak)
            if interrupt_runs:
                try:
                    await asyncio.wait_for(self.interrupt_active(run_ids=interrupt_runs), 3)
                except asyncio.TimeoutError:
                    self.service.diagnostics.record('INTERRUPT_TIMEOUT', '终止请求超时；本地写入围栏已生效。', component='queue', run_id=run['id'])
            self.service.diagnostics.record(code, str(error), component='inference', run_id=run['id'],
                                            work_item_id=item['id'], attempt_id=next((a for a,v in tuple(self.active_attempts.items()) if v['work_id']==item['id']),None),
                                            details={'provider_error': error.details.get('provider_error')} if isinstance(error, Problem) else {})
        finally:
            self.active_work.pop(item['id'], None)
        return True

    def valid(self, db, run, item):
        current = self.store.get(db, 'runs', run['id'])
        try:
            self.service.require_available(db, 'tasks', run['task_id'])
            if item.get('group_id'):
                self.service.require_available(db, 'groups', item['group_id'])
        except Problem:
            return False
        # An error in one worker pauses new dispatch, but in-flight siblings may finish.
        # Explicit stop/restart advances generation and therefore still rejects late results.
        live = current['state'] == 'running' or (current['state'] == 'paused' and current.get('error_code'))
        if not live or current['generation'] != run['generation']:
            return False
        if item.get('page_id'):
            page = self.store.get(db, 'pages', item['page_id'])
            if page['state'] == 'deleted' or self.store.get(db, 'work_items', item['id'])['state'] == 'superseded':
                return False
            for identifier, version in item.get('dependency_versions', {}).items():
                dependency = self.store.get(db, 'pages', identifier)
                if dependency['state'] == 'deleted' or dependency['image_version_id'] != version:
                    return False
            return (page['image_version_id'] == item['image_version_id'] and page['generation'] == item['generation']
                    and page['reference_version_id'] == item['reference_version_id'] and not page.get('reviewed'))
        draft = self.store.get(db, 'reference_drafts', run['task_id'])
        return draft['revision'] == run['draft_revision'] and draft['state'] == 'parsing'

    async def call(self, run, item, purpose, paths, variables):
        try:
            return await self._call(run, item, purpose, paths, variables)
        finally:
            for identifier, value in tuple(self.active_attempts.items()):
                if value['work_id'] == item['id']:
                    self.active_attempts.pop(identifier, None)

    async def quota_observation(self, profile_id):
        if not hasattr(self.provider, 'quota'):
            return None
        try:
            return await asyncio.wait_for(self.rpc('quota', profile_id), 5)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.service.diagnostics.record('QUOTA_OBSERVATION_UNAVAILABLE', str(error), component='usage')
            return None

    def require_dispatch(self, db, run, item):
        require(self.valid(db, run, item), 'STALE_WORK', '状态已变化，此次提交取消。', 409)
        require(self.store.get(db, 'runs', run['id'])['state'] == 'running', 'QUEUE_PAUSED',
                '队列已暂停，保留已取得的识别内容，未继续发送图片。', 409)

    async def _call(self, run, item, purpose, paths, variables):
        config = {**run['config']}
        if purpose == 'student_verify': config['effort'] = config.get('verification_effort', 'medium')
        from .power import power
        while power.suspended and not self.closing:
            await asyncio.sleep(1)
        with self.store.connection(write=True) as db:
            require(run.get('prompt_ids', {}).get(purpose), 'RUN_VERSION_CHANGED', '旧版队列需重新核对图片并创建处理轮次。')
            prompt = self.store.get(db, 'prompt_versions', run['prompt_ids'][purpose])
            require(self.valid(db, run, item), 'STALE_WORK', '状态已变化，此次提交取消。', 409)
            # Recover a validated durable checkpoint without charging for a second invocation.
            candidates = [c for c in self.store.list(db, 'checkpoints', task_id=run['task_id'])
                          if c.get('work_item_id') == item['id'] and c.get('purpose') == purpose
                          and c.get('eligible', False)
                          and c.get('prompt_hash') == prompt['prompt_hash']
                          and c.get('config_hash') == digest(config)
                          and c.get('paths_hash') == digest([{'path': str(p), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in paths])
                          and c.get('image_version_id') == item.get('image_version_id')
                          and c.get('reference_version_id') == run.get('reference_version_id')
                          and c.get('variables_hash') == digest(variables)]
            if candidates:
                checkpoint = candidates[-1]
                self.store.put(db, 'recovery_adoptions', {'parent_id': checkpoint['id'], 'task_id': run['task_id'],
                    'run_id': run['id'], 'work_item_id': item['id'], 'run_generation': run['generation']})
                return checkpoint['result'], checkpoint['attempt_id']
            # A prior strict decoder may have rejected only optional boxes. On
            # explicit resume, salvage that exact frozen response locally. Keep
            # the failed attempt/usage immutable and record a separate adoption.
            from .output_safety import decode_response
            image_hashes = [{'sha256': hashlib.sha256(p.read_bytes()).hexdigest(), 'bytes': p.stat().st_size} for p in paths]
            for failed in reversed(self.store.list(db, 'attempts', parent_id=item['id'])):
                if not (failed.get('error_code') == 'OUTPUT_SCHEMA' and failed.get('purpose') == purpose
                        and failed.get('prompt_hash') == prompt['prompt_hash'] and failed.get('config') == config
                        and failed.get('variables_hash') == digest(variables) and failed.get('input_images') == image_hashes
                        and failed.get('image_version_id') == item.get('image_version_id')
                        and failed.get('reference_version_id') == run.get('reference_version_id')):
                    continue
                raw_path = self.store.root / 'attempt-artifacts' / failed['id'] / 'invalid-output.txt'
                try:
                    parsed, warnings = decode_response(purpose, prompt['schema_version'], raw_path.read_text(encoding='utf-8'))
                except (OSError, ValueError, TypeError, KeyError):
                    continue
                if not warnings:
                    continue
                result = {'output': parsed, 'geometry_warnings': warnings, 'recovered_raw_attempt_id': failed['id']}
                checkpoint = self.store.put(db, 'checkpoints', {'parent_id': failed['id'], 'task_id': run['task_id'],
                    'attempt_id': failed['id'], 'work_item_id': item['id'], 'purpose': purpose,
                    'image_version_id': item.get('image_version_id'), 'reference_version_id': run.get('reference_version_id'),
                    'variables_hash': digest(variables), 'result': result, 'eligible': True,
                    'prompt_hash': prompt['prompt_hash'], 'config_hash': digest(config),
                    'paths_hash': digest([{'path': str(p), 'sha256': h['sha256']} for p, h in zip(paths, image_hashes)])})
                self.store.put(db, 'recovery_adoptions', {'parent_id': checkpoint['id'], 'task_id': run['task_id'],
                    'run_id': run['id'], 'work_item_id': item['id'], 'run_generation': run['generation'],
                    'reason': 'invalid_optional_box', 'geometry_warnings': warnings})
                self.store.event(db, 'attempt.geometry_recovered', failed, run_id=run['id'], attempt_id=failed['id'], count=len(warnings))
                return result, failed['id']
            self.require_dispatch(db, run, item)
            attempt = self.store.put(db, 'attempts', {'parent_id': item['id'], 'task_id': run['task_id'],
                'group_id': item.get('group_id'), 'state': 'running', 'run_id': run['id'], 'purpose': purpose,
                'page_id': item.get('page_id'), 'image_version_id': item.get('image_version_id'),
                'reference_version_id': run.get('reference_version_id'), 'run_generation': run['generation'],
                'config': config, 'prompt_version_id': prompt['id'], 'prompt_hash': prompt['prompt_hash'],
                'schema_hash': prompt['schema_hash'], 'schema_version': prompt['schema_version'],
                'preprocessing_version': prompt['preprocessing_version'], 'grading_rule_version': prompt['grading_rule_version'],
                'variables_hash': digest(variables), 'input_images': [{'sha256': hashlib.sha256(p.read_bytes()).hexdigest(), 'bytes': p.stat().st_size} for p in paths], 'started_at': now(), 'usage': None,
                'actual_model': None, 'actual_service_tier': None})
            self.store.event(db, 'attempt.started', attempt, run_id=run['id'], attempt_id=attempt['id'], purpose=purpose)
        self.active_attempts[attempt['id']] = {'run_id':run['id'], 'work_id':item['id'], 'page_id':item.get('page_id')}
        before = await self.quota_observation(run['config']['profile_id'])
        with self.store.connection(write=True) as db:
            current = self.store.get(db, 'attempts', attempt['id'])
            self.store.put(db, 'attempts', {**current, 'quota_before': before})
            if before is not None:
                self.store.put(db, 'quota_snapshots', {'task_id': run['task_id'], 'attempt_id': attempt['id'],
                    'profile_id': run['config']['profile_id'], 'position': 'before', 'snapshot': before})
        started = time.perf_counter()
        def on_started(thread_id, turn_id):
            with self.store.connection(write=True) as db:
                current = self.store.get(db, 'attempts', attempt['id'])
                self.store.put(db, 'attempts', {**current, 'thread_id': thread_id, 'turn_id': turn_id})
        def on_progress(phase, event):
            with self.store.connection(write=True) as db:
                current = self.store.get(db, 'runs', run['id'])
                if current['state'] not in ('running', 'queued') or current['generation'] != run['generation'] or not self.valid(db, run, item):
                    return
                progress = {**current.get('progress', {}), 'phase': phase, 'attempt_id': attempt['id'], 'page_id': item.get('page_id'), 'updated_at': now()}
                if event: progress.update(last_cloud_event_at=now(), last_event=event)
                self.store.put(db, 'runs', {**current, 'progress': progress})
                work = self.store.get(db, 'work_items', item['id'])
                self.store.put(db, 'work_items', {**work, 'progress': {**progress, 'purpose': purpose}})
        try:
            with self.store.connection() as db:
                require(self.valid(db, run, item), 'TURN_INTERRUPTED', '本次提交已终止。', 409)
                self.require_dispatch(db, run, item)
            import inspect
            kwargs = {'on_progress': on_progress} if 'on_progress' in inspect.signature(self.provider.infer).parameters else {}
            on_progress('preparing', None)
            result = await self.rpc('infer',attempt['id'], purpose, paths, prompt, variables, config, on_started, **kwargs)
        except BaseException as error:
            failure_elapsed = round((time.perf_counter() - started) * 1000)
            failure_quota = None
            if not isinstance(error, asyncio.CancelledError) and hasattr(self.provider, 'quota'):
                failure_quota = await self.quota_observation(run['config']['profile_id'])
            with self.store.connection(write=True) as db:
                current = self.store.get(db, 'attempts', attempt['id'])
                details = error.details if isinstance(error, Problem) else {}
                raw_path = None
                if details.get('raw_output') is not None:
                    raw_path = f'attempt-artifacts/{attempt["id"]}/invalid-output.txt'
                    atomic_write(self.store.root / raw_path, details['raw_output'].encode('utf-8'))
                self.store.put(db, 'attempts', {**current, 'state': 'interrupted' if isinstance(error, asyncio.CancelledError) or
                    (isinstance(error, Problem) and error.code in ('TURN_INTERRUPTED', 'STALE_WORK')) else 'failed',
                    'ended_at': now(), 'elapsed_ms': failure_elapsed, 'quota_after': failure_quota,
                    'error_code': error.code if isinstance(error, Problem) else type(error).__name__, 'usage': details.get('usage'),
                    'raw_output_relative_path': raw_path,
                    'validation_issues': details.get('validation_issues', []),
                    'error_message': str(error), 'provider_error': sanitize(details.get('provider_error'))})
                if failure_quota is not None:
                    self.store.put(db, 'quota_snapshots', {'task_id': run['task_id'], 'attempt_id': attempt['id'],
                        'profile_id': run['config']['profile_id'], 'position': 'after_failure', 'snapshot': failure_quota})
                self.store.event(db, 'attempt.failed', current, run_id=run['id'], attempt_id=attempt['id'])
            raise
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        # Persist successful result/checkpoint before optional account telemetry.
        after = None
        with self.store.connection(write=True) as db:
            current = self.store.get(db, 'attempts', attempt['id'])
            self.store.put(db, 'attempts', {**current, 'state': 'completed', 'ended_at': now(),
                'elapsed_ms': elapsed_ms, 'quota_after': after, 'geometry_warnings': result.get('geometry_warnings', []),
                **{k: result.get(k) for k in ('usage', 'thread_id', 'turn_id', 'actual_model', 'actual_service_tier')}})
            self.store.put(db, 'checkpoints', {'parent_id': attempt['id'], 'task_id': run['task_id'],
                'attempt_id': attempt['id'], 'work_item_id': item['id'], 'purpose': purpose,
                'image_version_id': item.get('image_version_id'), 'reference_version_id': run.get('reference_version_id'),
                'variables_hash': digest(variables), 'result': result, 'eligible': self.valid(db, run, item),
                'prompt_hash': prompt['prompt_hash'], 'config_hash': digest(config),
                'paths_hash': digest([{'path': str(p), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in paths])})
            if after is not None:
                self.store.put(db, 'quota_snapshots', {'task_id': run['task_id'], 'attempt_id': attempt['id'],
                    'profile_id': run['config']['profile_id'], 'position': 'after', 'snapshot': after})
            self.store.event(db, 'attempt.completed', current, run_id=run['id'], attempt_id=attempt['id'],
                             elapsed_ms=elapsed_ms, usage=result.get('usage'))
        after = await self.quota_observation(run['config']['profile_id'])
        if after is not None:
            with self.store.connection(write=True) as db:
                current = self.store.get(db, 'attempts', attempt['id'])
                self.store.put(db, 'attempts', {**current, 'quota_after':after})
                self.store.put(db, 'quota_snapshots', {'task_id':run['task_id'], 'attempt_id':attempt['id'],
                    'profile_id':config['profile_id'], 'position':'after', 'snapshot':after})
        return result, attempt['id']

    async def process(self, run, item):
        if run['purpose'] == 'reference':
            paths = [self.service.asset(a) for a in run['asset_ids']]
            result, attempt_id = await self.call(run, item, 'reference', paths, {'page_count': len(paths), 'page_order': list(range(1, len(paths) + 1)), 'scoring_enabled':run.get('scoring_enabled',False), 'scoring_only':run.get('scoring_only',False), 'rule_text':run.get('rule_text',''), 'structure':run.get('score_structure',[]) if run.get('scoring_only') else []})
            with self.store.connection(write=True) as db:
                if not self.valid(db, run, item):
                    self.store.event(db, 'result.stale', item, run_id=run['id'], attempt_id=attempt_id)
                    return
                draft = self.store.get(db, 'reference_drafts', run['task_id'])
                output = dict(result['output'])
                scoring = output.pop('scoring', None)
                candidates = [{**a, 'id': uid(), 'geometry_checked': False} for a in output['items']]
                if run.get('scoring_enabled') and scoring:
                    from .scoring import candidate
                    target_items = draft['items'] if run.get('scoring_only') else candidates
                    prior=self.store.get(db,'scoring_drafts',run['task_id'],optional=True)
                    candidate_score=candidate(scoring,target_items,attempt_id)
                    if prior:
                        self.store.put(db,'scoring_drafts',{**prior,'machine_candidate':candidate_score})
                    else:
                        self.store.put(db,'scoring_drafts',{'id':run['task_id'],'parent_id':run['task_id'],'task_id':run['task_id'],**candidate_score})
                if run.get('scoring_only'):
                    self.store.put(db,'reference_drafts',{**draft,'state':'draft'})
                    self.store.put(db,'work_items',{**item,'state':'completed'})
                    return
                self.store.put(db, 'reference_drafts', {**draft, **output, 'state': 'draft',
                    'items': draft['items'] if draft.get('items') else candidates,
                    'machine_candidates': candidates if draft.get('items') else [],
                    'issues': output.get('issues', []) + (['重新解析候选已保留；现有教师草稿未覆盖，请逐项采用候选。'] if draft.get('items') else []),
                    'parse_attempt_id': attempt_id})
                self.store.put(db, 'work_items', {**item, 'state': 'completed'})
            return
        with self.store.connection() as db:
            page = self.store.get(db, 'pages', item['page_id'])
            version = self.store.get(db, 'image_versions', page['image_version_id'])
            reference = self.store.get(db, 'reference_versions', run['reference_version_id'])
            previous = self.store.get(db, 'observations', page['observation_id']) if page.get('observation_id') else None
        paths = [self.service.asset(version['asset_id'])]
        verification_pages = [{'slot': page['slot'], 'image_version_id': version['id'], 'asset_id': version['asset_id']}]
        page_order = [page['slot']]
        mappings = {page['slot']: item.get('preparation', {}).get('mapping', {}).get('matrix', [[1,0,0],[0,1,0],[0,0,1]])}
        with self.store.connection() as db:
            for dependency_id, dependency_version in item.get('dependency_versions', {}).items():
                dep = self.store.get(db, 'pages', dependency_id)
                if dep['id'] == page['id']:
                    continue
                dep_image = self.store.get(db, 'image_versions', dependency_version)
                verification_pages.append({'slot':dep['slot'], 'image_version_id':dep_image['id'], 'asset_id':dep_image['asset_id']})
                paths.append(self.service.asset(dep_image['asset_id']))
                page_order.append(dep['slot'])
                mappings[dep['slot']] = (dep.get('preparation') or {}).get('mapping', {}).get('matrix', [[1,0,0],[0,1,0],[0,0,1]])
        structure = []
        from .imaging import project, corners
        for answer in reference['items']:
            if answer['slot'] != page['slot'] or answer.get('excluded'):
                continue
            if item.get('retry_question_ids') and answer['id'] not in item['retry_question_ids']:
                continue
            value = {k: answer.get(k) for k in ('id', 'number', 'slot', 'section', 'part_id', 'kind', 'response_encoding', 'selection_rule', 'row', 'column')}
            value['answer_zone'] = item.get('preparation', {}).get('zones', {}).get(answer['id']) or answer.get('answer_zone') or answer.get('box')
            value['box'] = value['answer_zone']
            mapping = item.get('preparation', {}).get('mapping', {}).get('matrix', [[1,0,0],[0,1,0],[0,0,1]])
            def region_copy(node):
                copied = {k: node.get(k) for k in ('id','group','slot','label') if k in node}
                if node.get('box'):
                    polygon = project(corners(node['box']), mappings.get(node.get('slot',page['slot']),mapping))
                    copied['polygon'] = polygon
                    copied['box'] = {'x1': min(p[0] for p in polygon), 'y1': min(p[1] for p in polygon), 'x2': max(p[0] for p in polygon), 'y2': max(p[1] for p in polygon)}
                return copied
            pages = reference.get('page_ids') or reference.get('asset_ids', [])
            value['options'] = [region_copy({**o, 'slot':pages.index(o['page_id'])+1 if o.get('page_id') in pages else answer['slot']}) for o in answer.get('options', [])]
            rule = answer.get('matching_rule') or answer.get('answer_spec', {})
            value['nodes'] = [region_copy(n) for n in rule.get('nodes', [])]
            # Deliberately omit allowed_edges, english, variants and all answer rules.
            structure.append(value)
        import copy
        full_structure = copy.deepcopy(structure)
        crop_map = None
        crop_manifest = None
        if item.get('retry_question_ids'):
            import numpy as np
            boxes = [s['answer_zone'] for s in structure]
            crop = {'x1':max(0,min(b['x1'] for b in boxes)-.04),'y1':max(0,min(b['y1'] for b in boxes)-.04),
                    'x2':min(1,max(b['x2'] for b in boxes)+.04),'y2':min(1,max(b['y2'] for b in boxes)+.04)}
            cropped = self.service.prepare_asset(version['asset_id'], {'operation':{'kind':'crop','box':crop}})
            with self.store.connection() as db:
                source_asset = self.store.get(db,'assets',version['asset_id'])
            crop_map = np.array(source_asset.get('geometry',{}).get('forward',np.eye(3))) @ np.array(cropped['geometry']['inverse'])
            to_crop = np.linalg.inv(crop_map)
            def convert_box(box,matrix):
                if not box:
                    return box
                points = project(corners(box),matrix)
                return {'x1':min(p[0] for p in points),'y1':min(p[1] for p in points),'x2':max(p[0] for p in points),'y2':max(p[1] for p in points)}
            for value in structure:
                value['box'] = value['answer_zone'] = convert_box(value['answer_zone'],to_crop)
                for node in value['nodes'] + value['options']:
                    if node.get('box'):
                        node['box'] = convert_box(node['box'],to_crop)
                    if node.get('polygon'):
                        node['polygon'] = project(node['polygon'],to_crop)
            paths = [self.service.asset(cropped['id'])]
            crop_manifest = {'asset_id':cropped['id'],'source_image_version_id':version['id'],'source_box':crop,'question_ids':item['retry_question_ids']}
        if run.get('reuse_transcription'):
            require(previous, 'TRANSCRIPTION_MISSING', '没有可复用转录。')
            output, attempt_id = {'cells': previous['cells'], 'photo_issues': previous.get('photo_issues', []),
                'verification': previous.get('verification', {}), 'verification_policy': previous.get('verification_policy')}, previous['parent_id']
        else:
            # Compact only redundant metadata, never the pupil evidence or expected answers.
            aliases = {f'q{i+1}': value['id'] for i, value in enumerate(structure)}
            reverse_aliases = {value:key for key,value in aliases.items()}
            compact_structure = []
            for value in structure:
                compact = {k:v for k,v in value.items() if v is not None and k != 'box'}
                compact['id'] = reverse_aliases[value['id']]
                for field in ('options','nodes'):
                    compact[field] = [{k:v for k,v in node.items() if v is not None and k != 'polygon'} for node in value.get(field,[])]
                compact_structure.append(compact)
            result, attempt_id = await self.call(run, item, 'student', paths, {'slot': page['slot'], 'image_page_order': page_order, 'crop_manifest':crop_manifest, 'structure': compact_structure})
            output = result['output']
            geometry_warnings = result.get('geometry_warnings', [])
            if geometry_warnings:
                output['geometry_warnings'] = [{**w, 'question_id': aliases.get(w.get('question_id'), w.get('question_id'))}
                                               for w in geometry_warnings]
            for cell in output.get('cells', []):
                if cell.get('question_id') in aliases: cell['question_id'] = aliases[cell['question_id']]
                if any(w.get('question_id') == cell.get('question_id') for w in output.get('geometry_warnings', [])):
                    fallback = next((a.get('answer_zone') for a in structure if a['id'] == cell['question_id']), None)
                    cell['box'] = fallback
                    if not fallback:
                        cell.update(uncertain=True, unknown_reason='定位框无效且没有已确认作答区，请教师核实。')
            # Question metadata is already known. Expand compact v5.1 output without model tokens.
            for cell in output.get('cells', []):
                meta = next((a for a in structure if a['id'] == cell.get('question_id')), None)
                if meta and 'value' in cell:
                    value=cell.pop('value')
                    cell.update({k:meta.get(k) for k in ('number','slot','section','part_id')})
                    cell['symbol']=value if meta['kind']=='true_false' else None
                    cell['rank']=value if meta['kind']=='ordering' else None
        verification_quota_exhausted = False
        if run['config'].get('conservative_review') and not run.get('reuse_transcription'):
            from . import verification
            # Missing fields belong to pre-v2 runs. Resume/recovery must retain
            # their original grouping and checkpoint identity.
            verification_policy = run.get('verification_policy', LEGACY_POLICY_VERSION)
            with self.store.connection() as db:
                submission = self.store.get(db, 'submissions', page['parent_id'])
                score = self.store.get(db, 'scoring_versions', submission['scoring_version_id']) if submission.get('scoring_version_id') else None
            targets = verification.candidates(reference, output, page['slot'], score)
            second_outputs, rejected = [], []
            if targets:
                try:
                    prepared = verification.prepare(self.service, item['id'], verification_pages, full_structure, targets,
                                                    policy_version=verification_policy)
                    rejected.extend(prepared['rejected'])
                except Exception as error:
                    prepared = {'batches': []}
                    rejected.extend({**t, 'reason':'原始题区无法可靠准备，需教师核实。'} for t in targets)
                    self.service.diagnostics.record('VERIFY_PREPARE_FAILED', str(error), component='verification', work_item_id=item['id'])
                for batch in prepared['batches']:
                    if verification_quota_exhausted:
                        rejected.extend({**t, 'reason':'账户额度不足，独立复看未完成，请教师核实。'} for t in batch['manifest']['targets'])
                        continue
                    try:
                        second, second_id = await self.call(run, item, 'student_verify', batch['paths'], batch['variables'])
                        second_outputs.append({'output':second['output'], 'attempt_id':second_id, 'manifest':batch['manifest']})
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        if not isinstance(error, Problem) or error.code not in ('USAGE_LIMIT', 'QUEUE_PAUSED'):
                            raise
                        with self.store.connection() as db:
                            failures = [a for a in self.store.list(db, 'attempts', parent_id=item['id']) if a.get('purpose') == 'student_verify']
                        rejected.extend({**t, 'reason':'独立复看未完成，未自动放行为正确。',
                            'attempt_id':failures[-1]['id'] if failures else None} for t in batch['manifest']['targets'])
                        if isinstance(error, Problem) and error.code in ('USAGE_LIMIT', 'QUEUE_PAUSED'):
                            verification_quota_exhausted = True
                            with self.store.connection(write=True) as db:
                                current = self.store.get(db, 'runs', run['id'])
                                if current['generation'] == run['generation'] and current['state'] == 'running':
                                    self.store.put(db, 'runs', {**current, 'state':'paused', 'error_code':error.code, 'error_message':str(error)})
                output = verification.merge(reference, output, second_outputs, attempt_id, rejected,
                                            policy_version=verification_policy)
        writing = []
        if run.get('reuse_transcription') and reference.get('writing_rules'):
            # Local regrading never triggers an unconsented cloud writing pass.
            writing = [{'question_id': a['id'], 'status': 'unknown', 'rule_id': reference['writing_rules'][0]['id'],
                        'evidence': '本地重判未重新进行书写视觉检查，请教师确认。', 'box': a.get('box')} for a in structure]
        elif reference.get('writing_rules'):
            try:
                if verification_quota_exhausted:
                    raise Problem('QUEUE_PAUSED', '队列暂停，书写检查未完成。')
                result, _ = await self.call(run, item, 'writing', paths, {'slot': page['slot'], 'structure': structure,
                    'rules': reference['writing_rules']})
                writing = result['output']['items']
            except Problem as error:
                if error.code not in ('USAGE_LIMIT', 'QUEUE_PAUSED'):
                    raise
                # Preserve this page's completed observations while stopping
                # further dispatch. Same-generation in-flight siblings may save.
                with self.store.connection(write=True) as db:
                    current = self.store.get(db, 'runs', run['id'])
                    if current['generation'] == run['generation'] and current['state'] == 'running':
                        self.store.put(db, 'runs', {**current, 'state':'paused',
                            'error_code':error.code, 'error_message':str(error)})
                writing = [{'question_id': a['id'], 'status':'unknown', 'rule_id':rule['id'],
                    'evidence':'书写检查未完成，请教师核实。', 'box':a.get('answer_zone')} for a in structure for rule in reference['writing_rules']]
        if crop_map is not None:
            ids = set(item['retry_question_ids'])
            output['cells'] = [c for c in output['cells'] if c.get('question_id') in ids]
            for cell in output['cells']:
                cell['box'] = convert_box(cell.get('box'),crop_map)
                for edge in cell.get('edges',[]):
                    if edge.get('path'):
                        edge['path'] = project(edge['path'],crop_map)
            for candidate in writing:
                candidate['box'] = convert_box(candidate.get('box'),crop_map)
            output['cells'] = [c for c in previous['cells'] if c.get('question_id') not in ids] + output['cells']
            output['verification'] = {**{q:v for q,v in previous.get('verification', {}).items() if q not in ids},
                                      **output.get('verification', {})}
            output['verification_policy'] = output.get('verification_policy') or previous.get('verification_policy')
            output['photo_issues'] = list(dict.fromkeys(previous.get('photo_issues',[]) + output.get('photo_issues',[])))
            writing = [w for w in previous.get('writing',[]) if w['question_id'] not in ids] + writing
            full_structure = [s for s in previous.get('structure',[]) if s['id'] not in ids] + full_structure
        with self.store.connection(write=True) as db:
            if not self.valid(db, run, item):
                self.store.event(db, 'result.stale', item, run_id=run['id'], attempt_id=attempt_id)
                return
            page = self.store.get(db, 'pages', item['page_id'])
            observation = self.store.put(db, 'observations', {'parent_id': attempt_id, 'task_id': run['task_id'],
                'group_id': page['group_id'], 'page_id': page['id'], 'image_version_id': page['image_version_id'],
                'reference_version_id': run['reference_version_id'], **output, 'writing': writing,
                'structure': full_structure, 'image_page_order': page_order, 'crop_manifest':crop_manifest,
                'previous_observation_id': previous['id'] if previous else None,
                'reused_observation_id': previous['id'] if run.get('reuse_transcription') else None})
            photo_status = 'pending' if output.get('photo_issues') else 'clear'
            self.store.put(db, 'pages', {**page, 'state': 'processed', 'observation_id': observation['id'], 'processed_at': now(),
                'photo_status': photo_status, 'machine_photo_status': photo_status})
            self.store.put(db, 'work_items', {**item, 'state': 'completed', 'observation_id': observation['id']})
            self.service.update_submission(db, page['parent_id'])
            self.store.event(db, 'page.processed', page, run_id=run['id'], attempt_id=attempt_id)
