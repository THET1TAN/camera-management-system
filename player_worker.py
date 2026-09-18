"""One disposable libVLC session. No Tk import; all native calls have one owner."""
import json
import os
from queue import Queue, Empty, Full
import struct
import sys
import threading
import time
from urllib.parse import quote, urlsplit, urlunsplit
from xml.sax.saxutils import escape
from player_diagnostics import StackCapture


def authenticated_uri(uri, username, password):
    parts = urlsplit(uri)
    if parts.scheme not in ('rtsp', 'rtsps') or not parts.hostname:
        raise ValueError('Invalid stream address')
    # Keep the discovered host, port, path, query and profile, including IPv6.
    if parts.username is None and username:
        authority = quote(username, safe='') + ':' + quote(password, safe='') + '@' + parts.netloc
        uri = urlunsplit((parts.scheme, authority, parts.path, parts.query, parts.fragment))
    return uri


def discover_uri(config, stop, request=None):
    from camera_health import CameraTarget, DEVICE, MEDIA, SCHEMA, device_url, soap_request
    if config.get('uri'):
        return authenticated_uri(config['uri'], config.get('username', ''), config.get('password', ''))
    request = request or soap_request
    target = CameraTarget(0, config['host'], config.get('username', ''), config.get('password', ''))
    caps = request(device_url(target), target, DEVICE + '/GetCapabilities',
        f'<tds:GetCapabilities xmlns:tds="{DEVICE}"><tds:Category>Media</tds:Category></tds:GetCapabilities>', 2., stop)
    media_url = caps.findtext(f'.//{{{SCHEMA}}}Media/{{{SCHEMA}}}XAddr')
    if not media_url:
        raise ValueError('Missing media service')
    profiles = request(media_url, target, MEDIA + '/GetProfiles', f'<trt:GetProfiles xmlns:trt="{MEDIA}"/>', 2., stop)
    profile = profiles.find(f'.//{{{MEDIA}}}Profiles')
    if profile is None or not profile.get('token'):
        raise ValueError('Missing profile')
    body = (f'<trt:GetStreamUri xmlns:trt="{MEDIA}" xmlns:tt="{SCHEMA}">'
        '<trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream><tt:Transport>'
        '<tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>'
        f'<trt:ProfileToken>{escape(profile.get("token"))}</trt:ProfileToken></trt:GetStreamUri>')
    result = request(media_url, target, MEDIA + '/GetStreamUri', body, 2., stop)
    return authenticated_uri(result.findtext(f'.//{{{SCHEMA}}}Uri') or '', target.username, target.password)


class Notifications:
    """Callbacks copy only primitive data. Full queues drop duplicates, never wait."""
    def __init__(self, generation):
        self.generation = generation
        self.queue = Queue(maxsize=32)

    def publish(self, kind):
        try:
            self.queue.put_nowait((self.generation, kind))
        except Full:
            pass

    def callback(self, kind):
        def notify(_event):
            self.publish(kind)
        return notify


class Commands:
    def __init__(self, config):
        self.stop = threading.Event()
        self.audio = (config.get('muted', False), config.get('volume', 100))

    def read(self):
        try:
            pending = b''
            while True:
                data = os.read(sys.stdin.fileno(), 4096)
                if not data:
                    break
                pending += data
                if len(pending) > 65536:
                    break
                while b'\n' in pending:
                    line, pending = pending.split(b'\n', 1)
                    message = json.loads(line)
                    self.audio = (bool(message['muted']), max(0, min(100, int(message['volume']))))
        except (OSError, ValueError, KeyError):
            pass
        finally:
            self.stop.set()
            # Parent crash/EOF must also kill a native call that will never return.
            # Normal cleanup exits sooner. This process owns no PTZ controller.
            time.sleep(2)
            os._exit(0)


