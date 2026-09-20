"""Nonblocking archive supervisor; pause/EOF never trigger live RTSP recovery."""
from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path
from queue import Queue, Empty
import subprocess
import sys
import threading
import time

from player_supervisor import PlayerSupervisor
from .model import Controls
from .processes import Process


@dataclass(frozen=True)
class NativeSnapshot:
    state: str = 'IDLE'
    generation: int = 0
    position: float = 0.
    rate: float = 1.
    reason: str = ''
    decoded: int = 0
    displayed: int = 0
    confirmed_seek: int = 0
    stats_valid: bool = False
    audio: int = 0


class Engine:
    def __init__(self, hwnd, event=None):
        self.hwnd = int(hwnd)
        self.controls = Controls()
        self.snapshot = NativeSnapshot()
        self.runtime = {}
        self.request = None
        self.seek_request = None
        self.serial = 0
        self.event = event or (lambda *_args, **_kwargs: None)
        self.stop_event = threading.Event()
        self.closed = threading.Event()
        self.idle = threading.Event()
        self.idle.set()
        self.thread = threading.Thread(target=self._run, name='Archive native supervisor', daemon=True)
        self.thread.start()

    def open(self, url, offset, origin='initial-open'):
        self.serial += 1
        self.seek_request = None
        self.request = (self.serial, url, offset)
        self.open_origin = (self.serial, origin)
        self.idle.clear()
        self.snapshot = NativeSnapshot('STARTING', self.serial)
        self.event('player-open', generation=self.serial, position=offset, origin=origin)
        return self.serial

    def seek(self, offset, preview=False, origin='user'):
        self.serial += 1
        self.seek_request = {'id': self.serial, 'offset': max(0., offset) if offset is not None else None,
                             'preview': bool(preview), 'origin': origin,
                             'generation': self.request[0] if self.request else 0}
        return self.serial

    def stop(self):
        self.request = None
        self.seek_request = None

    def close(self):
        self.stop_event.set()

    def _session(self, request):
        generation, url, offset = request
        messages = Queue(maxsize=128)
        owned = Process([sys.executable, '-u', str(Path(__file__).resolve().parent.parent/'playback_worker.py')],
                        stdin=subprocess.PIPE)
        process = owned.process
        reader = threading.Thread(target=PlayerSupervisor._read_output, args=(process.stdout, messages), daemon=True)
        # Native diagnostics never reach terminal/logs, even on early import failure.
        def drain():
            while process.stderr.read(8192):
                pass
        stderr = threading.Thread(target=drain, daemon=True)
        reader.start()
        stderr.start()
        sent_controls, sent_seek = self.controls, None
        confirmed_frames = set()
        now = time.monotonic()
        operation, entered, last_message = 'boot', now, now
        try:
            PlayerSupervisor._send(process, {'generation': generation, 'url': url, 'offset': offset,
                'hwnd': self.hwnd, 'controls': asdict(sent_controls),
                'origin': self.open_origin[1] if self.open_origin[0] == generation else 'superseded'})
            while not self.stop_event.wait(.05) and self.request == request:
                now = time.monotonic()
                # The final ENDED/failure message may still be in the pipe when
                # the process exits. Let its reader drain before judging exit.
                exited = process.poll() is not None
                if exited:
                    reader.join(timeout=.2)
                for _ in range(128):
                    try:
                        message = messages.get_nowait()
                    except Empty:
                        break
                    if message.get('generation') != generation:
                        continue
                    last_message = now
                    if operation == 'boot':
                        operation = None
                    if message.get('kind') == 'runtime':
                        self.runtime = {'python': message.get('python', ''),
                            'python_executable_matches_parent': message.get('executable') == sys.executable}
                    elif message.get('kind') == 'operation':
                        operation = message.get('name') if message.get('phase') == 'enter' else None
                        entered = now
                    elif message.get('kind') == 'failure':
                        allowed = {'vlc-error', 'native-unavailable', 'rate-unavailable', 'seek-unavailable'}
                        reason = message.get('reason') if message.get('reason') in allowed else 'native-unavailable'
                        detail = {k:v for k,v in message.items() if k not in ('kind', 'reason', 'generation')}
                        self.event('native-failure', generation=generation, reason=reason, **detail)
                        self.snapshot = replace(self.snapshot, state='FAILED', generation=generation, reason=reason)
                        return
                    elif message.get('kind') == 'evidence':
                        event = message.get('event')
                        if event in ('native-seek-issued', 'native-seek-evidence'):
                            self.event(event, **{k:v for k,v in message.items() if k not in ('kind', 'event')})
                    elif message.get('kind') == 'sample':
                        self.snapshot = NativeSnapshot(message.get('state', 'BUFFERING'), generation,
                            float(message.get('position', 0)), float(message.get('rate', 1)), '',
                            int(message.get('decoded', 0)), int(message.get('displayed', 0)),
                            int(message.get('confirmed_seek', 0)), bool(message.get('stats_valid', False)),
                            int(message.get('audio', 0)))
                        confirmed = self.snapshot.confirmed_seek
                        latest = self.seek_request
                        if (message.get('frame_confirmed') and confirmed not in confirmed_frames and
                                (latest is None or latest['id'] == confirmed)):
                            confirmed_frames.add(confirmed)
                            # Keep bounded even during long continuous scrubbing.
                            confirmed_frames = {confirmed}
                            self.event('native-new-frame', generation=generation, seek_id=confirmed,
                                       position=self.snapshot.position, decoded=self.snapshot.decoded,
                                       displayed=self.snapshot.displayed, evidence='libvlc-counter-delta')
                if exited:
                    if self.snapshot.state != 'ENDED':
                        self.snapshot = replace(self.snapshot, state='FAILED', reason='native-unavailable')
                    return
                if self.controls != sent_controls:
                    sent_controls = self.controls
                    PlayerSupervisor._send(process, {'controls': asdict(sent_controls)})
                if self.seek_request is not None and self.seek_request != sent_seek:
                    sent_seek = self.seek_request
                    PlayerSupervisor._send(process, {'seek': sent_seek})
                if (operation and now-entered > 8) or now-last_message > 12:
                    self.snapshot = NativeSnapshot('FAILED', generation, reason='native-timeout')
                    return
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass
            deadline = time.monotonic()+1
            while process.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            owned.close()
            reader.join(timeout=1)
            stderr.join(timeout=1)

    def _run(self):
        done = None
        try:
            while not self.stop_event.wait(.05):
                request = self.request
                if request is not None and request != done:
                    try:
                        self._session(request)
                    except Exception:
                        self.snapshot = NativeSnapshot('FAILED', request[0], reason='native-unavailable')
                    done = request
                    self.idle.set()
                elif request is None:
                    done = None
                    self.snapshot = NativeSnapshot()
                    self.idle.set()
        finally:
            self.idle.set()
            self.closed.set()
