"""Independent, source-based interval exports. One bounded job per application."""
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import threading
import time
import uuid
from zoneinfo import ZoneInfo

from .cache import GIB, linked
from .model import PlaybackError, Cancelled, check_cancel, day_bounds
from .media import probe, packet_bounds
from .processes import run


@dataclass(frozen=True)
class ExportRequest:
    camera_id: int
    track: str
    start_utc: float
    end_utc: float
    destination: Path
    mode: str = 'precise'

    def __post_init__(self):
        if (not self.track or not math.isfinite(self.start_utc+self.end_utc) or
                not 0 < self.end_utc-self.start_utc <= 366*86400 or self.mode != 'precise'):
            raise PlaybackError('export-interval')


@dataclass(frozen=True)
class Slice:
    start: float
    end: float
    entry: object = None


def plan_interval(entries, request):
    """Half-open UTC coverage, deterministic latest-start revision wins overlaps.

    No epsilon fills a gap. A network failure must be dealt with by the caller
    before treating an uncovered interval as a confirmed recording gap.
    """
    eligible = {e.recording.key:e for e in entries if e.recording.camera_id==request.camera_id
        and e.recording.track==request.track and e.end is not None
        and e.recording.start < request.end_utc and e.end > request.start_utc}
    points = sorted({request.start_utc, request.end_utc} | {
        max(request.start_utc, min(request.end_utc, t)) for e in eligible.values()
        for t in (e.recording.start, e.end)})
    result = []
    for a,b in zip(points, points[1:]):
        candidates = [e for e in eligible.values() if e.recording.start <= a and e.end >= b]
        entry = max(candidates, key=lambda e:(e.recording.start,e.recording.observed,e.recording.key)) if candidates else None
        if result and result[-1].entry == entry:
            result[-1] = replace(result[-1], end=b)
        else:
            result.append(Slice(a,b,entry))
    return tuple(result)


def destination_check(path, cache_root, required, floor):
    path = Path(path)
    if any(linked(p) for p in (path, *path.parents)):
        raise PlaybackError('export-path')
    resolved = path.resolve()
    if cache_root == resolved or cache_root in resolved.parents or not path.parent.is_dir():
        raise PlaybackError('export-path')
    if shutil.disk_usage(path.parent).free < required+floor:
        raise PlaybackError('export-disk-full')


def frame_tolerance(media):
    try:
        rate = float(Fraction(media.get('frame_rate','0/0')))
        return max(.2,min(10.,1/rate+.02)) if rate>0 else .2
    except (ValueError,ZeroDivisionError):
        return .2


