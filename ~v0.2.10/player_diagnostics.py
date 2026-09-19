"""Bounded, structured player diagnostics; never accept camera/exception text."""
import logging
from logging.handlers import RotatingFileHandler
import os
import re
from pathlib import Path
from queue import Queue, Full
import sys
import threading

_terminal = None
_terminal_lock = threading.Lock()


def terminal_relay():
    global _terminal
    with _terminal_lock:
        if _terminal is None:
            _terminal = TerminalRelay()
        return _terminal


class TerminalRelay:
    """A slow/missing terminal cannot block either Tk or a pipe drainer."""
    def __init__(self):
        self.messages = Queue(maxsize=128)
        threading.Thread(target=self._write, name='Player terminal', daemon=True).start()

    def put(self, text):
        try:
            self.messages.put_nowait(text[:2048])
        except Full:
            pass

    def _write(self):
        while True:
            text = self.messages.get()
            try:
                if sys.stdout is not None:
                    sys.stdout.write(text + '\n')
                    sys.stdout.flush()
            except (OSError, ValueError):
                pass


def make_logger(camera_id, directory=None):
    camera_id = safe_camera_id(camera_id)
    directory = Path(directory or Path(__file__).resolve().parent / 'player-logs')
    logger = logging.getLogger(f'player.{camera_id}.{os.getpid()}')
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            # Different windows may open the same camera. Each process owns its
            # rotation; keep at most 24 older files per ID. Windows refuses to
            # delete files still open by another active player.
            older = sorted(directory.glob(f'camera-{camera_id}-*.log*'), key=lambda p: p.stat().st_mtime, reverse=True)
            for old in older[24:]:
                try:
                    old.unlink()
                except OSError:
                    pass
            handler = RotatingFileHandler(directory / f'camera-{camera_id}-{os.getpid()}.log',
                                          maxBytes=512 * 1024, backupCount=2, encoding='utf-8')
            handler.setFormatter(logging.Formatter(
                f'%(asctime)s camera={camera_id} pid=%(process)d thread=%(threadName)s %(message)s'))
            logger.addHandler(handler)
        except OSError:
            logger.addHandler(logging.NullHandler())
    return logger


def safe_camera_id(value):
    value = str(value)
    return value if re.fullmatch(r'[A-Za-z0-9_-]{1,32}', value) else 'local'


class StackCapture:
    """Opt-in one-shot dumps, rearmed by healthy progress; two bounded files."""
    def __init__(self, camera_id, role):
        import faulthandler
        self.handler = faulthandler
        self.file = None
        value = os.getenv('CAMERA_PLAYER_DUMP_SECONDS', '')
        self.seconds = int(value) if value.isdigit() else 0
        if not 5 <= self.seconds <= 300:
            return
        try:
            directory = Path(__file__).resolve().parent / 'player-logs'
            directory.mkdir(exist_ok=True)
            path = directory / f'camera-{safe_camera_id(camera_id)}-{role}-stacks.log'
            self.path = path
            if path.exists() and path.stat().st_size > 512*1024:
                path.replace(path.with_suffix('.log.1'))
            self.file = path.open('a', encoding='utf-8')
        except OSError:
            self.file = None

    def arm(self):
        if self.file is not None:
            if self.file.tell() > 512*1024:
                self.cancel()
                self.file.close()
                try:
                    self.path.replace(self.path.with_suffix('.log.1'))
                    self.file = self.path.open('a', encoding='utf-8')
                except OSError:
                    self.file = None
                    return
            self.handler.dump_traceback_later(self.seconds, file=self.file, exit=False)

    def cancel(self):
        if self.file is not None:
            self.handler.cancel_dump_traceback_later()

    def close(self):
        self.cancel()
        if self.file is not None:
            self.file.close()
