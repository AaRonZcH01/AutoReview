"""Windows awake time and native power notifications, shared by all deadlines."""
import os
import time
import threading


def awake_time():
    if os.name == 'nt':
        import ctypes
        value=ctypes.c_ulonglong()
        if ctypes.windll.kernel32.QueryUnbiasedInterruptTime(ctypes.byref(value)):
            return value.value / 10_000_000
    return time.monotonic()


class PowerState:
    def __init__(self, clock=awake_time):
        self.clock=clock; self.epoch=0; self.suspended=False; self.grace_until=0
        self._lock=threading.Lock(); self._handle=None

    def event(self, kind):
        with self._lock:
            if kind == 4:
                self.suspended=True
            elif kind in (7,18):
                self.suspended=False; self.epoch+=1; self.grace_until=self.clock()+120

    def protected(self):
        return self.suspended or self.clock() < self.grace_until

    def register(self):
        if os.name != 'nt' or self._handle:
            return
        import ctypes
        from ctypes import wintypes
        callback_type=ctypes.WINFUNCTYPE(wintypes.ULONG,ctypes.c_void_p,wintypes.ULONG,ctypes.c_void_p)
        self._callback=callback_type(lambda context,event,setting:(self.event(event),0)[1])
        class Parameters(ctypes.Structure):
            _fields_=[('callback',callback_type),('context',ctypes.c_void_p)]
        self._parameters=Parameters(self._callback,None)
        self._library=ctypes.WinDLL('powrprof',use_last_error=True)
        self._library.PowerRegisterSuspendResumeNotification.argtypes=[wintypes.DWORD,ctypes.c_void_p,ctypes.POINTER(ctypes.c_void_p)]
        handle=ctypes.c_void_p()
        result=self._library.PowerRegisterSuspendResumeNotification(2,ctypes.byref(self._parameters),ctypes.byref(handle))
        if result==0: self._handle=handle
        else: raise OSError(result,'无法注册 Windows 休眠恢复通知')

    def close(self):
        if self._handle:
            self._library.PowerUnregisterSuspendResumeNotification(self._handle)
            self._handle=None


power=PowerState()
