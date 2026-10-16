"""A backend belongs to its UI connections, not to a detached console."""
from __future__ import annotations
import asyncio
import os
import time
import threading
import zlib
from .core import uid
from .power import awake_time, power

# The heartbeat must not queue behind image/database work or depend on a
# filesystem replace racing the supervisor's read on Windows. A named mapping
# is shared only by this randomly named local backend/supervisor pair.
_pulse_maps = {}
_pulse_write_lock = threading.Lock()


def pulse_frame(phase, stamp):
    value = (phase + ':' + str(stamp)).encode('ascii')
    if phase not in ('running', 'closing') or len(value) > 100:
        raise ValueError('Invalid heartbeat phase or length')
    checksum = format(zlib.crc32(value), '08x').encode('ascii')
    return (value + b'|' + checksum).ljust(128, b'\0')


def decode_pulse_frame(frame):
    # A reader can run during the writer's memory copy. Only a complete,
    # self-consistent frame counts as a heartbeat; retry a torn read next tick.
    if len(frame) != 128:
        raise OSError('Incomplete heartbeat frame')
    value, separator, checksum = frame.split(b'\0', 1)[0].rpartition(b'|')
    if not separator or checksum != format(zlib.crc32(value), '08x').encode('ascii'):
        raise OSError('Incomplete heartbeat frame')
    try:
        phase, stamp = value.decode('ascii').split(':', 1)
        if phase not in ('running', 'closing') or not float(stamp) >= 0:
            raise ValueError('Invalid heartbeat value')
    except (ValueError, UnicodeError) as error:
        raise OSError('Invalid heartbeat frame') from error
    return value.decode('ascii')


def pulse_mapping(path):
    import mmap
    from pathlib import Path
    name = 'Local\\AutoReviewPulse-' + Path(path).stem
    if name not in _pulse_maps:
        _pulse_maps[name] = mmap.mmap(-1, 128, tagname=name)
    return _pulse_maps[name]


def read_pulse(path):
    if os.name == 'nt':
        return decode_pulse_frame(pulse_mapping(path)[:128])
    return path.read_text('ascii')


class UiLease:
    def __init__(self, clock=awake_time, startup=180, grace=8, lost=110, record=None):
        self.clock, self.startup, self.grace = clock, startup, grace
        self.started = clock()
        self.clients = set()
        self.last_empty = None
        self.seen = False
        self.last_reason = 'ui_not_opened'
        self.activity, self.tabs = {}, {}
        self.lost, self.record = lost, record or (lambda *a, **kw: None)

    def join(self, tab=None):
        identifier = uid()
        self.clients.add(identifier)
        self.tabs[identifier] = tab or identifier
        self.touch(tab or identifier)
        self.seen, self.last_empty = True, None
        self.record('ui_joined', clients=len(self.clients))
        return identifier

    def touch(self, tab=None):
        targets = [tab] if tab else {self.tabs[c] for c in self.clients}
        for key in targets:
            if key in self.tabs.values() or key in self.activity:
                self.activity[key] = self.clock()

    def recently_active(self, identifier, seconds=90):
        return self.clock()-self.activity.get(self.tabs.get(identifier), -1e12) < seconds

    def leave(self, identifier, reason='ui_closed'):
        self.clients.discard(identifier)
        tab = self.tabs.pop(identifier, None)
        if reason == 'ui_closed' and not any(self.tabs[c] == tab for c in self.clients):
            self.activity.pop(tab, None)
        self.record('ui_left', reason=reason, clients=len(self.clients))
        if not self.clients:
            self.last_empty = self.clock()
            self.last_reason = reason

    def expired(self):
        if power.protected():
            return False
        if self.clients:
            return False
        if not self.seen:
            return self.clock() - self.started >= self.startup
        if self.last_reason != 'ui_closed' and self.activity and self.clock()-max(self.activity.values()) < self.lost:
            return False
        return self.last_empty is not None and self.clock() - self.last_empty >= self.grace


