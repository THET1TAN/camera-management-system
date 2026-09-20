"""Owned hidden processes, bounded capture and Windows kill-on-owner-exit jobs."""
import ctypes
from ctypes import wintypes
import os
import subprocess
import threading
import time

from .model import PlaybackError, check_cancel


class Process:
    def __init__(self, args, *, stdin=None, background=False):
        self.job = None
        self.process = subprocess.Popen(args, stdin=stdin if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
            creationflags=(subprocess.CREATE_NO_WINDOW | (subprocess.BELOW_NORMAL_PRIORITY_CLASS if background else 0)) if os.name == 'nt' else 0)
        if os.name == 'nt':
            try:
                self._own_windows()
            except Exception:
                self.process.kill()
                self.process.wait(timeout=3)
                self.process.stdout.close()
                self.process.stderr.close()
                raise PlaybackError('process-ownership') from None

    def _own_windows(self):
        class Basic(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong), ('PerJobUserTimeLimit', ctypes.c_longlong),
                ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD), ('SchedulingClass', wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ('ReadOperationCount', 'WriteOperationCount',
                'OtherOperationCount', 'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]
        class Extended(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', Basic), ('IoInfo', IO), ('ProcessMemoryLimit', ctypes.c_size_t),
                ('JobMemoryLimit', ctypes.c_size_t), ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        api.CreateJobObjectW.restype = wintypes.HANDLE
        api.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        api.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        api.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = api.CreateJobObjectW(None, None)
        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not handle:
            raise OSError
        if not api.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)) or not api.AssignProcessToJobObject(handle, int(self.process._handle)):
            api.CloseHandle(handle)
            raise OSError
        self.job = (api, handle)

    def close(self):
        try:
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait(timeout=3)
        finally:
            if self.job:
                self.job[0].CloseHandle(self.job[1])
                self.job = None
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                if stream and not stream.closed:
                    stream.close()


def run(args, cancel, timeout=60, tick=None, limit=2*1024*1024, input_chunks=None, allow_early_input_close=False, background=False):
    try:
        owned = Process(args, stdin=subprocess.PIPE if input_chunks is not None else None, background=background)
    except OSError:
        raise PlaybackError('dependency-missing') from None
    output = bytearray()
    exceeded = threading.Event()
    stderr_bytes = [0]
    def drain(stream, collect):
        while True:
            data = stream.read(8192)
            if not data:
                return
            if collect:
                if len(output)+len(data) > limit:
                    exceeded.set()
                elif not exceeded.is_set():
                    output.extend(data)
            else:
                stderr_bytes[0] += len(data)  # Never retain arbitrary native diagnostics.
    readers = [threading.Thread(target=drain, args=(owned.process.stdout, True), daemon=True),
               threading.Thread(target=drain, args=(owned.process.stderr, False), daemon=True)]
    feeder_failed = threading.Event()
    if input_chunks is not None:
        def feed():
            try:
                for chunk in input_chunks:
                    check_cancel(cancel)
                    # A short raw-pipe write must not discard compressed bytes.
                    view = memoryview(chunk)
                    while view:
                        count = owned.process.stdin.write(view)
                        if not count:
                            raise OSError
                        view = view[count:]
            except BrokenPipeError:
                # A bounded metadata probe may finish successfully before reading
                # the entire supplied snapshot. Media producers still require EOF.
                if not allow_early_input_close:
                    feeder_failed.set()
            except Exception:
                feeder_failed.set()
            finally:
                try:
                    owned.process.stdin.close()
                except OSError:
                    pass
        readers.append(threading.Thread(target=feed, daemon=True))
    for reader in readers:
        reader.start()
    deadline = time.monotonic()+timeout
    try:
        while owned.process.poll() is None:
            check_cancel(cancel)
            if exceeded.is_set() or time.monotonic() > deadline:
                raise PlaybackError('media-timeout')
            if tick:
                tick()
            cancel.wait(.1)
        for reader in readers:
            reader.join(timeout=1)
        if exceeded.is_set():
            raise PlaybackError('media-output-limit')
        if owned.process.returncode or feeder_failed.is_set():
            raise PlaybackError('media-invalid')
        return bytes(output), stderr_bytes[0]
    finally:
        owned.close()
        for reader in readers:
            reader.join(timeout=1)
