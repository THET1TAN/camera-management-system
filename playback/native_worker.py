"""Disposable archive libVLC owner. No Tk and no camera credentials in this process."""
import json
import os
import sys
import threading
import time
from urllib.parse import urlsplit


class FrameGate:
    """Counters are native evidence, not an observation of pixels on screen."""
    def __init__(self, target, baseline, started=None, rate=1., baseline_valid=True):
        self.target, self.baseline = target, baseline
        self.started = time.monotonic() if started is None else started
        self.rate, self.baseline_valid = rate, bool(baseline_valid)
        self.landed = False
        self.previous = None

    def accepts(self, position, video, now=None, valid=True):
        now = time.monotonic() if now is None else now
        elapsed = max(0., now-self.started)
        near = abs(position-self.target) <= 1.
        self.landed = self.landed or near
        plausible = self.target-1. <= position <= self.target+elapsed*self.rate+1.
        moving = self.previous is not None and 0 < position-self.previous <= self.rate*elapsed+1.
        self.previous = position
        if not valid:
            return False
        if not self.baseline_valid or any(a < b for a, b in zip(video, self.baseline)):
            self.baseline = video  # Some native inputs reset statistics on seek.
            self.baseline_valid = True
            return False
        # A moving target may have passed the one-second landing window by the
        # time VLC updates its statistics. Require both fresh counters AND an
        # observed landing or a subsequent plausible, advancing media clock.
        return plausible and (self.landed or moving) and all(a > b for a, b in zip(video, self.baseline))


class Commands:
    def __init__(self, config):
        self.stop = threading.Event()
        self.controls = config['controls']
        self.seek = None

    def read(self):
        pending = bytearray()
        try:
            while True:
                data = os.read(sys.stdin.fileno(), 4096)
                if not data:
                    break
                pending.extend(data)
                if len(pending) > 65536:
                    break
                while b'\n' in pending:
                    line, _, rest = pending.partition(b'\n')
                    pending = bytearray(rest)
                    message = json.loads(line)
                    if 'controls' in message:
                        self.controls = message['controls']
                    if 'seek' in message:
                        self.seek = message['seek']
        except (OSError, ValueError):
            pass
        finally:
            self.stop.set()
            # A dead parent cannot leave stop/release blocked in a native call.
            def watchdog():
                time.sleep(2)
                os._exit(0)
            threading.Thread(target=watchdog, daemon=True).start()


