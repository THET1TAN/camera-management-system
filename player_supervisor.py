"""Tk-facing snapshots and a single supervisor for replaceable VLC processes.

Only the supervisor touches process pipes, waits, discovery or native-operation
deadlines. Each generation has its own pipes; it is reaped before a replacement.
"""
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
from queue import Queue, Empty, Full
import subprocess
import sys
import threading
import time

from player_diagnostics import make_logger, terminal_relay, safe_camera_id


@dataclass(frozen=True)
class PlayerSettings:
    operation_timeout: float = 8.0
    discovery_timeout: float = 15.0
    startup_timeout: float = 25.0
    stall_timeout: float = 15.0
    stable_seconds: float = 30.0
    stop_timeout: float = 1.0
    # Leave room for libVLC's own RTSP timeout and first decodable GOP inside
    # the lab's 20-second recovery target; 15 seconds exceeded it in 8/20 trials.
    backoff: tuple = (1., 2., 4., 8.)

    @classmethod
    def from_environment(cls):
        try:
            stall = float(os.getenv('CAMERA_PLAYER_STALL_SECONDS', '15'))
            if not math.isfinite(stall) or not 5 <= stall <= 300:
                raise ValueError
        except ValueError:
            stall = 15.
        return cls(stall_timeout=stall, startup_timeout=max(25., 2*stall))


@dataclass(frozen=True)
class PlayerSnapshot:
    state: str = 'STARTING'
    generation: int = 0
    attempt: int = 0
    reason: str = ''
    bitrate: float = 0.
    width: int = 0
    height: int = 0
    received: int = 0
    decoded: int = 0
    displayed: int = 0
    audio: int = 0


class Progress:
    """Counters, not Playing/TimeChanged/Vout events, establish video progress."""
    def __init__(self, now, settings):
        self.settings = settings
        self.started = self.last_video = now
        self.last_input = self.last_decode = now
        self.last = None
        self.video_baseline = None
        self.playing_since = None

    def observe(self, sample, now):
        # VLC can present the last picture repeatedly. Require new decoded AND
        # presented pictures, allowing their increments to straddle samples.
        video = (sample.get('decoded', 0), sample['displayed'])
        if self.video_baseline is None or (self.last is not None and (
                video[0] < self.last.get('decoded', 0) or video[1] < self.last['displayed'])):
            self.video_baseline = video  # Counter reset/wrap is not progress.
        elif all(value > baseline for value, baseline in zip(video, self.video_baseline)):
            self.video_baseline = video
            self.last_video = now
            if self.playing_since is None:
                self.playing_since = now
        if self.last is not None:
            if sample.get('received', 0) > self.last.get('received', 0):
                self.last_input = now
            if sample.get('decoded', 0) > self.last.get('decoded', 0):
                self.last_decode = now
        self.last = sample

    def failure(self, now):
        if self.playing_since is None:
            return 'startup-no-video' if now - self.started >= self.settings.startup_timeout else None
        if now - self.last_video >= self.settings.stall_timeout:
            if now - self.last_input >= self.settings.stall_timeout:
                return 'no-input-progress'
            if now - self.last_decode >= self.settings.stall_timeout:
                return 'no-decode-progress'
            return 'no-display-progress'
        return None

    def stable(self, now):
        return self.playing_since is not None and now - self.playing_since >= self.settings.stable_seconds