def run(config, commands, vlc_module=None, emit=None):
    generation = config['generation']
    dumps = StackCapture(config.get('camera_id', 'local'), 'worker')
    if emit is None:
        def emit(message):
            print(json.dumps(dict(message, generation=generation)), flush=True)

    def call(name, function, *args):
        emit({'kind': 'operation', 'name': name, 'phase': 'enter'})
        dumps.arm()
        try:
            result = function(*args)
        finally:
            dumps.cancel()
        emit({'kind': 'operation', 'name': name, 'phase': 'exit'})
        return result

    instance = player = media = manager = None
    attached = []
    notifications = Notifications(generation)
    try:
        uri = call('discover', discover_uri, config, commands.stop)
        if commands.stop.is_set():
            return
        if vlc_module is None:
            import vlc as vlc_module
        vlc = vlc_module
        emit({'kind': 'runtime', 'python': sys.version.split()[0], 'bits': struct.calcsize('P')*8,
              'python_vlc': vlc.__version__, 'libvlc': vlc.libvlc_get_version().decode(errors='replace'),
              'dll': str(vlc.dll._name)})
        instance = call('create', vlc.Instance, '--rtsp-tcp', '--network-caching=300',
                        '--no-video-title-show', '--avcodec-threads=2', '--verbose=-1')
        # Do not format/retain arbitrary native messages: they can contain secrets.
        @vlc.CallbackDecorators.LogCb
        def native_log(_data, _level, _ctx, fmt, _args):
            if fmt and any(pattern in fmt.lower() for pattern in
                    (b'swapchain present failed', b'device removed', b'dxgi_error_device_removed')):
                notifications.publish('graphics-error')
        call('attach', instance.log_set, native_log, None)
        player = call('create', instance.media_player_new)
        media = call('create', instance.media_new, uri)
        call('set-media', player.set_media, media)
        call('attach', player.set_hwnd, int(config['hwnd']))
        manager = call('attach', player.event_manager)
        for event, name in ((vlc.EventType.MediaPlayerEncounteredError, 'vlc-error'),
                            (vlc.EventType.MediaPlayerEndReached, 'ended'),
                            (vlc.EventType.MediaPlayerESDeleted, 'es-deleted')):
            call('attach', manager.event_attach, event, notifications.callback(name))
            attached.append(event)
        if commands.stop.is_set():
            return
        if call('play', player.play) == -1:
            emit({'kind': 'failure', 'reason': 'vlc-error'})
            return
        audio = None
        previous_bytes, previous_time = 0, time.monotonic()
        while not commands.stop.is_set():
            for _ in range(32):
                try:
                    event_generation, name = notifications.queue.get_nowait()
                except Empty:
                    break
                if event_generation == generation and name != 'es-deleted':
                    emit({'kind': 'failure', 'reason': name})
                    return
                # ES deletion alone is valid during codec negotiation. The video
                # progress deadline detects an actual loss, including no event.
            if commands.audio != audio:
                audio = commands.audio
                call('audio', player.audio_set_mute, audio[0])
                call('audio', player.audio_set_volume, audio[1])
            stats = vlc.MediaStats()
            valid = call('stats', media.get_stats, stats)
            width, height = call('stats', player.video_get_size, 0)
            now = time.monotonic()
            received = stats.demux_read_bytes if valid else previous_bytes
            rate = max(0, received - previous_bytes) * 8 / max(.001, now-previous_time) / 1_000_000
            previous_bytes, previous_time = received, now
            emit({'kind': 'sample', 'received': received, 'decoded': stats.decoded_video if valid else 0,
                  'displayed': stats.displayed_pictures if valid else 0, 'audio': stats.played_abuffers if valid else 0,
                  'bitrate': rate, 'width': width, 'height': height})
            commands.stop.wait(.5)
    except Exception:
        # Exception bodies, SOAP, URIs and tracebacks can disclose credentials.
        emit({'kind': 'failure', 'reason': 'worker-error'})
    finally:
        # One owner, callbacks still alive until detached. A deadline in the
        # supervisor covers *every* operation here, even stop/release that hangs.
        try:
            if manager is not None:
                for event in attached:
                    call('detach', manager.event_detach, event)
            if player is not None:
                call('stop', player.stop)
                call('detach', player.set_hwnd, 0)
                call('release-player', player.release)
            if media is not None:
                call('release-media', media.release)
            if instance is not None:
                call('detach', instance.log_unset)
                call('release-instance', instance.release)
        except Exception:
            pass  # Process exit releases any remaining native resources.
        finally:
            dumps.close()


def main():
    # Raw reads avoid holding Python's buffered-stdin lock in a daemon thread
    # during interpreter finalization after an early VLC error.
    line = bytearray()
    while len(line) < 65536:
        data = os.read(sys.stdin.fileno(), 1)
        if not data or data == b'\n':
            break
        line.extend(data)
    config = json.loads(line)
    commands = Commands(config)
    threading.Thread(target=commands.read, name='VLC parent lifetime', daemon=True).start()
    run(config, commands)


if __name__ == '__main__':
    main()
