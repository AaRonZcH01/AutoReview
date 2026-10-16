from __future__ import annotations
import asyncio
import json
import os
import shutil
import sys
from collections import deque
from pathlib import Path
from .core import Problem, require, uid, dumps, now
from .contracts import OUTPUT_MODELS

INFERENCE_TIMEOUT_SECONDS = 600
REFERENCE_MAX_TIMEOUT_SECONDS = 1200
INFERENCE_RESCHEDULE_INTERVAL_SECONDS = 1


def damaged_auth_store(message):
    text = str(message).lower()
    return any(marker in text for marker in ('failed to decrypt secrets file', 'failed to deserialize decrypted secrets file',
                                             'failed to deserialize cli auth', 'secrets file version'))


def remove_profile_tree(home):
    # Official CLI caches can exceed MAX_PATH even when the profile root is short.
    # The caller verifies this absolute target is its own non-link profile first.
    target = str(home.resolve())
    if os.name == 'nt' and not target.startswith('\\\\?\\'):
        target = '\\\\?\\UNC\\' + target[2:] if target.startswith('\\\\') else '\\\\?\\' + target
    def missing_ok(function, path, error):
        if not isinstance(error, FileNotFoundError): raise error
    shutil.rmtree(target, onexc=missing_ok)


class EventInbox:
    """One ephemeral thread's notifications, consumed once by its inference."""
    def __init__(self):
        self.events = deque()
        self.ready = asyncio.Event()
        self.closed = None

    def put(self, event):
        if self.closed is None:
            self.events.append(event)
            self.ready.set()

    def close(self, code='CODEX_DISCONNECTED'):
        self.closed = code
        self.ready.set()

    async def receive(self):
        await self.ready.wait()
        # Already received terminal events and usage must survive immediate EOF.
        if self.events:
            batch = list(self.events)
            self.events.clear()
            if self.closed is None:
                self.ready.clear()
            return batch
        code = self.closed or 'CODEX_DISCONNECTED'
        raise Problem(code, '本次提交已终止。' if code == 'TURN_INTERRUPTED' else 'Codex 连接中断。', 503)


def turn_failure(error, usage=None):
    from .telemetry import sanitize
    error = sanitize(error or {})
    reason = dumps(error)
    code, message = ('REQUEST_SCHEMA_INVALID', '程序请求格式不兼容，请使用新版请求重新识别。') if 'schema' in reason.lower() else ('TURN_FAILED', '识别未完成，请查看错误详情后重试。')
    if 'usageLimitExceeded' in reason:
        code, message = 'USAGE_LIMIT', '当前账户额度暂不可用。任务及已完成结果已保留，请待额度恢复后手动继续。'
    return Problem(code, message, 502, usage=usage, provider_error=error)


def executable(home=None):
    bundled = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[4])) / 'resources' / 'codex.exe'
    override = os.environ.get('AUTOREVIEW_CODEX_EXE')
    if override and Path(override).is_file():
        return override
    from .account_storage import instance_home
    from .cli_updates import active_executable
    managed = active_executable(home or instance_home())
    if managed:
        return managed
    # Frozen releases use their tested runtime. Source mode follows the already
    # installed official CLI so a stale project copy cannot hide new catalogue entries.
    if getattr(sys, 'frozen', False) and bundled.is_file():
        return str(bundled)
    root = Path(os.environ.get('LOCALAPPDATA', '')) / 'OpenAI/Codex/bin'
    options = sorted(root.glob('*/codex.exe'), key=lambda p: p.stat().st_mtime, reverse=True)
    if options:
        return str(options[0])
    found = shutil.which('codex.exe') or shutil.which('codex')
    if found:
        return found
    if bundled.is_file():
        return str(bundled)
    raise Problem('CODEX_NOT_INSTALLED', '未找到官方 Codex CLI，请安装 Codex 或恢复项目运行环境。', 503)