class PlayerSupervisor:
    def __init__(self, camera_id, config, hwnd, settings=PlayerSettings(), *,
                 worker_command=None, log_directory=None):
        self.config = dict(config, hwnd=int(hwnd), camera_id=safe_camera_id(camera_id))
        self.settings = settings
        self.worker_command = worker_command or [sys.executable, '-u',
            str(Path(__file__).with_name('player_worker.py'))]
        self._snapshot = PlayerSnapshot()
        self._stop = threading.Event()
        self.closed = threading.Event()
        self._audio = (False, 100)
        self._heartbeat = time.monotonic()
        self._log_directory = log_directory
        self.camera_id = safe_camera_id(camera_id)
        self.worker_pid = None
        self.thread = threading.Thread(target=self._run, name='Player supervisor', daemon=True)
        self.thread.start()

    @property
    def snapshot(self):
        return self._snapshot  # Immutable, atomically replaced by the supervisor.

    def heartbeat(self):
        self._heartbeat = time.monotonic()

    def set_audio(self, muted, volume):
        self._audio = (bool(muted), max(0, min(100, int(volume))))

    def close(self):
        self._stop.set()  # Never waits on a lock held across I/O.

    def _state(self, state, generation, attempt, reason=''):
        previous = self._snapshot.state
        self._snapshot = PlayerSnapshot(state, generation, attempt, reason)
        self._log('state', generation, f'{previous}->{state} reason={reason} attempt={attempt}')

    def _log(self, event, generation, detail=''):
        text = f'mono={time.monotonic():.3f} generation={generation} {event} {detail}'
        self.logger.info(text)
        if event != 'metrics' and (event != 'operation' or not detail.startswith('stats ')):
            self.terminal.put(f'[Player {self.camera_id}] {text}')

    @staticmethod
    def _read_output(stream, messages):
        try:
            while True:
                line = stream.readline(8192)
                if not line:
                    break
                if not line.endswith(b'\n'):
                    continue
                try:
                    message = json.loads(line)
                    if isinstance(message, dict):
                        messages.put_nowait(message)
                except (ValueError, Full):
                    pass
        except (OSError, ValueError):
            pass

    @staticmethod
    def _send(process, message):
        process.stdin.write((json.dumps(message) + '\n').encode())
        process.stdin.flush()

    def _retire(self, process, reader, messages, generation):
        self._log('shutdown-enter', generation)
        try:
            process.stdin.close()  # EOF requests cleanup, with an independent worker watchdog.
        except OSError:
            pass
        deadline = time.monotonic() + self.settings.stop_timeout
        while process.poll() is None and time.monotonic() < deadline:
            self._cleanup_messages(messages, generation)
            time.sleep(.02)  # Supervisor only; Tk keeps running.
        if process.poll() is None:
            self._log('shutdown-kill', generation)
            process.kill()  # Only this owned session, never a PID/name-wide kill.
            process.wait(timeout=3)
        reader.join(timeout=1)
        self._cleanup_messages(messages, generation)
        process.stdout.close()
        self.worker_pid = None
        self._log('shutdown-exit', generation)

    def _cleanup_messages(self, messages, generation):
        for _ in range(128):
            try:
                message = messages.get_nowait()
            except Empty:
                break
            if message.get('generation') == generation and message.get('kind') == 'operation':
                name, phase = message.get('name'), message.get('phase')
                if name in {'detach', 'stop', 'release-player', 'release-media', 'release-instance'} and phase in {'enter', 'exit'}:
                    self._log('operation', generation, f'{name} {phase}')

    def _session(self, generation, attempt):
        messages = Queue(maxsize=128)
        env = dict(os.environ)
        env.pop('CAMERA_PARENT_PIPE', None)  # This worker has a separate, private protocol.
        process = subprocess.Popen(self.worker_command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env, bufsize=0,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
        self.worker_pid = process.pid
        reader = threading.Thread(target=self._read_output, args=(process.stdout, messages),
                                  name='VLC status pipe', daemon=True)
        reader.start()
        now = time.monotonic()
        progress = Progress(now, self.settings)
        operation, op_started = 'boot', now
        last_message = now
        last_metrics = now
        sent_audio = self._audio
        reason = 'worker-exited'
        reset_attempt = False
        heartbeat_warned = False
        self._log('session-enter', generation, f'worker_pid={process.pid}')
        try:
            self._send(process, dict(self.config, generation=generation,
                                    muted=sent_audio[0], volume=sent_audio[1]))
            while not self._stop.wait(.05):
                now = time.monotonic()
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
                    kind = message.get('kind')
                    if kind == 'operation':
                        name = message.get('name')
                        if name in {'discover', 'create', 'set-media', 'attach', 'play', 'audio',
                                    'stats', 'detach', 'stop', 'release-player', 'release-media', 'release-instance'}:
                            entering = message.get('phase') == 'enter'
                            operation, op_started = (name if entering else None), now
                            self._log('operation', generation, f'{name} {message.get("phase")}')
                    elif kind == 'runtime':
                        # Versions are scalar, bounded metadata; no URIs/exceptions/argv.
                        for key in ('python', 'bits', 'python_vlc', 'libvlc', 'dll'):
                            value = str(message.get(key, 'unknown')).replace('\n', '')[:200]
                            self._log('runtime', generation, f'{key}={value}')
                    elif kind == 'sample':
                        sample = {key: max(0, int(message.get(key, 0))) for key in
                                  ('received', 'decoded', 'displayed', 'audio', 'width', 'height')}
                        sample['bitrate'] = max(0., float(message.get('bitrate', 0.)))
                        progress.observe(sample, now)
                        if now - last_metrics >= 5:
                            self._log('metrics', generation, ' '.join(f'{key}={value}' for key, value in sample.items()))
                            last_metrics = now
                        state = 'PLAYING' if progress.playing_since is not None else 'STARTING'
                        if state != self._snapshot.state:
                            self._state(state, generation, attempt, 'video-progress')
                        self._snapshot = PlayerSnapshot(state, generation, attempt, **sample)
                    elif kind == 'failure':
                        reason = message.get('reason')
                        if reason not in {'vlc-error', 'ended', 'graphics-error', 'worker-error'}:
                            reason = 'worker-error'
                        return reason, reset_attempt
                if process.poll() is not None:
                    break
                if self._audio != sent_audio:
                    sent_audio = self._audio
                    self._send(process, {'muted': sent_audio[0], 'volume': sent_audio[1]})
                limit = self.settings.discovery_timeout if operation == 'discover' else self.settings.operation_timeout
                if (operation and now - op_started > limit) or now - last_message > max(limit, 2):
                    reason = 'operation-timeout'
                    self._log(reason, generation, operation or 'status')
                    break
                reason = progress.failure(now)
                if reason:
                    break
                reset_attempt = reset_attempt or progress.stable(now)
                if now - self._heartbeat > 1 and not heartbeat_warned:
                    self._log('tk-heartbeat-late', generation, f'delay={now-self._heartbeat:.3f}')
                    heartbeat_warned = True
                elif now - self._heartbeat <= 1:
                    heartbeat_warned = False
            return reason or 'closed', reset_attempt
        finally:
            # Clear the stale picture/status before waiting on native cleanup.
            self._state('STOPPING' if self._stop.is_set() else 'RECONNECTING', generation, attempt, reason or '')
            self._retire(process, reader, messages, generation)

    def _run(self):
        self.logger = make_logger(self.camera_id, self._log_directory)
        self.terminal = terminal_relay()
        self._log('runtime', 0, f'python_executable={sys.executable} tk={self.config.get("tk_version", "unknown")}')
        generation = attempt = 0
        try:
            while not self._stop.is_set():
                generation += 1
                self._state('STARTING' if generation == 1 else 'RECONNECTING', generation, attempt)
                try:
                    reason, stable = self._session(generation, attempt)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    # Do not replace a process whose termination could not be confirmed.
                    if self.worker_pid is not None:
                        self._state('FAILED', generation, attempt, 'cleanup-failed')
                        return
                    reason, stable = 'worker-start-failed', False
                if self._stop.is_set():
                    break
                attempt = 0 if stable else attempt
                delay = self.settings.backoff[min(attempt, len(self.settings.backoff)-1)]
                attempt += 1
                self._state('RECONNECT_WAIT', generation, attempt, reason)
                self._log('retry-wait', generation, f'delay={delay:.3f}')
                deadline = time.monotonic() + delay
                warned = False
                while not self._stop.wait(min(.1, max(0, deadline-time.monotonic()))):
                    if time.monotonic() >= deadline:
                        break
                    if time.monotonic() - self._heartbeat > 1 and not warned:
                        self._log('tk-heartbeat-late', generation, 'during-retry-wait')
                        warned = True
        finally:
            if self._snapshot.state != 'FAILED':
                self._state('CLOSED', generation, attempt)
            self.closed.set()
