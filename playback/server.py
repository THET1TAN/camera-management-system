"""Session-only loopback HTTP with opaque URLs and a bounded connection pool."""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import math
from pathlib import Path
import re
import secrets
from socketserver import ThreadingMixIn
import threading
from urllib.parse import urlsplit

from .model import PlaybackError


class BoundedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 8

    def __init__(self, handler):
        self.slots = threading.BoundedSemaphore(6)
        super().__init__(('127.0.0.1', 0), handler)

    def process_request(self, request, address):
        if not self.slots.acquire(False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


class SessionServer:
    def __init__(self):
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.resources = {}
        self.active_readers = 0
        self.drained = threading.Event()
        self.drained.set()
        owner = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.0'
            def log_message(self, *args):
                pass  # Request paths contain a private session token.
            def setup(self):
                super().setup()
                self.connection.settimeout(3)
            def do_HEAD(self):
                self.serve(False)
            def do_GET(self):
                self.serve(True)
            def serve(self, send_body):
                parts = urlsplit(self.path)
                expected_host = f'127.0.0.1:{owner.port}'
                if (parts.query or parts.fragment or self.headers.get('Host') != expected_host or
                        not parts.path.startswith('/'+owner.token+'/')):
                    self.send_error(404)
                    return
                name = parts.path[len(owner.token)+2:]
                with owner.lock:
                    resource = owner.resources.get(name)
                    if resource is not None:
                        owner.active_readers += 1
                        owner.drained.clear()
                if resource is None:
                    self.send_error(404)
                    return
                data, mime = resource
                handle = None
                try:
                    if isinstance(data, bytes):
                        size = len(data)
                    else:
                        # All resources were registered explicitly by a producer.
                        if data.is_symlink() or not data.is_file():
                            self.send_error(404)
                            return
                        handle = data.open('rb')
                        size = data.stat().st_size
                    start, end = 0, size-1
                    ranged = self.headers.get('Range')
                    if ranged:
                        match = re.fullmatch(r'bytes=(\d+)-(\d*)', ranged)
                        if not match:
                            self.send_error(416)
                            return
                        start = int(match[1])
                        end = min(end, int(match[2])) if match[2] else end
                        if not 0 <= start <= end < size:
                            self.send_error(416)
                            return
                    self.send_response(206 if ranged else 200)
                    self.send_header('Content-Type', mime)
                    self.send_header('Content-Length', str(max(0, end-start+1)))
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('X-Content-Type-Options', 'nosniff')
                    self.send_header('Accept-Ranges', 'bytes')
                    if ranged:
                        self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
                    self.end_headers()
                    if send_body:
                        if handle:
                            handle.seek(start)
                            remaining = end-start+1
                            while remaining:
                                chunk = handle.read(min(65536, remaining))
                                if not chunk:
                                    break
                                self.wfile.write(chunk)
                                remaining -= len(chunk)
                        else:
                            self.wfile.write(data[start:end+1])
                except (OSError, ValueError):
                    pass
                finally:
                    if handle:
                        handle.close()
                    with owner.lock:
                        owner.active_readers -= 1
                        if owner.active_readers == 0:
                            owner.drained.set()
        self.http = BoundedServer(Handler)
        self.port = self.http.server_port
        self.thread = threading.Thread(target=self.http.serve_forever, kwargs={'poll_interval': .1}, daemon=True)
        self.thread.start()

    def publish(self, name, data, mime):
        if not re.fullmatch(r'[a-zA-Z0-9_-]+\.(m3u8|ts)', name):
            raise PlaybackError('playlist-invalid')
        with self.lock:
            self.resources[name] = (data, mime)
        return f'http://127.0.0.1:{self.port}/{self.token}/{name}'

    def clear(self):
        with self.lock:
            self.resources.clear()

    def close(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=2)
        self.clear()


class Playlist:
    """One bounded EVENT generation. Native-file completion is not session EOF."""
    def __init__(self, server, requested):
        self.server, self.requested = server, requested
        self.identifier = secrets.token_hex(12)
        self.groups = []
        self.closed = False
        self.url = ''
        self.target_duration = 0  # Measured before the first public generation.

    @property
    def segments(self):
        return tuple(s for _, group in self.groups for s in group)

    @property
    def start(self):
        return self.segments[0].start if self.segments else self.requested

    @property
    def end(self):
        return self.segments[-1].start+self.segments[-1].duration if self.segments else self.requested

    def absolute_at(self, media_seconds):
        elapsed = 0.
        segments = self.segments
        for segment in segments:
            if media_seconds < elapsed+segment.duration:
                return segment.start+max(0., media_seconds-elapsed)
            elapsed += segment.duration
        return self.end if segments else self.requested

    def media_offset(self, stamp):
        elapsed = 0.
        for segment in self.segments:
            if segment.start <= stamp < segment.start+segment.duration:
                return elapsed+stamp-segment.start
            elapsed += segment.duration
        raise PlaybackError('no-video')

    def update(self, key, segments):
        if not segments:
            return
        if not self.url:
            self.target_duration = max(self.target_duration, math.ceil(max(s.duration for s in segments)))
        if any(s.duration > self.target_duration for s in segments):
            raise PlaybackError('long-gop-progressive')
        for n, (old_key, old_segments) in enumerate(self.groups):
            if old_key == key:
                if tuple(segments[:len(old_segments)]) != tuple(old_segments):
                    raise PlaybackError('playlist-rewritten')
                self.groups[n] = (key, segments)
                break
        else:
            if self.groups and abs(segments[0].start-self.end) > .5:
                raise PlaybackError('archive-boundary-gap')
            self.groups.append((key, segments))
        self.publish()

    def publish(self):
        segments = self.segments
        if not segments:
            return
        lines = ['#EXTM3U', '#EXT-X-VERSION:6', '#EXT-X-PLAYLIST-TYPE:EVENT',
            '#EXT-X-MEDIA-SEQUENCE:0', f'#EXT-X-TARGETDURATION:{self.target_duration}',
            f'#EXT-X-START:TIME-OFFSET={max(0., self.requested-self.start):.6f},PRECISE=YES']
        for group_index, (_, group) in enumerate(self.groups):
            if group_index:
                lines.append('#EXT-X-DISCONTINUITY')
            for s in group:
                name = f'{s.archive_key}-{s.path.name}'
                self.server.publish(name, s.path, 'video/mp2t')
                lines.extend(('#EXT-X-PROGRAM-DATE-TIME:'+datetime.fromtimestamp(s.start, timezone.utc).isoformat(),
                              f'#EXTINF:{s.duration:.6f},', name))
        if self.closed:
            lines.append('#EXT-X-ENDLIST')
        self.url = self.server.publish(self.identifier+'.m3u8', ('\n'.join(lines)+'\n').encode(),
                                       'application/vnd.apple.mpegurl')

    def finish(self):
        self.closed = True
        self.publish()

    def rebase(self, requested, target_duration):
        """Called only after the old native owner is reaped; old URLs are retired."""
        self.identifier = secrets.token_hex(12)
        self.requested = requested
        self.target_duration = max(1, math.ceil(target_duration))
        self.closed = False
        self.url = ''