class Rpc:
    def __init__(self, home: Path, executable_path=None):
        self.home = home
        self.executable_override = executable_path
        self.process = None
        self.pending = {}
        # Retain only a small account/server notification history. Inference
        # notifications go directly to their subscriber and are never scanned by
        # other turns or retained for the rest of the application's lifetime.
        self.events = deque(maxlen=64)
        self.subscribers = {}
        self.disconnected = True
        self.condition = asyncio.Condition()
        self.lock = asyncio.Lock()
        self.sequence = 0
        self.reader = None
        self.executable_path = None
        self.server_info = {}

    async def start(self):
        async with self.lock:
            if self.process and self.process.returncode is None and not self.disconnected:
                return
            if self.process:
                await self.close()
            for attempt in range(2):
                try:
                    await self.start_once()
                    return
                except BaseException as error:
                    await self.close()
                    from .cli_updates import rollback
                    profile_home = self.home.parent.parent if self.home.parent.name == 'profiles' else None
                    if not isinstance(error, asyncio.CancelledError) and not attempt and not self.executable_override and profile_home and rollback(profile_home, self.executable_path):
                        continue
                    raise

    async def start_once(self):
        self.home.mkdir(parents=True, exist_ok=True)
        workspace = self.home / 'empty-workspace'
        workspace.mkdir(exist_ok=True)
        from .account_storage import cli_environment, cli_options
        env = cli_environment(self.home)
        # Each profile is created and refreshed by official managed ChatGPT login.
        # Never inspect or copy its credential files, or inherit API-key authentication.
        self.executable_path = self.executable_override or executable(self.home.parent.parent if self.home.parent.name == 'profiles' else None)
        self.process = await asyncio.create_subprocess_exec(self.executable_path,
            *cli_options(self.home),
            'app-server', '--listen', 'stdio://', cwd=workspace, env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=16 * 1024 * 1024,
            creationflags=0x08000000 if os.name == 'nt' else 0)
        self.disconnected = False
        self.events.clear()
        self.reader = asyncio.create_task(self.read(self.process))
        try:
            self.server_info = await self.request('initialize', {'clientInfo': {'name': 'autoreview', 'version': '1.4.2'},
                                              'capabilities': {'experimentalApi': True}}, timeout=30)
            await self.send({'method': 'initialized', 'params': {}})
        except BaseException:
            await self.close()
            raise

    def subscribe(self, thread_id):
        require(not self.disconnected, 'CODEX_DISCONNECTED', 'Codex 连接中断。', 503)
        require(thread_id not in self.subscribers, 'THREAD_IN_USE', '该识别会话已有执行中的请求。', 409)
        inbox = EventInbox()
        self.subscribers[thread_id] = inbox
        return inbox

    def unsubscribe(self, thread_id, inbox):
        if self.subscribers.get(thread_id) is inbox:
            self.subscribers.pop(thread_id, None)
        inbox.close()
        inbox.events.clear()

    def publish(self, event):
        params = event.get('params') or {}
        if not isinstance(params, dict):
            return
        thread_id = params.get('threadId')
        if thread_id is not None:
            inbox = self.subscribers.get(thread_id)
            if inbox is not None:
                inbox.put(event)
            return
        self.events.append(event)

    async def disconnected_now(self):
        self.disconnected = True
        for inbox in tuple(self.subscribers.values()):
            inbox.close()
        for future in tuple(self.pending.values()):
            if not future.done():
                future.set_exception(Problem('CODEX_DISCONNECTED', 'Codex 连接中断。', 503))
        async with self.condition:
            self.condition.notify_all()

    async def send(self, message):
        require(not self.disconnected and self.process and self.process.stdin and self.process.returncode is None,
                'CODEX_DISCONNECTED', 'Codex 连接中断。', 503)
        try:
            self.process.stdin.write((dumps(message) + '\n').encode())
            await self.process.stdin.drain()
        except (BrokenPipeError, ConnectionError, OSError) as error:
            await self.disconnected_now()
            raise Problem('CODEX_DISCONNECTED', 'Codex 连接中断。', 503) from error

    async def request(self, method, params=None, timeout=45):
        self.sequence += 1
        identifier = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        try:
            await self.send({'id': identifier, 'method': method, 'params': params or {}})
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(identifier, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()  # Observe disconnect faults even if send itself failed.

    async def read(self, process=None):
        process = process or self.process
        try:
            async for line in process.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                identifier = event.get('id')
                if 'method' not in event and identifier in self.pending:
                    future = self.pending[identifier]
                    if not future.done():
                        if event.get('error'):
                            future.set_exception(Problem('CODEX_RPC_ERROR', str(event['error'].get('message', '请求失败')), 502))
                        else:
                            future.set_result(event.get('result', {}))
                elif 'method' in event:
                    if identifier is not None:
                        # The transcription adapter cannot authorize tools or arbitrary input requests.
                        await self.send({'id': identifier, 'error': {'code': -32601, 'message': 'AutoReview disables tool execution'}})
                    self.publish(event)
        except Exception:
            pass  # The shared disconnect path wakes RPC requests and all inferences.
        finally:
            if self.process is process:
                await self.disconnected_now()

    async def close(self):
        await self.disconnected_now()
        if self.process and self.process.returncode is None:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                try:
                    self.process.kill()
                except ProcessLookupError:
                    pass
                await self.process.wait()
        if self.reader:
            try:
                await asyncio.wait_for(asyncio.shield(self.reader), 2)
            except Exception:
                self.reader.cancel()
                await asyncio.gather(self.reader, return_exceptions=True)


class Provider:
    def __init__(self, settings):
        self.settings = settings
        self.clients = {}
        self.active_turns = {}
        self.login_urls = {}
        self.cancelled = set()
        self.inboxes = {}
        self.pending_profiles = {}
        self.account_lock = asyncio.Lock()

    async def client(self, profile_id=None):
        profile_id = profile_id or self.settings.data.get('active_profile_id')
        require(profile_id and (profile_id in self.pending_profiles or any(p['id'] == profile_id for p in self.settings.data['profiles'])),
                'LOGIN_REQUIRED', '请先登录 ChatGPT 账户。', 401)
        if profile_id not in self.clients:
            self.clients[profile_id] = Rpc(self.settings.home / 'profiles' / profile_id)
        client = self.clients[profile_id]
        await client.start()
        return client

    async def add_profile(self, name, device=False):
        require(not self.pending_profiles, 'LOGIN_PENDING', '请完成或取消当前登录。', 409)
        await self.clean_pending()
        profile = {'id': uid(), 'name': (name or 'ChatGPT账户')[:80]}
        self.pending_profiles[profile['id']] = profile
        self.settings.save(pending_profiles=list(self.pending_profiles.values()))
        try:
            return await self.login(profile['id'], device)
        except BaseException:
            await self.cancel_login(profile['id'])
            raise

    async def clean_pending(self):
        for profile in list(self.settings.data.get('pending_profiles', [])):
            await self.forget(profile['id'])
            self.pending_profiles.pop(profile['id'], None)
            self.settings.save(pending_profiles=[p for p in self.settings.data.get('pending_profiles', []) if p['id'] != profile['id']])

    async def login(self, profile_id, device=False):
        client = await self.client(profile_id)
        result = await client.request('account/login/start', {'type': 'chatgptDeviceCode' if device else 'chatgpt'})
        if result.get('authUrl'):
            self.login_urls[profile_id] = result['authUrl']
        return {**result, 'profile_id': profile_id}

    async def login_status(self, profile_id):
        async with self.account_lock:
            client = await self.client(profile_id)
            account = await self.read_identity(client)
            if not account.get('account'):
                return {'complete': False}
            require(account['account'].get('type') == 'chatgpt', 'ACCOUNT_TYPE', '只支持 ChatGPT 账户。')
            profile = self.pending_profiles.get(profile_id)
            if profile:
                self.settings.save(profiles=[*self.settings.data['profiles'], {**profile, 'display_email': account['account'].get('email')}],
                                   active_profile_id=profile_id,
                                   pending_profiles=[p for p in self.settings.data.get('pending_profiles', []) if p['id'] != profile_id])
                self.pending_profiles.pop(profile_id)
            self.login_urls.pop(profile_id, None)
            return {'complete': True, **account}

    async def read_identity(self, client):
        for attempt in range(2):
            try:
                return await client.request('account/read', {'refreshToken': bool(attempt)}, timeout=20)
            except (Problem, TimeoutError) as error:
                if damaged_auth_store(error):
                    raise Problem('ACCOUNT_STORAGE_DAMAGED', '本账户的加密登录无法读取，额度也无法加载。请重新登录以修复，或删除此账户；作业资料保留。', 503) from error
                if attempt or (isinstance(error, Problem) and 'workspace routing' not in str(error).lower()):
                    raise Problem('ACCOUNT_CONNECTION_FAILED', '账户连接暂不可用。请重试连接；仍可取消登录、删除账户或退出。', 503) from error

    async def cancel_login(self, profile_id, login_id=None):
        client = self.clients.get(profile_id)
        if client and login_id:
            try:
                await client.request('account/login/cancel', {'loginId': login_id}, timeout=5)
            except (Problem, TimeoutError):
                pass
        self.login_urls.pop(profile_id, None)
        if profile_id in self.pending_profiles:
            await self.forget(profile_id)
            self.pending_profiles.pop(profile_id, None)
            self.settings.save(pending_profiles=[p for p in self.settings.data.get('pending_profiles', []) if p['id'] != profile_id])
        return {'ok': True}

    async def forget(self, profile_id):
        """Official offline logout, never open/copy credential contents."""
        import re
        require(isinstance(profile_id, str) and re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', profile_id), 'PROFILE_NOT_FOUND', '账户配置不存在。')
        client = self.clients.pop(profile_id, None)
        if client:
            await client.close()
        profiles_root = self.settings.home / 'profiles'
        home = profiles_root / profile_id
        require(not profiles_root.is_symlink() and not profiles_root.is_junction() and profiles_root.resolve().parent == self.settings.home and
                not home.is_symlink() and not home.is_junction() and home.resolve().parent == profiles_root.resolve(), 'ACCOUNT_PATH', '账户目录不安全，已停止清理。')
        from .account_storage import cli_environment, cli_options
        home.mkdir(parents=True, exist_ok=True)
        process = await asyncio.create_subprocess_exec(executable(self.settings.home), *cli_options(home), 'logout',
            env=cli_environment(home), cwd=home, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            creationflags=0x08000000 if os.name == 'nt' else 0)
        try:
            _, diagnostic = await asyncio.wait_for(process.communicate(), 15)
        except BaseException:
            if process.returncode is None: process.kill(); await process.wait()
            raise
        # Official logout decrypts the store before deleting it. A damaged store
        # must remain deletable after the user's explicit local cleanup request.
        # Inspect only a failure category; never persist CLI output or auth values.
        damaged = process.returncode != 0 and damaged_auth_store(diagnostic.decode(errors='replace'))
        require(process.returncode == 0 or damaged, 'ACCOUNT_CLEANUP_FAILED', '官方退出未完成，账户条目已保留。请关闭相关窗口后重试。', 503,
                stage='official_logout', exit_code=process.returncode)
        from .account_storage import remove_keyring_keys
        try:
            await asyncio.to_thread(remove_profile_tree, home)
        except OSError as error:
            raise Problem('ACCOUNT_CLEANUP_FAILED', '账户文件清理未完成，可能被占用或无写入权限；账户条目保留，可关闭相关窗口后重试。', 503, stage='profile_files') from error
        # Never strand encrypted files by deleting their key before tree removal.
        # On an I/O failure both remain together, so a retry can still read them.
        remove_keyring_keys(home)
        if damaged:
            probe = Rpc(home)
            try:
                await probe.start()
                identity = await probe.request('account/read', {'refreshToken': False}, timeout=15)
                require(identity.get('account') is None, 'ACCOUNT_CLEANUP_FAILED', '清理后仍检测到登录，账户条目已保留，请重试。', 503, stage='verify_signed_out')
            finally:
                await probe.close()
            try:
                await asyncio.to_thread(remove_profile_tree, home)
            except OSError as error:
                raise Problem('ACCOUNT_CLEANUP_FAILED', '清理验证目录仍被占用，账户条目保留，请重试。', 503, stage='verification_files') from error
            remove_keyring_keys(home)
        self.login_urls.pop(profile_id, None)
        return {'cleanup_mode': 'damaged_store_reset' if damaged else 'official_logout'}

    async def remove_profile(self, profile_id):
        async with self.account_lock:
            require(any(p['id'] == profile_id for p in self.settings.data['profiles']), 'PROFILE_NOT_FOUND', '账户配置不存在。')
            cleanup = await self.forget(profile_id)
            profiles = [p for p in self.settings.data['profiles'] if p['id'] != profile_id]
            self.settings.save(profiles=profiles, active_profile_id=None if self.settings.data.get('active_profile_id') == profile_id else self.settings.data.get('active_profile_id'))
        return {'ok': True, **(cleanup or {})}

    async def repair_login(self, profile_id):
        async with self.account_lock:
            require(any(p['id'] == profile_id for p in self.settings.data['profiles']), 'PROFILE_NOT_FOUND', '账户配置不存在。')
            await self.forget(profile_id)
            # Keep the profile ID and frozen task references. Only its obsolete
            # local login is cleared; a human completes the new official login.
            return await self.login(profile_id)

    async def account(self, profile_id=None, refresh_models=False):
        async with self.account_lock:
            return await self._account_current(profile_id, refresh_models)

    async def _account_current(self, profile_id=None, refresh_models=False):
        client = await self.client(profile_id)
        fresh_client = None
        if refresh_models:
            # A separate official process refreshes its own catalogue without
            # interrupting inference already queued on the profile's primary client.
            fresh_client = Rpc(client.home)
            try:
                await fresh_client.start()
                return await self._account(fresh_client, profile_id)
            finally:
                await fresh_client.close()
        return await self._account(client, profile_id)

    async def _account(self, client, profile_id=None):
        storage_error = None
        try:
            account = await self.read_identity(client)
        except Problem as error:
            if error.code != 'ACCOUNT_STORAGE_DAMAGED': raise
            account, storage_error = {'account': None}, error.message
        result = {'account': account.get('account'), 'rate_limits': None, 'models': [], 'usage': None,
                  'connection_state': 'connected' if account.get('account') else 'not_connected',
                  'quota_status': 'not_connected',
                  'model_catalogue': {'source': 'codex_app_server', 'read_at': now(),
                                      'cli': Path(client.executable_path or executable()).name,
                                      'server': client.server_info.get('userAgent'),
                                      'complete': False,
                                      'access_verified_by_inference': False}}
        if not result['account']:
            result['connection_state'] = 'credentials_unreadable' if storage_error else 'not_connected'
            result['warning'] = storage_error or '本副本保存过此账户，但当前尚未连接，无法读取额度。请重新登录；不会使用其他副本的账户。'
        if result['account']:
            require(result['account'].get('type') == 'chatgpt', 'ACCOUNT_TYPE', '只支持官方 ChatGPT 登录账户。')
            active = profile_id or self.settings.data.get('active_profile_id')
            email = result['account'].get('email')
            if email:
                self.settings.save(profiles=[{**p, 'display_email': email} if p['id'] == active else p
                                             for p in self.settings.data['profiles']])
            for method, key in [('account/rateLimits/read', 'rate_limits'), ('account/tokenUsage/read', 'usage')]:
                try:
                    result[key] = await client.request(method, timeout=8)
                except (Problem, TimeoutError):
                    pass
            limits = result['rate_limits'] or {}
            buckets = list((limits.get('rateLimitsByLimitId') or {}).values()) + [limits.get('rateLimits')]
            has_quota = any(isinstance(bucket, dict) and any(bucket.get(key) is not None for key in ('primary', 'secondary')) for bucket in buckets)
            result['quota_status'] = 'available' if has_quota else 'unavailable'
            if not has_quota:
                result['quota_warning'] = '账户已连接，但官方额度窗口暂未返回。请刷新登录与额度；缺失不代表额度为零或无限制。'
            cursor, seen = None, set()
            while True:
                try:
                    models = await client.request('model/list', {'cursor': cursor, 'includeHidden': False, 'limit': 100}, timeout=15)
                except (Problem, TimeoutError):
                    result['warning'] = '账户已连接，但模型目录暂不可用，请刷新；未提交云端任务。'
                    break
                rows = models.get('data')
                if not isinstance(rows, list) or any(not isinstance(m, dict) or not (m.get('model') or m.get('id')) for m in rows):
                    result['warning'] = '账户已连接，但模型目录返回不完整，请刷新；未提交云端任务。'
                    break
                result['models'].extend(rows)
                cursor = models.get('nextCursor')
                if not cursor:
                    result['model_catalogue']['complete'] = True
                    break
                if not isinstance(cursor, str) or cursor in seen or len(seen) >= 32:
                    result['warning'] = '账户已连接，但模型目录分页未完成，请刷新；未提交云端任务。'
                    break
                seen.add(cursor)
            result['model_catalogue']['count'] = len(result['models'])
        return result

    async def validate(self, config):
        account = await self.account(config['profile_id'])
        require(account['account'], 'LOGIN_REQUIRED', '账户未登录或登录已失效。', 401)
        def selected(result):
            return next((m for m in result['models'] if (m.get('model') or m.get('id')) == config['model']), None)
        def complete(result):
            return bool(result['models']) and not result.get('warning') and result.get('model_catalogue', {}).get('complete', True)
        model = selected(account)
        if not complete(account) or model is None:
            # A stale process or cached/bundled catalogue is not an entitlement
            # check. Refresh once without disrupting the profile's active turns.
            account = await self.account(config['profile_id'], refresh_models=True)
            require(account['account'], 'LOGIN_REQUIRED', '账户未登录或登录已失效。', 401)
            model = selected(account)
        require(complete(account), 'MODEL_CATALOGUE_UNAVAILABLE', '账户已连接，但模型目录尚未完整加载。请重试或在设置中刷新模型目录；未提交云端任务。', 503)
        require(model, 'MODEL_UNAVAILABLE', f'当前 Codex 组件的模型目录未列出 {config["model"]}。请更新程序或在设置中选择目录中的模型；这不代表账户没有权限。')
        require('image' in model.get('inputModalities', ['text', 'image']), 'IMAGE_INPUT_UNAVAILABLE', '所选模型不支持图片输入。')
        efforts = [x.get('reasoningEffort') for x in model.get('supportedReasoningEfforts', [])]
        require(config['effort'] in efforts, 'EFFORT_UNAVAILABLE', '该模型不支持所选推理强度。')
        if config.get('conservative_review'):
            require(config.get('verification_effort', 'medium') in efforts, 'EFFORT_UNAVAILABLE', '该模型不支持所选保守复看强度。')
        tiers = model.get('serviceTiers') or []
        if config.get('speed') == 'fast':
            tier = next((t['id'] for t in tiers if t.get('id') in ('fast', 'priority') or 'fast' in t.get('name', '').lower()), None)
            require(tier, 'FAST_UNAVAILABLE', '当前账户/模型未提供 Fast，不能提交快速模式。')
        else:
            tier = 'default'
        return {**config, 'service_tier': tier}, account

    async def quota(self, profile_id):
        try:
            client = await self.client(profile_id)
            return await client.request('account/rateLimits/read', timeout=15)
        except (Problem, TimeoutError):
            return None

    async def interrupt(self, attempt_id):
        self.cancelled.add(attempt_id)
        running = self.active_turns.get(attempt_id)
        if running:
            client, thread_id, turn_id = running
            try:
                await client.request('turn/interrupt', {'threadId': thread_id, 'turnId': turn_id}, timeout=10)
            except (Problem, TimeoutError):
                pass
            finally:
                binding = self.inboxes.get(attempt_id)
                if binding:
                    binding[2].close('TURN_INTERRUPTED')

    async def infer(self, attempt_id, purpose, paths, prompt, variables, config, on_started=None, on_progress=None):
        try:
            return await self._infer(attempt_id, purpose, paths, prompt, variables, config, on_started, on_progress)
        except asyncio.CancelledError:
            if attempt_id not in self.cancelled:
                await self.interrupt(attempt_id)
            raise
        except Exception:
            # A local callback/adapter failure must not leave a known running
            # cloud turn orphaned after its subscriber has gone away.
            if attempt_id in self.active_turns and attempt_id not in self.cancelled:
                await self.interrupt(attempt_id)
            raise
        finally:
            binding = self.inboxes.pop(attempt_id, None)
            if binding:
                client, thread_id, inbox = binding
                client.unsubscribe(thread_id, inbox)
            self.active_turns.pop(attempt_id, None)
            self.cancelled.discard(attempt_id)

    async def _infer(self, attempt_id, purpose, paths, prompt, variables, config, on_started=None, on_progress=None):
        from .contracts import validate_output_schema
        validate_output_schema(prompt['schema'])
        require(attempt_id not in self.cancelled, 'TURN_INTERRUPTED', '本次提交已终止。', 409)
        if on_progress: await asyncio.to_thread(on_progress,'connecting', None)
        client = await self.client(config['profile_id'])
        offset = len(client.events)
        thread = await client.request('thread/start', {'model': config['model'], 'modelProvider': 'openai',
            'cwd': str(client.home / 'empty-workspace'), 'approvalPolicy': 'never', 'sandbox': 'read-only',
            'ephemeral': True, 'experimentalRawEvents': False, 'serviceTier': config['service_tier'],
            'baseInstructions': prompt['template_text'], 'environments': [], 'selectedCapabilityRoots': [],
            'config': {'project_doc_max_bytes': 0, 'web_search': 'disabled'}, 'allowProviderModelFallback': False})
        thread_id = thread['thread']['id']
        require(attempt_id not in self.cancelled, 'TURN_INTERRUPTED', '本次提交已终止。', 409)
        inbox = client.subscribe(thread_id) if callable(getattr(client, 'subscribe', None)) else None
        if inbox is not None:
            # Subscribe before turn/start: completed text and token usage can be
            # emitted before that RPC response, especially for very short turns.
            self.inboxes[attempt_id] = (client, thread_id, inbox)
        request = {'threadId': thread_id, 'model': config['model'], 'effort': config['effort'],
            'serviceTierForTurn': config['service_tier'], 'outputSchema': prompt['schema'],
            'input': [{'type': 'text', 'text': dumps(variables), 'text_elements': []},
                      *[{'type': 'localImage', 'path': str(p)} for p in paths]]}
        started = await client.request('turn/start', request)
        turn_id = started['turn']['id']
        self.active_turns[attempt_id] = (client, thread_id, turn_id)
        if attempt_id in self.cancelled:
            await self.interrupt(attempt_id)
        try:
            if on_started:
                await asyncio.to_thread(on_started,thread_id, turn_id)
            if on_progress: await asyncio.to_thread(on_progress,'waiting', 'turn/started')
        except BaseException:
            await self.interrupt(attempt_id)
            raise
        usage, actual_model, actual_tier = None, None, None
        last_provider_error = None
        last_progress = 0
        texts, forbidden = {}, False
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        hard_deadline = started_at + (REFERENCE_MAX_TIMEOUT_SECONDS if purpose == 'reference' else INFERENCE_TIMEOUT_SECONDS)
        last_reschedule = started_at - INFERENCE_RESCHEDULE_INTERVAL_SECONDS
        def matches(params):
            event_turn = params.get('turnId') or (params.get('turn') or {}).get('id')
            return params.get('threadId') == thread_id and (event_turn is None or event_turn == turn_id)
        try:
            async with asyncio.timeout(INFERENCE_TIMEOUT_SECONDS) as timeout:
                while True:
                    if inbox is not None:
                        events = await inbox.receive()
                    else:
                        # Older protocol test adapters expose the original list
                        # and condition only; production Rpc uses per-thread inboxes.
                        async with client.condition:
                            await client.condition.wait_for(lambda: len(client.events) > offset or client.process.returncode is not None)
                            events = client.events[offset:]
                            offset = len(client.events)
                        require(events or client.process.returncode is None, 'CODEX_DISCONNECTED', 'Codex 进程已退出。', 503)
                    # A terminal error can precede a usage event in the same read
                    # batch. Both have already arrived, so retain the latest known
                    # usage before exiting on that error. Never invent missing usage.
                    for event in events:
                        params = event.get('params', {})
                        if event.get('method') == 'thread/tokenUsage/updated' and matches(params):
                            reported_usage = params.get('tokenUsage', {}).get('last')
                            if reported_usage is not None:
                                usage = reported_usage
                    for event in events:
                        params, method = event.get('params', {}), event.get('method', '')
                        if not matches(params):
                            continue
                        # Large reference results may still be streaming at ten
                        # minutes. Only real text activity extends their idle
                        # deadline; retries and status notifications cannot keep
                        # an unproductive turn alive beyond the original limit.
                        delta = params.get('delta')
                        if (purpose == 'reference' and method in ('item/agentMessage/delta',
                                'item/reasoning/textDelta', 'item/reasoning/summaryTextDelta')
                                and isinstance(delta, str) and delta.strip()):
                            activity_time = loop.time()
                            if activity_time - last_reschedule >= INFERENCE_RESCHEDULE_INTERVAL_SECONDS:
                                timeout.reschedule(min(hard_deadline, activity_time + INFERENCE_TIMEOUT_SECONDS))
                                last_reschedule = activity_time
                        if method == 'error':
                            from .telemetry import sanitize
                            error = sanitize(params.get('error') or {})
                            last_provider_error = {**(error if isinstance(error, dict) else {'message': error}),
                                                   'notification': 'error', 'willRetry': params.get('willRetry')}
                            # willRetry=true describes the CLI's ongoing transport
                            # recovery of this turn, not a new AutoReview submission.
                            if params.get('willRetry') is False:
                                self.active_turns.pop(attempt_id, None)
                                require(not forbidden, 'UNEXPECTED_TOOL', '模型尝试调用工具，结果已拒绝。',
                                        usage=usage, provider_error=last_provider_error)
                                if on_progress: await asyncio.to_thread(on_progress,'failed', method)
                                raise turn_failure(last_provider_error, usage)
                            if on_progress and asyncio.get_running_loop().time()-last_progress >= 1:
                                await asyncio.to_thread(on_progress,'cloud_active', method)
                                last_progress = asyncio.get_running_loop().time()
                            continue
                        if on_progress and (asyncio.get_running_loop().time()-last_progress >= 1 or method == 'turn/completed'):
                            phase = 'receiving' if method == 'item/agentMessage/delta' else 'validating' if method == 'turn/completed' else 'cloud_active'
                            await asyncio.to_thread(on_progress,phase, method)
                            last_progress = asyncio.get_running_loop().time()
                        if method == 'model/rerouted':
                            actual_model = params.get('toModel')
                        if method in ('item/started', 'item/completed'):
                            item = params.get('item', {})
                            if item.get('type') not in ('userMessage', 'agentMessage', 'reasoning', 'plan'):
                                forbidden = True
                                await self.interrupt(attempt_id)
                            if method == 'item/completed' and item.get('type') == 'agentMessage':
                                texts[item['id']] = item.get('text', '')
                        if method == 'turn/completed':
                            self.active_turns.pop(attempt_id, None)
                            from .telemetry import sanitize
                            turn = params['turn']
                            require(not forbidden, 'UNEXPECTED_TOOL', '模型尝试调用工具，结果已拒绝。', usage=usage)
                            if turn['status'] == 'failed':
                                raise turn_failure(turn.get('error') or last_provider_error, usage)
                            require(turn['status'] == 'completed', 'TURN_' + turn['status'].upper(),
                                    '模型调用未完成。', 502, usage=usage, provider_error=sanitize(turn.get('error') or last_provider_error))
                            raw = list(texts.values())[-1] if texts else ''
                            try:
                                from .output_safety import decode_response
                                parsed, geometry_warnings = decode_response(purpose, prompt.get('schema_version', '4.0'), raw)
                            except Exception as error:
                                issues = [{'path': list(e['loc']), 'type': e['type']} for e in error.errors()] if hasattr(error, 'errors') else []
                                raise Problem('OUTPUT_SCHEMA', '模型返回不符合结构，已保留失败记录。', 502,
                                    usage=usage, raw_output=raw, validation_issues=issues) from error
                            return {'output': parsed, 'geometry_warnings': geometry_warnings, 'usage': usage, 'thread_id': thread_id, 'turn_id': turn_id,
                                    'actual_model': actual_model, 'actual_service_tier': actual_tier}
        except TimeoutError as error:
            await self.interrupt(attempt_id)
            raise Problem('TURN_TIMEOUT', '本次调用超时，需检查后继续。', 504, usage=usage,
                          provider_error=last_provider_error) from error
        except Problem as error:
            if error.code in ('CODEX_DISCONNECTED', 'TURN_INTERRUPTED'):
                require(not forbidden, 'UNEXPECTED_TOOL', '模型尝试调用工具，结果已拒绝。', usage=usage)
                error.details.setdefault('usage', usage)
            raise

    async def close(self):
        for profile_id in tuple(self.pending_profiles):
            try:
                await self.cancel_login(profile_id)
            except Exception:
                pass  # Persisted pending IDs are retried on next explicit login/cleanup.
        await asyncio.gather(*(c.close() for c in self.clients.values()), return_exceptions=True)
