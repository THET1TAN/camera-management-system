"""One bounded prefix inspector/producer per transfer, with explicit fallbacks."""
import threading
import time

from .media import PREFIX_STEPS, probe_prefix
from .model import Cancelled, PlaybackError, check_cancel


class TransferCancel:
    def __init__(self, parent, failed):
        self.parent, self.failed = parent, failed

    def is_set(self):
        return self.parent.is_set() or self.failed.is_set()

    def wait(self, seconds):
        deadline = time.monotonic()+seconds
        while not self.is_set() and time.monotonic() < deadline:
            self.parent.wait(min(.05, max(0, deadline-time.monotonic())))
        return self.is_set()


class ProgressivePreparation:
    def __init__(self, path, settings, cancel, finished, failed, produce, emit, published=lambda: False):
        self.path, self.settings = path, settings
        self.cancel = TransferCancel(cancel, failed)
        self.finished, self.produce, self.emit = finished, produce, emit
        self.published = published
        self.complete, self.error, self.fallback = False, '', ''
        self.thread = threading.Thread(target=self._run, name='Archive prefix and producer', daemon=True)

    def full_file(self, reason):
        self.fallback = reason
        self.emit('mode-selected', mode='complete', reason=reason)

    def _run(self):
        try:
            for size in PREFIX_STEPS:
                while self.path.stat().st_size < size:
                    check_cancel(self.cancel)
                    if self.finished.is_set():
                        self.full_file('download-ended-before-prefix')
                        return
                    self.cancel.wait(.1)
                check_cancel(self.cancel)
                self.emit('prefix-analysis', received=size)
                began = time.monotonic()
                result = probe_prefix(self.path, self.settings, self.cancel, size)
                self.emit('prefix-result', received=size, elapsed=time.monotonic()-began,
                          outcome='sufficient' if result.media else 'retry' if result.retry else 'fallback',
                          reason=result.reason, container=result.media['container'] if result.media else '')
                if result.media:
                    self.emit('prefix-sufficient', received=size, container=result.media['container'])
                    self.emit('mode-selected', mode='progressive', container=result.media['container'])
                    self.emit('producer-start', mode='progressive')
                    self.produce(result.media, self.cancel)
                    self.complete = True
                    return
                self.emit('prefix-insufficient', received=size, reason=result.reason)
                if not result.retry:
                    self.full_file(result.reason)
                    return
            self.full_file('prefix-budget-exhausted')
        except Cancelled:
            self.error = 'cancelled'
        except Exception as exc:
            self.error = exc.code if isinstance(exc, PlaybackError) else 'preparation-incomplete'
            if self.published():
                self.emit('producer-failed', mode='stopped', reason=self.error)
            else:
                self.full_file(self.error)

    def start(self):
        self.thread.start()

    def join(self):
        # Owned FFmpeg/probe is cancelled by TransferCancel on transfer failure.
        # Only the coordinator waits here, never Tk.
        while self.thread.is_alive():
            self.thread.join(.1)
