"""Latest-request scheduling. Workers publish snapshots; only UI code touches Tk."""
import calendar
from dataclasses import dataclass, replace, asdict
from datetime import date, datetime, timedelta
import hashlib
import json
import os
import math
from pathlib import Path
import shutil
import threading
import time
from zoneinfo import ZoneInfo

from .config import ROOT, load_cameras, load_settings, save_settings
from .engine import Engine
from .media import probe, prepare, read_segments, growing_chunks, packet_bounds
from .model import Controls, PlaybackError, Cancelled, check_cancel, day_bounds, covered_days
from .processes import run
from .server import SessionServer, Playlist
from .store import Store
from .diagnostics import Diagnostics, safe_fields
from .remote import RemoteBackend
from .progressive import ProgressivePreparation
from .preparation import PreparationJob


@dataclass(frozen=True)
class Status:
    state: str = 'INITIALIZING'
    camera_id: int = 0
    position: float = 0.
    reason: str = ''
    received: int = 0
    expected: int = 0
    prepared_end: float = 0.
    key: str = ''
    track: str = ''
    source: str = ''
    preview_position: float = 0.


class Controller:
    def __init__(self, hwnd, root=ROOT, fixture_directory=None):
        self.root, self.hwnd = root, hwnd
        self.fixture_directory = fixture_directory
        self.fixture_data = None
        self.connect_backend = lambda camera, cancel: RemoteBackend(camera, cancel, self._diagnostic)
        self.log = None
        self.status = Status()
        self.controls = Controls()
        self.settings = None
        self.cameras = ()
        self.entries = ()
        self.calendar = {}
        self.devices = {}
        self.diagnostic_events = {}
        self.diagnostic_lock = threading.Lock()
        self.index_request_lock = threading.Lock()
        self.index_idle = threading.Event()
        self.index_idle.set()
        self.settings_request = None
        self.configuration_status = ''
        self.ready = threading.Event()
        self.closed = threading.Event()
        self.stop_event = threading.Event()
        self.cancel = threading.Event()
        self.index_cancel = threading.Event()
        self.request = None
        self.month_request = None
        self.serial = 0
        self.export_request = None
        self.export_status = ''
        self.export_cancel = threading.Event()
        self.store = self.engine = self.server = None
        self.index_thread = None
        self.active_playlist = None
        self.active_request = None
        self.seek_serial = 0
        self.seek_target = None
        self.sent_seek = None
        self.open_seek_token = None
        self.session_ranges = ()
        self.preview_position = 0.
        self.playlist_changing = threading.Event()
        self.playback_lock = threading.RLock()
        self.seek_thread = None
        self.resume_target = None
        self.open_origin = 'initial-open'
        self.thread = threading.Thread(target=self._run, name='Archive coordinator', daemon=True)
        self.thread.start()

    def month(self, year, month, camera_ids, force=False, day=None):
        with self.index_request_lock:
            if self.configuration_status == 'pending':
                return
            self.index_cancel.set()
            self.month_request = (year, month, tuple(camera_ids), bool(force), time.monotonic(), day)

    def _diagnostic(self, detail):
        detail = safe_fields(detail)
        cid = detail.get('camera_id')
        if cid is None:
            return
        with self.diagnostic_lock:
            history = self.diagnostic_events.get(cid, ())
            updated = dict(self.diagnostic_events)
            updated[cid] = (*history[-31:], detail)
            self.diagnostic_events = updated
        if self.log:
            self.log.event('camera-protocol', **detail)
            if detail.get('stage') == 'first-byte':
                self.log.event('first-byte', generation=self.active_request[0] if self.active_request else self.serial,
                               **detail)

    def _native_event(self, event, **values):
        active = self.active_request
        if not self.log or not active or active != self.request:
            return False
        if 'generation' in values and (not self.engine.request or self.engine.request[0] != values['generation']):
            return False
        playlist = self.active_playlist
        if playlist:
            values.setdefault('playlist_id', playlist.identifier)
            resource = values.get('resource_id', '')
            if '-segment-' in resource:
                values.setdefault('archive_id', resource.split('-segment-', 1)[0][:12])
            elif resource.endswith('.m3u8'):
                values['playlist_id'] = resource[:-5]
            if 'target' in values:
                values.setdefault('relative_target', values['target'])
            if 'position' in values:
                stamp = values['position'] if event == 'screen-frame-observed' else playlist.absolute_at(values['position'])
                values.setdefault('absolute_position', stamp)
                segment = next((s for s in playlist.segments if s.start <= stamp < s.start+s.duration), None)
                if segment:
                    values.setdefault('archive_id', segment.archive_key[:12])
        if event == 'native-new-frame' and self.sent_seek and self.seek_target:
            marker, native_id = self.sent_seek
            if (values.get('seek_id') == native_id and marker[0] == self.seek_target[0] and
                    marker[2] == values.get('generation') and not self.seek_target[3]):
                # Acknowledgment must not depend on whether Tk has polled view.
                self.seek_target = None
        self.log.event(event, camera_id=active[1], session_id=active[0], **values)
        return True

    def apply_settings(self, settings, camera_id, day):
        """Queue a local reconfiguration; no network/disk/wait in the Tk callback."""
        with self.index_request_lock:
            if self.configuration_status == 'pending':
                return
            self.stop()
            self.export_cancel.set()
            self.configuration_status = 'pending'
            self.index_cancel.set()
            self.month_request = None
            self.settings_request = (settings, camera_id, day)

    def _apply_configuration(self, request):
        settings, cid, day = request
        # No old backend may write metadata after new camera settings take effect.
        while not self.index_idle.wait(.05):
            check_cancel(self.stop_event)
        check_cancel(self.stop_event)
        cameras, _ = load_cameras(settings, self.root)
        if cid not in {camera.camera_id for camera in cameras}:
            raise PlaybackError('camera-unavailable')
        save_settings(settings, self.root)
        self.settings, self.cameras = settings, cameras
        self.store.settings = settings
        self.devices = {}
        self.store.invalidate_searches(cid)
        self.configuration_status = 'complete'
        self.settings_request = None
        # Start with this camera/day, irrespective of the wider calendar filter.
        self.month(day.year, day.month, (cid,), force=True, day=day)

    def select(self, camera_id, stamp):
        self.serial += 1
        self.cancel.set()
        self.request = (self.serial, int(camera_id), float(stamp))
        self.status = Status('LOADING', camera_id, stamp)
        if self.log:
            target = self.seek_target
            self.log.event('request', camera_id=camera_id, generation=self.serial, position=stamp, rate=self.controls.rate,
                           requested_monotonic=target[4] if target and target[1:3] == (camera_id, stamp) else time.monotonic())

    def seek(self, camera_id, stamp, preview=False):
        """Latest target mailbox, separate from transfer/session ownership."""
        if self.seek_target is None and self.active_playlist and self.engine and self.engine.snapshot.displayed:
            self.preview_position = self.active_playlist.absolute_at(self.engine.snapshot.position)
        self.seek_serial += 1
        self.seek_target = (self.seek_serial, int(camera_id), float(stamp), bool(preview), time.monotonic())

    def _seek_loop(self):
        while not self.stop_event.wait(.05):
            try:
                self._service_seek()
            except PlaybackError as exc:
                self.status = replace(self.status, reason=exc.code)

    def _service_seek(self):
        with self.playback_lock:
            target = self.seek_target
            if target is None or self.playlist_changing.is_set():
                return
            token, cid, stamp, preview, requested_at = target
            playlist = self.active_playlist
            active = self.active_request == self.request and self.request is not None and self.request[1] == cid
            available = active and playlist and any(s.start <= stamp < s.start+s.duration for s in playlist.segments)
            receiving = active and any(a <= stamp and (b is None or stamp < b) for a, b in self.session_ranges)
            if not available and not receiving and not preview:
                # Only a committed target outside this source replaces ownership.
                if self.open_seek_token != token:
                    self.open_seek_token = token
                    if not (self.request and self.request[1:] == (cid, stamp)) or self.status.state in ('ERROR','FAILED','GAP','ENDED'):
                        self.select(cid, stamp)
                return
            if not self.engine or not self.engine.request:
                if self.engine and available:
                    self._maybe_open(playlist, stamp, self.request)
                return  # _maybe_open will use the most recent target.
            marker = (token, bool(available), self.engine.request[0])
            if self.sent_seek and self.sent_seek[0] == marker:
                return
            offset = playlist.media_offset(stamp) if available else None
            native_id = self.engine.seek(offset, preview=preview)
            self.sent_seek = (marker, native_id)
            self.log.event('seek-request', camera_id=cid, generation=self.engine.request[0], seek_id=native_id,
                           session_id=self.active_request[0],
                           position=stamp, preview=preview, requested_monotonic=requested_at,
                           relative_target=offset if offset is not None else -1., origin='user',
                           playlist_id=playlist.identifier,
                           reason='' if available else 'target-not-received')
            if available:
                self.log.event('target-available', camera_id=cid, session_id=self.active_request[0],
                               seek_id=native_id, position=stamp, reserve=playlist.end-stamp)

    def stop(self):
        self.seek_target = self.sent_seek = None
        self.request = None
        self.cancel.set()
        self.status = Status('STOPPED', self.status.camera_id, self.status.position)

    def set_controls(self, controls):
        self.controls = controls
        if self.engine:
            self.engine.controls = controls

    def export(self, path, start=None, end=None):
        key = self.view.key
        if self.export_request is not None or not key:
            return
        self.export_cancel = threading.Event()
        self.export_request = (key, Path(path), start, end)
        self.export_status = 'pending'

    def cancel_export(self):
        self.export_cancel.set()

    def close(self):
        self.stop_event.set()
        self.cancel.set()
        self.index_cancel.set()
        self.export_cancel.set()

    @property
    def view(self):
        status = self.status
        if status.state in ('ERROR', 'FAILED', 'CONFIGURATION', 'STOPPED'):
            return status
        playlist = self.active_playlist
        target = self.seek_target
        if target and target[1] != status.camera_id:
            return replace(status, state='PREVIEW_LOADING', camera_id=target[1], position=target[2], preview_position=0.)
        if (playlist is not None and self.engine is not None and self.active_request == self.request
                and self.engine.request is not None):
            native = self.engine.snapshot
            if native.state == 'FAILED':
                return replace(status, state='FAILED', reason=native.reason)
            if native.state == 'ENDED' and not playlist.closed:
                return replace(status, state='BUFFERING', reason='next-archive-pending')
            if native.generation and native.state not in ('IDLE', 'FAILED'):
                position = playlist.absolute_at(native.position)
                target = self.seek_target
                if target and target[1] == status.camera_id:
                    sent = self.sent_seek
                    confirmed = (sent and sent[0] == (target[0], True, native.generation)
                                 and native.confirmed_seek == sent[1])
                    if confirmed:
                        self.preview_position = position
                        if not target[3]:
                            self.seek_target = None
                    else:
                        available = any(s.start <= target[2] < s.start+s.duration for s in playlist.segments)
                        return replace(status, state='SEEKING' if available else 'PREVIEW_LOADING',
                                       position=target[2], preview_position=self.preview_position, prepared_end=playlist.end)
                    if target[3]:
                        return replace(status, state='PREVIEW', position=target[2], preview_position=position,
                                       prepared_end=playlist.end)
                segment = next((s for s in playlist.segments if s.start <= position < s.start+s.duration), None)
                return replace(status, state=native.state, position=position, prepared_end=playlist.end,
                               key=segment.archive_key if segment else status.key)
        target = self.seek_target
        if target and status.state not in ('ERROR', 'FAILED', 'CONFIGURATION', 'GAP', 'STOPPED'):
            return replace(status, state='PREVIEW_LOADING', camera_id=target[1], position=target[2],
                           preview_position=self.preview_position)
        return status

    def _catalog(self, year, month, ids):
        first = date(year, month, 1)
        last = date(year+1, 1, 1) if month == 12 else date(year, month+1, 1)
        start, _ = day_bounds(first, self.settings.display_zone)
        _, end = day_bounds(last-timedelta(days=1), self.settings.display_zone)
        entries = self.store.entries(ids, start, end)
        states = {}
        for n in range((last-first).days):
            day = first+timedelta(days=n)
            a, b = day_bounds(day, self.settings.display_zone)
            for cid in ids:
                known = self.store.search_status(cid, a, b)
                state = 'unknown'
                if known:
                    state = 'configuration' if known[2] == 'timezone-required' else 'empty' if known[1] else 'partial' if known[2] in (
                        'cgi-coverage-unconfirmed', 'cgi-result-limit', 'pagination-limit',
                        'pagination-repeated', 'pagination-count', 'tracks-partial') else 'error'
                states[(cid, day)] = {'state': state, 'checked': known[0] if known else 0.,
                    'reason': known[2] if known else '', 'cached': False, 'present': False}
        for entry in entries:
            for day in covered_days(entry.recording.start, entry.end, self.settings.display_zone):
                key = (entry.recording.camera_id, day)
                if key in states:
                    item = states[key]
                    item['present'] = True
                    if entry.state in ('prepared', 'downloaded', 'preparing'):
                        item['cached'] = True
                    if item['state'] not in ('error', 'partial', 'configuration'):
                        item['state'] = 'present' if entry.remote else 'cache'
        self.entries, self.calendar = entries, states

    def _index(self):
        previous = None
        while not self.stop_event.wait(.1):
            with self.index_request_lock:
                request = self.month_request
                if request is None or request == previous:
                    continue
                previous = request
                self.index_idle.clear()
                cancel = self.index_cancel = threading.Event()
            year, month, ids, force, _, selected_day = request
            backends = {}
            try:
                self._catalog(year, month, ids)
                days = [selected_day] if selected_day else [date(year, month, d) for d in range(1, calendar.monthrange(year, month)[1]+1)]
                # Closest day first; no full-history scan, one metadata worker.
                focus = datetime.fromtimestamp(self.status.position or time.time(), ZoneInfo(self.settings.display_zone)).date()
                days.sort(key=lambda d: abs((d-focus).days))
                for cid in ids:
                    camera = next((c for c in self.cameras if c.camera_id == cid), None)
                    if camera is None:
                        continue
                    failure = None
                    for day in days:
                        check_cancel(cancel)
                        if self.stop_event.is_set() or self.month_request != request:
                            raise Cancelled()
                        a, b = day_bounds(day, self.settings.display_zone)
                        status = self.store.search_status(cid, a, b)
                        if not force and status and time.time()-status[0] < (self.settings.metadata_ttl if status[1] else 60):
                            continue
                        try:
                            if failure:
                                raise PlaybackError(failure)
                            if cid not in backends:
                                backends[cid] = self.connect_backend(camera, cancel)
                                self.devices = dict(self.devices, **{str(cid): backends[cid].device})
                            backend = backends[cid]
                            result = backend.list_recordings(a, b, cancel)
                            self.store.record_search(cid, a, b, backend.device, result)
                            self.log.event('search',camera_id=cid,count=len(result.records),complete=result.complete,reason=result.reason)
                        except Cancelled:
                            raise
                        except PlaybackError as exc:
                            self.store.search_error(cid, a, b, exc.code)
                            if exc.code in ('temporarily-unreachable', 'authentication-failed', 'timezone-required',
                                            'device-identity-required', 'unsupported'):
                                failure = exc.code  # Do not repeat a failing probe 31 times.
                        if self.month_request == request:
                            self._catalog(year, month, ids)
            except Cancelled:
                pass
            except Exception:
                # A broken index never closes the player or publishes a false empty day.
                if self.month_request == request:
                    self.calendar = {key: dict(value, state='error', reason='index-unavailable')
                                     for key, value in self.calendar.items()}
            finally:
                try:
                    for backend in backends.values():
                        try:
                            backend.close()
                        except Exception:
                            pass  # Ownership close already attempts kill/reap in finally.
                finally:
                    self.index_idle.set()

    def _find(self, camera, stamp, cancel, backend_holder, exclude=()):
        def candidates():
            entries = self.store.entries((camera.camera_id,), stamp, stamp+1)
            device = self.devices.get(str(camera.camera_id))
            usable = [e for e in entries if (not camera.track or e.recording.track == camera.track) and
                e.recording.key not in exclude and
                (e.end is None or e.end > stamp) and e.recording.start <= stamp and
                (not device or e.recording.device == device)]
            # An ambiguous CGI end is a candidate, not coverage. Probe before showing it.
            return sorted(usable, key=lambda e: (e.state != 'prepared', not e.remote,
                e.end is None, -e.recording.observed, e.recording.track, -e.recording.start))
        found = candidates()
        if found:
            return found[0]
        day = datetime.fromtimestamp(stamp, ZoneInfo(self.settings.display_zone)).date()
        a, b = day_bounds(day, self.settings.display_zone)
        status = self.store.search_status(camera.camera_id, a, b)
        if status and status[1] and time.time()-status[0] < self.settings.metadata_ttl:
            raise PlaybackError('no-video')
        backend = backend_holder[0] or self.connect_backend(camera, cancel)
        backend_holder[0] = backend
        self.devices = dict(self.devices, **{str(camera.camera_id): backend.device})
        result = backend.list_recordings(a, b, cancel)
        self.store.record_search(camera.camera_id, a, b, backend.device, result)
        found = candidates()
        if not found:
            raise PlaybackError('no-video' if result.complete else 'coverage-unknown')
        return found[0]

    def _prepare(self, entry, camera, playlist, cancel, backend_holder, requested, request, predecessor=None):
        r = entry.recording
        with self.playback_lock:
            self.session_ranges = (*self.session_ranges, (r.start, entry.end))
        self.log.event('archive-prepare', camera_id=camera.camera_id, session_id=request[0],
                       archive_id=r.key[:12], position=r.start, prepared_end=entry.end or 0.,
                       origin='prefetch' if predecessor else 'initial', state=entry.state)
        def publish_ready(segments, final, publish_cancel=cancel):
            # Transfer and remux can overlap, but a later group may not change
            # offsets while its predecessor is still appending segments.
            if predecessor:
                while not predecessor.done.wait(.05):
                    check_cancel(publish_cancel)
                if predecessor.error:
                    raise Cancelled()
            check_cancel(publish_cancel)
            self._publish_segments(playlist, r.key, segments, requested, request, final)
        directory = self.store.path(r.key)
        self.store.pin(r.key)
        directory.mkdir(exist_ok=True)
        media = entry.media
        source = directory/'original.bin'
        if not source.exists() or entry.state not in ('downloaded', 'prepared', 'preparing'):
            if backend_holder[0] is None:
                backend_holder[0] = self.connect_backend(camera, cancel)
            backend = backend_holder[0]
            if isinstance(backend, RemoteBackend):
                backend.diagnostic = lambda detail: self._diagnostic(dict(detail,
                    archive_id=r.key[:12], session_id=request[0]))
            if backend.device != r.device:
                raise PlaybackError('device-changed')
            limit = int(self.settings.max_archive_gib*1024**3)
            if r.size > limit:
                raise PlaybackError('archive-too-large')
            # Reserve the full allowed raw transfer before handing the target to
            # a camera process; its progress reports cannot overshoot the quota.
            self.store.ensure_space(limit)
            self.store.state(r.key, 'partial')
            partial = directory/'original.part'
            last_budget = [0]
            download_done, download_failed = threading.Event(), threading.Event()
            progressive = None
            first_byte = False
            def emit(event, **values):
                self.log.event(event, camera_id=camera.camera_id, generation=request[0], session_id=request[0],
                               archive_id=r.key[:12], backend=r.backend, **values)
                if event == 'mode-selected' and values.get('mode') == 'complete':
                    self.status = replace(self.status, reason=values['reason'])
                elif event == 'producer-failed' and not predecessor:
                    self.status = replace(self.status, state='FAILED', reason=values['reason'])
            def produce(header, producer_cancel):
                def publish(segments, final):
                    check_cancel(producer_cancel)
                    publish_ready(segments, final, producer_cancel)
                prepare('pipe:0', directory, r, header, self.settings, producer_cancel, publish,
                    lambda: self.store.ensure_space(4*1024*1024),
                    growing_chunks(partial, download_done, download_failed, producer_cancel))
            def progress(received, expected, elapsed):
                nonlocal progressive, first_byte
                check_cancel(cancel)
                if self.request != request:
                    raise Cancelled()
                if self.engine.request and self.engine.snapshot.state == 'FAILED':
                    raise PlaybackError(self.engine.snapshot.reason)
                if not first_byte and received:
                    first_byte = True
                    emit('first-byte' if r.backend == 'fixture' else 'first-progress', received=received,
                         elapsed=elapsed, evidence='fixture-first-chunk' if r.backend == 'fixture' else 'delivered-progress')
                if progressive and progressive.error and (progressive.error == 'invalid-media-response' or
                        any(k == r.key for k, _ in playlist.groups)):
                    raise PlaybackError(progressive.error)
                if received-last_budget[0] >= 4*1024*1024 or last_budget[0] == 0:
                    self.store.ensure_space(4*1024*1024)
                    last_budget[0] = received
                self.status = replace(self.status, received=received, expected=expected or r.size)
                if not playlist.url and not predecessor:
                    self.status = Status('DOWNLOADING', camera.camera_id, requested, reason=self.status.reason,
                        received=received, expected=expected or r.size, key=r.key, track=r.track)
                if received and progressive is None:
                    progressive = ProgressivePreparation(partial, self.settings, cancel, download_done,
                                                         download_failed, produce, emit,
                                                         lambda: any(k == r.key for k, _ in playlist.groups))
                    progressive.start()
            try:
                began = time.monotonic()
                received = backend.download(r, partial, cancel, limit, progress)
                emit('download-complete', received=received or partial.stat().st_size, elapsed=time.monotonic()-began)
                download_done.set()
                if progressive:
                    progressive.join()
                check_cancel(cancel)
                media = probe(partial, self.settings, cancel)
                # Successful probe != finalization. Preserve the observed revision,
                # and limit coverage to bytes actually received in this snapshot.
                partial.replace(source)
                self.store.state(r.key, 'downloaded', media)
                if progressive and progressive.error and any(k == r.key for k, _ in playlist.groups):
                    emit('progressive-failed', reason=progressive.error)
                    raise PlaybackError(progressive.error)
                if progressive and progressive.complete:
                    self.store.state(r.key, 'prepared', media)
                    entry = self.store.entry(r.key)
            except Exception:
                download_failed.set()
                self.store.state(r.key, 'failed')
                raise
            finally:
                download_done.set()
                if progressive:
                    progressive.join()
        if media is None:
            media = probe(source, self.settings, cancel)
        with self.playback_lock:
            self.session_ranges = tuple((a, r.start+media['duration'] if a == r.start else b)
                                        for a, b in self.session_ranges)
        if not r.start <= requested < r.start+media['duration']:
            raise PlaybackError('no-video-in-snapshot')
        self.status = replace(self.status, key=r.key, track=r.track,
            source='cache' if entry.state == 'prepared' else 'camera-snapshot')
        segments, complete = read_segments(directory, r, timing=True,
                                            video_offset=media.get('video_start_offset', 0.))
        if entry.state == 'prepared' and complete:
            publish_ready(segments, True)
            return r.start+media['duration']
        self.store.state(r.key, 'preparing', media)
        if not playlist.url:
            self.status = replace(self.status, state='PREPARING')
        def publish(segments, final):
            check_cancel(cancel)
            publish_ready(segments, final)
        try:
            self.log.event('producer-start', camera_id=camera.camera_id, generation=request[0], session_id=request[0],
                           archive_id=r.key[:12], mode='complete', container=media['container'])
            media = prepare(source, directory, r, media, self.settings, cancel, publish,
                            lambda: self.store.ensure_space(4*1024*1024))
            self.store.state(r.key, 'prepared', media)
            if self.month_request:
                self._catalog(*self.month_request[:3])
        except Exception:
            self.store.state(r.key, 'downloaded', media)
            raise
        return r.start+media['duration']

    def _publish_segments(self, playlist, key, segments, requested, request, final):
        with self.playback_lock:
            self._publish_locked(playlist, key, segments, requested, request, final)

    def _publish_locked(self, playlist, key, segments, requested, request, final):
        if self.request != request:
            raise Cancelled()
        if not segments:
            return
        longest = max(s.duration for s in segments)
        self.log.event('segments-ready', camera_id=self.status.camera_id, generation=request[0],
                       session_id=request[0], archive_id=key[:12], playlist_id=playlist.identifier,
                       count=len(segments), segment_duration=longest, complete=final,
                       prepared_start=segments[0].start, prepared_end=segments[-1].start+segments[-1].duration,
                       first_pts=segments[0].first_pts if segments[0].first_pts is not None else -1.)
        first_publication = not any(k == key for k, _ in playlist.groups)
        if playlist.url and longest > playlist.target_duration:
            self.playlist_changing.set()
            resume = self.view.position if self.engine.request is not None else requested
            try:
                self.engine.stop()
                while not self.engine.idle.wait(.05):
                    check_cancel(self.cancel)
                self.server.clear()
                playlist.rebase(resume, longest)
                self.resume_target, self.open_origin = resume, 'gop-rebase'
                requested = resume
                self.sent_seek = None
                self.status = replace(self.status, state='BUFFERING')
                self.log.event('long-gop-rebase', camera_id=self.status.camera_id, generation=request[0],
                               session_id=request[0], archive_id=key[:12], playlist_id=playlist.identifier,
                               position=resume, target_duration=playlist.target_duration, segment_duration=longest)
            finally:
                self.playlist_changing.clear()
        with self.playback_lock:
            if playlist.groups and all(k != key for k, _ in playlist.groups):
                self.log.event('archive-boundary', camera_id=request[1], session_id=request[0],
                    archive_id=key[:12], previous_archive_id=playlist.groups[-1][0][:12],
                    playlist_id=playlist.identifier, boundary=segments[0].start,
                    boundary_delta=segments[0].start-playlist.end,
                    relative_target=segments[0].start-playlist.start)
            playlist.update(key, segments)
            if first_publication:
                self.log.event('first-segment-published', camera_id=self.status.camera_id, generation=request[0],
                               session_id=request[0], archive_id=key[:12], playlist_id=playlist.identifier,
                               segment_duration=segments[0].duration, target_duration=playlist.target_duration)
            self._maybe_open(playlist, requested, request, final=final)

    def _maybe_open(self, playlist, requested, request, final=False):
        with self.playback_lock:
            self._open_ready(playlist, requested, request, final)

    def _open_ready(self, playlist, requested, request, final):
        if self.request != request:
            raise Cancelled()
        requested = getattr(self, 'resume_target', None) or playlist.requested
        target = self.seek_target
        if target and target[1] == request[1]:
            requested = target[2]
        self.status = replace(self.status, prepared_end=playlist.end)
        # Two viewing seconds to start; comfort prefetch remains independently 120s.
        reserve = max(2., 2*self.controls.rate)
        available = any(s.start <= requested < s.start+s.duration for s in playlist.segments)
        if not available and playlist.segments and getattr(self, '_waiting_target', None) != requested:
            self._waiting_target = requested
            self.log.event('target-wait', camera_id=request[1], session_id=request[0], target=requested,
                           prepared_end=playlist.end, reason='target-not-received', classification='E')
        if self.engine.request is None and playlist.url and available and (
                final or playlist.end-requested >= reserve):
            self.engine.controls = self.controls
            self.log.event('target-available', camera_id=request[1], generation=request[0], position=requested,
                           reserve=playlist.end-requested)
            self.engine.open(playlist.url, playlist.media_offset(requested), origin=getattr(self, 'open_origin', 'initial-open'))
            self.resume_target = None
            self.status = replace(self.status, state='STARTING', position=requested)

    def _session(self, request):
        _, cid, requested = request
        camera = next((c for c in self.cameras if c.camera_id == cid), None)
        if camera is None:
            raise PlaybackError('camera-unavailable')
        cancel = self.cancel = threading.Event()
        backend = [None]
        playlist = Playlist(self.server, requested)
        self.session_ranges = ()
        self.sent_seek = None
        self.resume_target, self.open_origin = None, 'initial-open'
        self.active_playlist, self.active_request = playlist, request
        held = set()
        jobs, backends = [], [backend]
        def launch(entry, predecessor=None):
            holder = backend if not jobs else [None]
            if holder is not backend:
                backends.append(holder)
            held.add(entry.recording.key)
            self.store.pin(entry.recording.key)
            job = PreparationJob(entry, lambda: self._prepare(entry, camera, playlist, cancel, holder,
                requested if not predecessor else entry.recording.start, request, predecessor), predecessor)
            jobs.append(job)
            job.start()
        try:
            entry = self._find(camera, requested, cancel, backend)
            launch(entry)
            logged_at = 0.
            recovery = None
            next_check = 0.
            previous_position = None
            previous_seek = None
            no_successor = False
            while not self.stop_event.wait(.1):
                if self.request != request:
                    break
                native = self.engine.snapshot
                self._maybe_open(playlist, requested, request, final=all(j.done.is_set() for j in jobs))
                position = playlist.absolute_at(native.position) if native.generation else requested
                if native.state == 'FAILED':
                    raise PlaybackError(native.reason)
                for job in jobs:
                    if job.done.is_set() and job.error and not job.reported:
                        job.reported = True
                        reason = job.error.code if isinstance(job.error, PlaybackError) else 'preparation-incomplete'
                        self.log.event('preparation-failed', camera_id=cid, session_id=request[0],
                                       archive_id=job.entry.recording.key[:12], reason=reason,
                                       origin='prefetch' if job.predecessor else 'initial')
                        if not job.predecessor:
                            raise job.error
                        no_successor = True
                        self.status = replace(self.status, reason=reason)
                if native.generation:
                    seek_marker = self.engine.seek_request['id'] if self.engine.seek_request else None
                    if (previous_position is not None and seek_marker == previous_seek and
                            position < previous_position-2 and native.state == 'PLAYING'):
                        self.log.event('native-clock-regression', camera_id=cid, session_id=request[0],
                                       generation=native.generation, position=position,
                                       boundary=previous_position, playlist_id=playlist.identifier)
                    previous_position, previous_seek = position, seek_marker
                    self.status = replace(self.status, state=native.state, position=position, prepared_end=playlist.end)
                    if time.monotonic()-logged_at>5:
                        logged_at=time.monotonic()
                        self.log.event('native-sample',camera_id=cid,generation=native.generation,state=native.state,
                            session_id=request[0], playlist_id=playlist.identifier,
                            position=position,relative_target=native.position,rate=native.rate,
                            paused=self.controls.paused,decoded=native.decoded,displayed=native.displayed,
                            stats_valid=native.stats_valid,audio=native.audio)
                if self.export_request and all(j.done.is_set() for j in jobs):
                    self._export(cancel)
                # At most two transfers/remux jobs. The next native file can
                # download while the first is still arriving; publication stays
                # ordered. Initial reserve remains two viewing seconds.
                tail = jobs[-1]
                tail_entry = self.store.entry(tail.entry.recording.key) if tail.done.is_set() else tail.entry
                boundary = tail_entry.end
                horizon = max(self.settings.reserve_seconds, 180)*self.controls.rate
                if (time.monotonic() >= next_check and not no_successor and len(held) < 4 and
                        sum(not j.done.is_set() for j in jobs) < 2):
                    next_check = time.monotonic()+1.
                    candidates = self.store.entries((cid,), tail.entry.recording.start, tail.entry.recording.start+86400)
                    next_entries = sorted((e for e in candidates if e.recording.key not in held and
                        e.recording.device == entry.recording.device and e.recording.track == entry.recording.track and
                        e.recording.start > tail.entry.recording.start and
                        (boundary is None or abs(e.recording.start-boundary) <= .5)), key=lambda e: e.recording.start)
                    near = (next_entries[0].recording.start if next_entries else boundary)
                    if not next_entries and tail.done.is_set() and boundary is not None and boundary-position <= horizon:
                        try:
                            discovered = self._find(camera, boundary, cancel, backend, exclude=held)
                            if (discovered.recording.track == entry.recording.track and
                                    abs(discovered.recording.start-boundary) <= .5):
                                next_entries = [discovered]
                        except Cancelled:
                            raise
                        except PlaybackError as exc:
                            # Remaining local media stays playable during a camera
                            # outage. Never replace it with an unrelated later time.
                            self.log.event('successor-unavailable', camera_id=cid, session_id=request[0], reason=exc.code)
                        if not next_entries:
                            no_successor = True
                    if next_entries and (near is None or near-max(position, requested) <= horizon):
                        self.log.event('prefetch-start', camera_id=cid, session_id=request[0],
                            archive_id=next_entries[0].recording.key[:12], boundary=next_entries[0].recording.start,
                            reserve=horizon)
                        launch(next_entries[0], tail)
                if (no_successor or len(held) >= 4) and all(j.done.is_set() for j in jobs) and not playlist.closed:
                    playlist.finish()
                if native.state == 'ENDED':
                    # Temporary native EOF is not end of archive coverage. Wait
                    # until new local segments exist, then reopen once for that
                    # (generation, available-end) pair. No repeated seek storm.
                    marker = (round(position, 1), playlist.end)
                    if playlist.end-position > .5 and recovery != marker:
                        recovery = marker
                        with self.playback_lock:
                            self.engine.open(playlist.url, playlist.media_offset(position), origin='ended-recovery')
                            self.sent_seek = None
                        continue
                    if not playlist.closed or any(not j.done.is_set() for j in jobs):
                        self.status = replace(self.status, state='BUFFERING', position=position,
                                              reason='next-archive-pending')
                        continue
                    # A gap or quota boundary must not masquerade as a still live image.
                    self.status = replace(self.status, state='GAP', position=playlist.end,
                                          reason='session-limit' if len(held) >= 4 else 'end-of-coverage')
                    if len(held) >= 4:
                        # Start a new bounded generation at the same archive time.
                        # Existing process is reaped in finally before replacement.
                        self.select(cid, playlist.end)
                    return
        finally:
            cancel.set()
            # Reap all publishers before retiring their resources or pins.
            for job in jobs:
                job.join()
            self.playlist_changing.set()
            self.engine.stop()
            while not self.engine.idle.wait(.05):
                pass  # Coordinator only; native supervisor has bounded teardown.
            self.server.clear()
            self.active_playlist = self.active_request = None
            self.session_ranges = ()
            self.sent_seek = None
            self.playlist_changing.clear()
            if self.server.drained.wait(4):
                for key in held:
                    self.store.unpin(key)
            for holder in backends:
                if holder[0]:
                    holder[0].close()

    def _export(self, cancel):
        key, destination, start, end = self.export_request
        self.export_status = 'working'
        temporary = destination.with_name(destination.name+'.partial')
        sidecar = destination.with_name(destination.name+'.json')
        owns_temporary = False
        try:
            if destination.exists() or sidecar.exists() or temporary.exists():
                raise PlaybackError('export-exists')
            entry = self.store.entry(key)
            source = self.store.path(key)/'original.bin'
            if not source.is_file():
                raise PlaybackError('export-not-ready')
            metadata = {'camera_id': entry.recording.camera_id, 'backend': entry.recording.backend,
                'raw_start': entry.recording.raw_start, 'raw_end': entry.recording.raw_end,
                'archive_start_utc': entry.recording.start, 'display_zone': self.settings.display_zone,
                'finalization': 'unconfirmed', 'requested_interval': [start, end],
                'mode': 'original' if start is None else 'keyframe-remux'}
            if start is None:
                digest = hashlib.sha256()
                with source.open('rb') as inp, temporary.open('xb') as out:
                    owns_temporary = True
                    while True:
                        check_cancel(cancel)
                        check_cancel(self.export_cancel)
                        data = inp.read(1024*1024)
                        if not data:
                            break
                        out.write(data)
                        digest.update(data)
                metadata['sha256'] = digest.hexdigest()
                metadata['effective_interval'] = [entry.recording.start, entry.end]
            else:
                if not entry.media or not entry.recording.start <= start < end <= entry.end:
                    raise PlaybackError('export-interval')
                offset = start-entry.recording.start
                # Reserve our temporary name before handing it to FFmpeg.
                with temporary.open('xb'):
                    owns_temporary = True
                run([self.settings.ffmpeg, '-nostdin', '-hide_banner', '-v', 'warning',
                    '-copyts', '-start_at_zero', '-protocol_whitelist', 'file,pipe',
                    '-ss', str(offset), '-i', str(source), '-to', str(end-entry.recording.start),
                    '-map', '0:v:0', '-map', '0:a:0?', '-c:v', 'copy', '-avoid_negative_ts', 'disabled',
                    '-c:a', 'copy' if entry.media['audio'] == 'aac' else 'pcm_s16le',
                    '-f', 'matroska', '-y', str(temporary)], self.export_cancel, timeout=1800,
                    tick=lambda: check_cancel(cancel))
                inspected = probe(temporary, self.settings, self.export_cancel)
                first_pts,last_pts=packet_bounds(temporary,self.settings,self.export_cancel)
                if first_pts < -.01 or last_pts > entry.media['duration']+2 or last_pts <= offset:
                    raise PlaybackError('export-timestamps')
                digest = hashlib.sha256()
                with temporary.open('rb') as handle:
                    for chunk in iter(lambda: handle.read(1024*1024), b''):
                        check_cancel(self.export_cancel)
                        digest.update(chunk)
                metadata['sha256'] = digest.hexdigest()
                metadata['duration_seconds'] = last_pts-first_pts
                metadata['effective_interval'] = [entry.recording.start+first_pts,entry.recording.start+last_pts]
                metadata['bounds_basis'] = 'Preserved video packet PTS, copyts/start_at_zero, correlated with indexed archive start.'
                metadata['warning'] = 'Keyframe cut; actual packet bounds can differ from requested bounds. OSD correlation and frame-exactness not qualified.'
            metadata['size_bytes'] = temporary.stat().st_size
            check_cancel(cancel)
            check_cancel(self.export_cancel)
            # Windows rename refuses an existing target; POSIX link gives the
            # same no-overwrite publication semantics on the destination volume.
            if os.name == 'nt':
                os.rename(temporary, destination)
            else:
                os.link(temporary, destination)
                temporary.unlink()
            with sidecar.open('x', encoding='utf-8') as out:
                json.dump(metadata, out, ensure_ascii=False, indent=2)
            self.export_status = 'complete'
        except Cancelled:
            self.export_status = 'cancelled'
        except PlaybackError as exc:
            self.export_status = exc.code
        except Exception:
            self.export_status = 'export-failed'
        finally:
            # Only this task's temporary file, never the user's final export.
            if owns_temporary and temporary.exists():
                temporary.unlink()
            self.export_request = None

    def _run(self):
        try:
            if self.fixture_directory is not None:
                from .fixture import load_fixture, FixtureBackend
                self.settings,self.cameras,cipher,self.fixture_data=load_fixture(self.fixture_directory)
                self.connect_backend=lambda camera,cancel:FixtureBackend(camera,self.fixture_directory,self.fixture_data)
            else:
                self.settings = load_settings(self.root)
                self.cameras, cipher = load_cameras(self.settings, self.root)
            self.store = Store(self.settings, cipher)
            self.log = Diagnostics(self.store.root)
            sources = sorted((Path(__file__).parent).glob('*.py'))
            digest = hashlib.sha256(b''.join(p.name.encode()+p.read_bytes() for p in sources)).hexdigest()
            self.log.event('runtime-build', build='0.2.11-dev-boundaries-1', source_digest=digest,
                           python=__import__('sys').version.split()[0])
            self.engine = Engine(self.hwnd, self._native_event)
            self.server = SessionServer(self._native_event)
            self.index_thread = threading.Thread(target=self._index, name='Archive metadata', daemon=True)
            self.index_thread.start()
            self.status = Status('IDLE')
            self.ready.set()
            self.seek_thread = threading.Thread(target=self._seek_loop, name='Archive seek mailbox', daemon=True)
            self.seek_thread.start()
            previous = None
            while not self.stop_event.wait(.1):
                if self.settings_request is not None:
                    settings_request = self.settings_request
                    try:
                        self._apply_configuration(settings_request)
                    except Cancelled:
                        break
                    except Exception:
                        self.configuration_status = 'configuration-apply-failed'
                        self.settings_request = None
                request = self.request
                if self.export_request and (request is None or request == previous):
                    key = self.export_request[0]
                    self.store.pin(key)
                    try:
                        self._export(self.stop_event)
                    finally:
                        self.store.unpin(key)
                if request is None or request == previous:
                    continue
                previous = request
                try:
                    self._session(request)
                except Cancelled:
                    pass
                except PlaybackError as exc:
                    self.log.event('error',camera_id=request[1],session_id=request[0],reason=exc.code,
                                   archive_id=self.status.key[:12], position=self.status.position)
                    if self.request == request:
                        state = 'CONFIGURATION' if exc.code == 'timezone-required' else 'GAP' if exc.code.startswith('no-video') else 'ERROR'
                        self.status = replace(self.status, state=state, reason=exc.code)
                except Exception:
                    self.log.event('error',camera_id=request[1],session_id=request[0],reason='playback-unavailable')
                    if self.request == request:
                        self.status = replace(self.status, state='ERROR', reason='playback-unavailable')
        except PlaybackError as exc:
            self.status = Status('ERROR', reason=exc.code)
            self.ready.set()
        except Exception:
            self.status = Status('ERROR', reason='initialization-failed')
            self.ready.set()
        finally:
            self.stop_event.set()
            self.index_cancel.set()
            if self.seek_thread:
                self.seek_thread.join(timeout=2)
            if self.engine:
                self.engine.close()
                self.engine.closed.wait(12)
            if self.server:
                self.server.close()
            if self.index_thread:
                self.index_thread.join(timeout=8)
            if self.store:
                # Camera DNS/HTTP live in killable owned processes. Keep this
                # guard for a metadata worker stuck in local filesystem I/O.
                if not self.index_thread or not self.index_thread.is_alive():
                    self.store.close()
            if self.log and (not self.index_thread or not self.index_thread.is_alive()):
                self.log.close()
            self.closed.set()