def run(config, commands):
    generation = config['generation']
    def emit(**message):
        print(json.dumps(dict(message, generation=generation)), flush=True)
    def call(name, function, *args):
        emit(kind='operation', name=name, phase='enter')
        value = function(*args)
        emit(kind='operation', name=name, phase='exit')
        return value
    instance = player = media = None
    try:
        parts = urlsplit(config['url'])
        if parts.scheme != 'http' or parts.hostname != '127.0.0.1' or parts.username is not None:
            raise ValueError
        import vlc
        emit(kind='runtime', executable=sys.executable, python=sys.version.split()[0], python_vlc=vlc.__version__,
             libvlc=vlc.libvlc_get_version().decode(errors='replace'))
        instance = call('create', vlc.Instance, '--no-video-title-show', '--verbose=-1',
            '--network-caching=500', '--file-caching=300', '--avcodec-threads=2',
            '--no-video-deco', '--no-skip-frames')
        player = call('create', instance.media_player_new)
        media = call('create', instance.media_new, config['url'])
        offset = max(0., config['offset'])
        call('options', media.add_option, f':start-time={offset:.6f}')
        call('set-media', player.set_media, media)
        call('attach', player.set_hwnd, config['hwnd'])
        desired = dict(commands.controls)
        # Audio is applied before playback as well as after native initialization.
        call('audio', player.audio_set_mute, True)  # Unmute only after the opening frame is confirmed.
        call('audio', player.audio_set_volume, desired['volume'])
        call('rate', player.set_rate, desired['rate'])
        if call('play', player.play) == -1:
            emit(kind='failure', reason='vlc-error')
            return
        applied, seek_id, opening = None, 0, True
        seeking, seek_deadline = offset, time.monotonic()+20
        confirmed_seek, frame_confirmed = 0, False
        preview, holding, gate = False, False, None
        sought = False
        previous_video = (0, 0)
        last_progress = time.monotonic()
        last_evidence = 0.
        while not commands.stop.wait(.1):
            state = call('state', player.get_state)
            if state == vlc.State.Error:
                emit(kind='failure', reason='vlc-error')
                return
            if state == vlc.State.Ended:
                emit(kind='sample', state='ENDED', position=call('time', player.get_time)/1000,
                     rate=call('rate', player.get_rate), decoded=previous_video[0], displayed=previous_video[1],
                     stats_valid=False)
                return
            ready = state in (vlc.State.Playing, vlc.State.Paused)
            controls = dict(commands.controls)
            command = commands.seek
            if command and command.get('generation', generation) == generation and command['id'] != seek_id:
                seek_id = command['id']
                preview = bool(command.get('preview'))
                holding = command['offset'] is None
                seeking = None if holding else max(0., command['offset'])
                seek_deadline, sought, frame_confirmed = time.monotonic()+20, False, False
                gate = None
                applied = None
            effective = dict(controls, muted=controls['muted'] or preview or holding or seeking is not None)
            if ready and (applied != effective or opening):
                call('audio', player.audio_set_volume, int(controls['volume']))
                call('audio', player.audio_set_mute, bool(effective['muted']))
                accepted = call('rate', player.set_rate, float(controls['rate']))
                if accepted == -1:
                    emit(kind='failure', reason='rate-unavailable')
                    return
                if seeking is None:
                    call('pause', player.set_pause, int(controls['paused'] or preview or holding))
                applied = effective
                opening = False
            if ready and seeking is not None and not sought:
                # Do not rely on HLS duration or its default live-edge choice.
                baseline = vlc.MediaStats()
                valid_baseline = call('stats', media.get_stats, baseline)
                gate = FrameGate(seeking, (baseline.decoded_video, baseline.displayed_pictures)
                                 if valid_baseline else previous_video, rate=float(controls['rate']),
                                 baseline_valid=valid_baseline)
                emit(kind='evidence', event='native-seek-issued', seek_id=seek_id,
                     target=seeking, baseline_decoded=gate.baseline[0], baseline_displayed=gate.baseline[1],
                     baseline_valid=bool(valid_baseline), origin=(command or {}).get('origin', config.get('origin', 'initial-open')))
                call('pause', player.set_pause, 0)
                call('seek', player.set_time, int(seeking*1000))
                sought = True
            position = max(0., call('time', player.get_time)/1000)
            stats = vlc.MediaStats()
            valid = call('stats', media.get_stats, stats)
            video = (stats.decoded_video, stats.displayed_pictures) if valid else previous_video
            if valid and any(a < b for a, b in zip(video, previous_video)):
                previous_video = video
                last_progress = time.monotonic()
            if all(a > b for a, b in zip(video, previous_video)):
                previous_video = video
                last_progress = time.monotonic()
            if seeking is not None:
                accepted_frame = ready and gate is not None and gate.accepts(position, video, valid=bool(valid))
                if time.monotonic()-last_evidence >= .5:
                    last_evidence = time.monotonic()
                    emit(kind='evidence', event='native-seek-evidence', seek_id=seek_id, target=seeking,
                         position=position, decoded=video[0], displayed=video[1], stats_valid=bool(valid),
                         baseline_decoded=gate.baseline[0] if gate else 0,
                         baseline_displayed=gate.baseline[1] if gate else 0,
                         baseline_valid=bool(gate and gate.baseline_valid),
                         elapsed=last_evidence-gate.started if gate else 0., state=str(state),
                         rate=float(controls['rate']), paused=bool(controls['paused']), preview=preview,
                         landed=bool(gate and gate.landed))
                if accepted_frame:
                    seeking = None
                    confirmed_seek, frame_confirmed = seek_id, True
                    call('pause', player.set_pause, int(controls['paused'] or preview))
                    applied = None  # Restore user's mute state only after the new frame.
                elif time.monotonic() > seek_deadline:
                    emit(kind='failure', reason='seek-unavailable', seek_id=seek_id, position=position,
                         target=seeking, decoded=video[0], displayed=video[1], stats_valid=bool(valid))
                    return
            rate = call('rate', player.get_rate)
            if ready and abs(rate-float(controls['rate'])) > .01:
                emit(kind='failure', reason='rate-unavailable')
                return
            label = ('SEEKING' if seeking is not None else 'PAUSED' if (controls['paused'] or preview or holding) and ready else
                     'BUFFERING' if not ready or time.monotonic()-last_progress > 2 else 'PLAYING')
            emit(kind='sample', state=label, position=position, rate=rate,
                 decoded=video[0], displayed=video[1], audio=stats.played_abuffers if valid else 0,
                 confirmed_seek=confirmed_seek, frame_confirmed=frame_confirmed, stats_valid=bool(valid))
    except Exception:
        emit(kind='failure', reason='native-unavailable')
    finally:
        try:
            if player is not None:
                call('stop', player.stop)
                call('detach', player.set_hwnd, 0)
                call('release', player.release)
            if media is not None:
                call('release', media.release)
            if instance is not None:
                call('release', instance.release)
        except Exception:
            pass


def main():
    line = bytearray()
    while len(line) < 65536:
        chunk = os.read(sys.stdin.fileno(), 1)
        if not chunk or chunk == b'\n':
            break
        line.extend(chunk)
    try:
        config = json.loads(line)
        commands = Commands(config)
        threading.Thread(target=commands.read, daemon=True).start()
        run(config, commands)
    except Exception:
        pass