class Exporter:
    def __init__(self, controller):
        self.c = controller
        self.thread = None
        self.choice = threading.Event()
        self.policy = None
        self.prompt = None

    def submit(self, request):
        if self.c.export_request is not None:
            raise PlaybackError('export-busy')
        self.c.export_cancel = threading.Event()
        self.c.export_request = request
        self.c.export_status = 'Discovering all intersecting archives…'
        self.choice.clear()
        self.policy = self.prompt = None
        self.thread = threading.Thread(target=self._run, args=(request,), name='Archive export', daemon=True)
        self.thread.start()

    def decide(self, policy):
        self.policy = policy
        self.prompt = None
        self.choice.set()

    def close(self):
        self.c.export_cancel.set()
        self.choice.set()
        if self.thread:
            self.thread.join()

    def _check(self):
        check_cancel(self.c.export_cancel)
        check_cancel(self.c.stop_event)

    def _priority(self):
        # Network/encoding starts yield while useful playback is being prepared.
        while self.c.request and self.c.view.state in ('LOADING','DOWNLOADING','PREPARING','BUFFERING','STARTING','SEEKING'):
            self._check()
            self.c.export_status = 'Waiting for playback preparation…'
            self.c.export_cancel.wait(.2)

    def _source(self, entry, camera, backend_holder, owner):
        store, cancel = self.c.store, self.c.export_cancel
        key = entry.recording.key
        directory = store.path(key)
        source = store.file(key,'original.bin')
        with store.writer(key, cancel):
            current = store.entry(key)
            if source.is_file():
                if current.media is None:
                    store.state(key,'downloaded',probe(source,self.c.settings,cancel))
                    current=store.entry(key)
                store.touch(key)
                return current, source
            self._priority()
            limit = int(self.c.settings.max_archive_gib*GIB)
            size = entry.recording.size or limit
            if size > limit:
                raise PlaybackError('archive-too-large')
            with store.reserve(owner+':raw', key, size+4*1024**2) as allocation:
                directory.mkdir(exist_ok=True)
                if backend_holder[0] is None:
                    backend_holder[0] = self.c.connect_backend(camera, cancel)
                backend = backend_holder[0]
                if backend.device != entry.recording.device:
                    raise PlaybackError('device-changed')
                partial = store.file(key,'original.part')
                def progress(received, expected, elapsed):
                    self._check()
                    allocation.check()
                    self.c.export_status = f'Receiving source: {received/1048576:.1f} MiB'
                try:
                    store.state(key, 'partial')
                    backend.download(entry.recording, partial, cancel, limit, progress)
                    media = probe(partial, self.c.settings, cancel)
                    partial.replace(source)
                    store.state(key, 'downloaded', media)
                except Exception:
                    store.state(key, 'failed')
                    raise
            return store.entry(key), source

    def _discover(self, request, camera, backend):
        settings, store = self.c.settings, self.c.store
        zone = ZoneInfo(settings.display_zone)
        first = datetime.fromtimestamp(request.start_utc,zone).date()
        last = datetime.fromtimestamp(request.end_utc-.000001,zone).date()
        complete = True
        day = first
        while day <= last:
            self._check()
            self._priority()
            a,b = day_bounds(day,zone)
            try:
                if backend[0] is None:
                    backend[0] = self.c.connect_backend(camera,self.c.export_cancel)
                result = backend[0].list_recordings(a,b,self.c.export_cancel)
                store.record_search(camera.camera_id,a,b,backend[0].device,result)
                complete = complete and result.complete
            except Cancelled:
                raise
            except PlaybackError:
                complete = False  # Cached complete coverage may still be exported.
            day += timedelta(days=1)
        entries = store.entries((camera.camera_id,),request.start_utc,request.end_utc)
        candidates = sorted((e for e in entries if e.recording.track==request.track),key=lambda e:e.recording.start)
        candidates = [e for i,e in enumerate(candidates) if e.end is not None or
                      (candidates[i+1].recording.start if i+1<len(candidates) else request.end_utc)>request.start_utc]
        inspected = []
        for index, entry in enumerate(candidates):
            self._check()
            self.c.export_status = f'Checking source {index+1}/{len(candidates)}…'
            owner = 'export-inspect:'+entry.recording.key
            with store.lease(entry.recording.key,owner):
                current,_ = self._source(entry,camera,backend,owner)
                inspected.append(current)
        plan = plan_interval(inspected,request)
        if any(s.entry is None for s in plan) and not complete:
            raise PlaybackError('export-coverage-unknown')
        return plan

    def _command(self, part, source, media, target, width, height):
        settings = self.c.settings
        duration = part.end-part.start
        command = [settings.ffmpeg,'-nostdin','-hide_banner','-v','warning','-y',
                   '-filter_threads','1','-threads','2']
        if source:
            command += ['-protocol_whitelist','file,pipe','-ss',str(part.start-part.entry.recording.start),'-i',str(source)]
        else:
            command += ['-f','lavfi','-i',f'color=c=black:s={width}x{height}:r=25:d={duration}']
        silent = not source or not media.get('audio')
        if silent:
            command += ['-f','lavfi','-i','anullsrc=r=48000:cl=stereo']
        video = f'scale=trunc(iw*sar/2)*2:ih,setsar=1,scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1'
        if not source:
            video += ",drawtext=text='No recording':fontcolor=white:fontsize=40:x=(w-tw)/2:y=(h-th)/2"
        return command+['-map','0:v:0','-map','1:a:0' if silent else '0:a:0',
            '-t',str(duration),'-vf',video,'-af',f'aresample=async=1:first_pts=0,apad,atrim=duration={duration}',
            '-c:v','libx264','-preset','fast','-crf','18','-maxrate','8M','-bufsize','16M',
            '-threads','2','-pix_fmt','yuv420p','-vsync','vfr','-enc_time_base','1:90000',
            '-c:a','pcm_s16le','-ar','48000','-ac','2','-f','matroska',str(target)]

    def _run(self, request):
        c, store, settings = self.c, self.c.store, self.c.settings
        backend = [None]
        destination = Path(request.destination)
        temporary = destination.with_name(destination.name+'.'+uuid.uuid4().hex+'.partial')
        sidecar = destination.with_name(destination.name+'.json')
        sidepart = temporary.with_suffix('.json.partial')
        owns = False
        try:
            destination_check(destination,store.root,0,int(settings.free_gib*GIB))
            if destination.exists() or sidecar.exists():
                raise PlaybackError('export-exists')
            camera = next((v for v in c.cameras if v.camera_id==request.camera_id),None)
            if not camera:
                raise PlaybackError('camera-unavailable')
            plan = self._discover(request,camera,backend)
            gaps = [[p.start,p.end] for p in plan if p.entry is None]
            if gaps:
                self.prompt = dict(gaps=gaps, request=request)
                c.export_status = 'Choose how to handle missing recordings.'
                while not self.choice.wait(.1):
                    self._check()
                self._check()
                if self.policy not in ('neutral','available'):
                    raise Cancelled()
            else:
                self.policy = 'available'
            parts = [p for p in plan if p.entry or self.policy=='neutral']
            if not parts:
                raise PlaybackError('no-video-in-selection')
            duration = sum(p.end-p.start for p in parts)
            # H264 maxrate + stereo PCM + mux/buffer margin. Both intermediates
            # and final destination are budgeted before the first encoder starts.
            budget = int(duration*1_350_000)+32*1024**2
            same_volume = destination.parent.stat().st_dev == store.root.stat().st_dev
            destination_check(destination,store.root,budget*(2 if same_volume else 1),int(settings.free_gib*GIB))
            media = [p.entry.media for p in parts if p.entry]
            width = max((m.get('display_width',m['width']) for m in media),default=1280)
            height = max((m['height'] for m in media),default=720)
            width += width%2
            height += height%2
            records = []
            effective = []
            with store.workspace('export-work') as (key, directory), store.reserve('export-work',key,budget) as allocation:
                files = []
                for i,part in enumerate(parts):
                    self._check()
                    self._priority()
                    target = directory/f'export-{i:06d}.mkv'
                    c.export_status = f'Encoding part {i+1}/{len(parts)}…'
                    def encode(source, info):
                        run(self._command(part,source,info,target,width,height),c.export_cancel,
                            timeout=max(1800,(part.end-part.start)*20),tick=lambda:(self._check(),allocation.check()), background=True)
                    if part.entry:
                        owner = 'export-part:'+str(i)
                        with store.lease(part.entry.recording.key,owner):
                            current,source = self._source(part.entry,camera,backend,owner)
                            encode(source,current.media)
                            store.touch(part.entry.recording.key)
                        records.append(dict(archive_id=part.entry.recording.key[:12],
                            interval=[part.start,part.end],source_video=current.media['video'],
                            source_audio=current.media['audio'],width=current.media['width'],height=current.media['height']))
                    else:
                        encode(None,{})
                    first,last = packet_bounds(target,settings,c.export_cancel)
                    tolerance=frame_tolerance(part.entry.media) if part.entry else .2
                    if abs(first)>tolerance or abs(last-first-(part.end-part.start))>tolerance:
                        raise PlaybackError('export-timestamps')
                    effective.append([part.start+first,part.start+last])
                    files.append((target,part.end-part.start))
                    allocation.check()
                manifest = directory/'export-concat.txt'
                manifest.write_text('ffconcat version 1.0\n'+''.join(
                    f"file '{p.name}'\nduration {length:.9f}\n" for p,length in files),encoding='utf-8')
                with temporary.open('xb'):
                    owns = True
                c.export_status = 'Assembling and checking audio/video…'
                def output_budget():
                    self._check()
                    allocation.check()
                    destination_check(destination,store.root,0,int(settings.free_gib*GIB))
                    if temporary.stat().st_size > budget:
                        raise PlaybackError('export-disk-full')
                run([settings.ffmpeg,'-nostdin','-hide_banner','-v','warning','-y','-f','concat','-safe','1',
                     '-protocol_whitelist','file,pipe','-i',str(manifest),'-map','0:v:0','-map','0:a:0',
                     '-c:v','libx264','-preset','fast','-crf','18','-maxrate','8M','-bufsize','16M',
                     '-vsync','vfr','-pix_fmt','yuv420p','-enc_time_base','1:90000',
                     '-c:a','aac','-b:a','128k','-threads','2','-movflags','+faststart',
                     '-f','mp4',str(temporary)],c.export_cancel,timeout=max(1800,duration*2),tick=output_budget,background=True)
                checked = probe(temporary,settings,c.export_cancel)
                first,last = packet_bounds(temporary,settings,c.export_cancel)
                audio_first,audio_last = packet_bounds(temporary,settings,c.export_cancel,stream='a:0')
                tolerance=max((frame_tolerance(m) for m in media),default=.2)
                if abs(first)>tolerance or abs(last-first-duration)>tolerance or checked['audio']!='aac':
                    raise PlaybackError('export-timestamps')
                if abs(audio_first-first)>tolerance or abs(audio_last-last)>tolerance:
                    raise PlaybackError('export-timestamps')
                digest = hashlib.sha256()
                with temporary.open('rb') as source:
                    for block in iter(lambda:source.read(1024**2),b''):
                        self._check()
                        digest.update(block)
                metadata = dict(camera_id=request.camera_id,track=request.track,mode=request.mode,
                    requested_interval=[request.start_utc,request.end_utc],
                    effective_intervals=effective,video_packet_bounds=[first,last],
                    audio_packet_bounds=[audio_first,audio_last],
                    measured_duration=last-first,display_zone=settings.display_zone,gaps=gaps,gap_policy=self.policy,
                    source_refs=records,sha256=digest.hexdigest(),size_bytes=temporary.stat().st_size,
                    silent_audio_for_sources_without_audio=True, validation_tolerance_seconds=tolerance,
                    overlap_rule='Latest archive start, then observed revision, then archive hash; half-open UTC.',
                    conversion='Useful intervals decoded to H264 CRF18/PCM intermediates, then assembly re-encoded to uniform H264 CRF18 capped 8Mbps/AAC 48k stereo. Aspect-preserving pad; no motion interpolation.',
                    bounds_basis='Indexed UTC plus source-relative accurate seek; output packet durations checked. Boundary precision limited to source frames; verify OSD and audio on real cameras.')
                sidepart.write_text(json.dumps(metadata,indent=2)+'\n',encoding='utf-8')
                self._check()
                if sidecar.exists() or destination.exists():
                    raise PlaybackError('export-exists')
                # No-overwrite publication on both supported platforms.
                if os.name=='nt':
                    os.rename(temporary,destination)
                    os.rename(sidepart,sidecar)
                else:
                    os.link(temporary,destination)
                    os.link(sidepart,sidecar)
                    temporary.unlink()
                    sidepart.unlink()
            c.export_status = 'complete'
        except Cancelled:
            c.export_status = 'cancelled'
        except PlaybackError as exc:
            c.export_status = exc.code
        except Exception:
            c.export_status = 'export-failed'
        finally:
            if backend[0]:
                try:
                    backend[0].close()
                except Exception:
                    pass
            if owns:
                for path in (temporary,sidepart) if not destination.exists() else (temporary,):
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
            self.prompt = None
            c.export_request = None
            store.cache_wake.set()
