"""Tie each auxiliary process to its parent without blocking Tk on shutdown."""
import os
import subprocess
import sys
import threading
import time


PARENT_PIPE_ENV = 'CAMERA_PARENT_PIPE'
POLL_MS = 50


class ParentLifetime:
    def __init__(self, stream=None):
        self.closed = threading.Event()
        if stream is not None:
            threading.Thread(target=self._watch, args=(stream,),
                             name='Camera parent lifetime', daemon=True).start()

    def _watch(self, stream):
        try:
            # The parent holds this pipe open. EOF also covers a parent crash.
            while stream.read(1):
                pass
        finally:
            self.closed.set()

    def bind(self, root, on_close):
        def poll():
            if self.closed.is_set():
                on_close()
            else:
                root.after(POLL_MS, poll)
        root.after(POLL_MS, poll)


# Consume the marker so unrelated subprocesses do not inherit this contract.
parent_lifetime = ParentLifetime(
    sys.stdin if os.environ.pop(PARENT_PIPE_ENV, None) == '1' else None)


class ChildProcesses:
    GRACE_SECONDS = 10.0  # Allow PTZ's bounded graceful Stop/retry sequence.

    def __init__(self, root, clock=time.monotonic):
        self.root = root
        self.clock = clock
        self.processes = []
        self.closing = False
        self._deadline = None

    @staticmethod
    def _close_pipe(process):
        if process.stdin is not None and not process.stdin.closed:
            try:
                process.stdin.close()
            except OSError:
                pass  # The child may already have exited.

    def _reap(self):
        alive = []
        for process in self.processes:
            if process.poll() is None:
                alive.append(process)
            else:
                self._close_pipe(process)
        self.processes = alive

    def spawn(self, command):
        if self.closing:
            return None
        self._reap()
        env = os.environ.copy()
        env[PARENT_PIPE_ENV] = '1'
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
        self.processes.append(process)
        return process

    def close(self):
        if self.closing:
            return
        self.closing = True
        self._deadline = self.clock() + self.GRACE_SECONDS
        self.root.withdraw()
        # Notify all siblings together, including managers with their own children.
        for process in self.processes:
            self._close_pipe(process)
        self._finish_close()

    def _finish_close(self):
        self._reap()
        if not self.processes:
            self.root.destroy()
            return
        if self.clock() >= self._deadline:
            for process in self.processes:
                try:
                    process.kill()
                except OSError:
                    pass  # It may have exited between poll and kill.
        self.root.after(POLL_MS, self._finish_close)