def parent_alive(pid):
    if not pid:
        return True
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = wintypes.DWORD()
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        try:
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def supervise(pid, ready):
    """The supervisor owns the job outside it; a dying member never owns its kill handle."""
    if os.name != 'nt':
        return None
    import ctypes
    from ctypes import wintypes
    class Basic(ctypes.Structure):
        _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                    ('flags', wintypes.DWORD), ('min_ws', ctypes.c_size_t), ('max_ws', ctypes.c_size_t),
                    ('active', wintypes.DWORD), ('affinity', ctypes.c_size_t),
                    ('priority', wintypes.DWORD), ('scheduling', wintypes.DWORD)]
    class Io(ctypes.Structure):
        _fields_ = [(x, ctypes.c_uint64) for x in ('read_ops', 'write_ops', 'other_ops', 'read', 'write', 'other')]
    class Extended(ctypes.Structure):
        _fields_ = [('basic', Basic), ('io', Io), ('process_mem', ctypes.c_size_t),
                    ('job_mem', ctypes.c_size_t), ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateJobObjectW(None, None)
    info = Extended()
    info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not handle or not kernel.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
        if handle:
            kernel.CloseHandle(handle)
        raise OSError(ctypes.get_last_error(), 'Cannot create backend process job')
    process = kernel.OpenProcess(0x100000 | 0x0100 | 0x0001, False, pid)
    if not process or not kernel.AssignProcessToJobObject(handle, process):
        kernel.CloseHandle(handle)
        raise OSError(ctypes.get_last_error(), 'Cannot contain backend child processes')
    from pathlib import Path
    Path(ready).write_text('ready', encoding='ascii')
    pulse_file = Path(ready).with_suffix('.pulse')
    try:
        power.register()
        last, previous, payload = awake_time(), awake_time(), None
        while kernel.WaitForSingleObject(process, 1000) == 258:
            current = awake_time()
            # Sleep/resume grants a new observation window; the OS cannot clean while asleep.
            if current - previous > 15:
                last = current
            previous = current
            try:
                value = read_pulse(pulse_file)
                if value != payload:
                    payload, last = value, current
            except OSError:
                pass
            limit = 20 if payload and payload.startswith('closing:') else 90 if payload else 180
            if current - last > limit and not power.protected():
                import json
                record = {'pid':pid,'reason':'shutdown_timeout' if payload and payload.startswith('closing:') else 'event_loop_stalled','idle_seconds':current-last,'recorded_at':time.time()}
                Path(ready).with_suffix('.exit.json').write_text(json.dumps(record), encoding='utf-8')
                try:
                    from .core import MachineSettings
                    destination=MachineSettings().home/'startup-logs'/'watchdog-exits.jsonl'
                    destination.parent.mkdir(parents=True,exist_ok=True)
                    with destination.open('a',encoding='utf-8') as log: log.write(json.dumps(record)+'\n')
                except OSError: pass
                break
    finally:
        kernel.CloseHandle(handle)
        kernel.CloseHandle(process)
        power.close()
        try:
            pulse_file.unlink(missing_ok=True)
        except OSError:
            pass


def contain_children():
    if os.name != 'nt':
        return None
    import subprocess, sys, tempfile
    from pathlib import Path
    ready = Path(tempfile.gettempdir()) / ('autoreview-supervisor-' + uid() + '.ready')
    command = [sys.executable] + ([] if getattr(sys, 'frozen', False) else ['-m', 'autoreview.v2.launch'])
    child = subprocess.Popen(command + ['--supervise', str(os.getpid()), '--ready-file', str(ready)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=0x08000000)
    deadline = time.monotonic()+15
    while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
        time.sleep(.05)
    if not ready.exists():
        child.kill(); child.wait()
        raise RuntimeError('后台进程监护启动失败。')
    for retry in range(20):
        try:
            ready.unlink(missing_ok=True)
            break
        except PermissionError:
            time.sleep(.05)
    os.environ['AUTOREVIEW_WATCHDOG_FILE'] = str(ready.with_suffix('.pulse'))
    return child


def pulse(phase='running'):
    path = os.environ.get('AUTOREVIEW_WATCHDOG_FILE')
    if path:
        from pathlib import Path
        if os.name == 'nt':
            with _pulse_write_lock:
                pulse_mapping(path)[:128] = pulse_frame(phase, time.monotonic())
        else:
            from .core import atomic_write
            value = (phase + ':' + str(time.monotonic())).encode('ascii')
            atomic_write(Path(path), value)


async def watch(app, lease, parent_pid=None, manage_lease=True):
    import faulthandler
    failures = set()

    def report_once(event, error):
        if event in failures:
            return
        failures.add(event)
        # A diagnostics failure must not terminate the lifecycle task either.
        try:
            lease.record(event, error_type=type(error).__name__)
        except Exception:
            try:
                import sys
                print('AutoReview lifecycle: ' + event + ' / ' + type(error).__name__, file=sys.stderr)
            except Exception:
                pass

    try:
        while True:
            try:
                if faulthandler.is_enabled() and os.environ.get('AUTOREVIEW_WATCHDOG_FILE'):
                    import sys
                    faulthandler.dump_traceback_later(75,file=getattr(app.state,'hang_trace',sys.stderr))
                failures.discard('heartbeat_trace_error')
            except Exception as error:
                report_once('heartbeat_trace_error', error)
            await asyncio.sleep(1)
            if not getattr(app.state, 'shutting_down', False):
                try:
                    # A bounded mapping write does not queue behind database or
                    # image work in the shared default executor.
                    if os.name == 'nt':
                        pulse()
                    else:
                        await asyncio.to_thread(pulse)
                    failures.discard('heartbeat_transport_error')
                except Exception as error:
                    report_once('heartbeat_transport_error', error)
            try:
                expired = manage_lease and not parent_pid and lease.expired()
                if expired or not parent_alive(parent_pid):
                    await app.state.request_shutdown(reason=lease.last_reason if expired else 'parent_lost')
                    return
                failures.discard('heartbeat_lifecycle_error')
            except Exception as error:
                report_once('heartbeat_lifecycle_error', error)
    finally:
        try:
            faulthandler.cancel_dump_traceback_later()
        except Exception:
            pass
