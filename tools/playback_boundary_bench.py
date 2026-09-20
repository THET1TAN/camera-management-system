"""User-run two-native-archive bench. No player is launched by this tool."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from playback.config import load_settings, load_cameras
from playback.fixture import load_fixture, FixtureBackend
from playback.media import probe, prepare, read_segments, packet_bounds
from playback.model import PlaybackError
from playback.server import Playlist, SessionServer
from playback.store import Store


def copy_pair(directory, camera_id):
    """Copy only completed originals; the real cache is read-only throughout."""
    from cryptography.fernet import Fernet
    settings = load_settings()
    _, cipher = load_cameras(settings)
    db = sqlite3.connect((settings.cache_path/'index.sqlite3').as_uri()+'?mode=ro', uri=True)
    try:
        rows = db.execute("SELECT key,sealed,media FROM recordings WHERE camera=? AND state IN ('downloaded','prepared') ORDER BY start",
                          (camera_id,)).fetchall()
    finally:
        db.close()
    records = [(key, json.loads(cipher.decrypt(sealed)), json.loads(media)) for key, sealed, media in rows if media]
    pair = next(((a,b) for a,b in zip(records, records[1:]) if
        a[1]['device'] == b[1]['device'] and a[1]['track'] == b[1]['track'] and
        abs(b[1]['start']-(a[1]['start']+a[2]['duration'])) <= .5 and
        all((settings.cache_path/v[0]/'original.bin').is_file() for v in (a,b))), None)
    if not pair:
        raise PlaybackError('two-cached-consecutive-archives-required')
    directory.mkdir(parents=True, exist_ok=False)
    output = []
    for label, (key, record, media) in zip(('A','B'), pair):
        filename = f'archive-{label}.bin'
        shutil.copyfile(settings.cache_path/key/'original.bin', directory/filename)
        output.append(dict(camera_id=camera_id, file=filename, start=record['start'], end=record['start']+media['duration']))
    (directory/'fixture.key').write_bytes(Fernet.generate_key())
    (directory/'fixture.json').write_text(json.dumps(dict(zone=settings.display_zone, chunk_delay=.3,
        recordings=output), indent=2), encoding='utf-8')
    print('Copied two completed native archives. Original cache and settings were not changed.')


def warm(directory, camera_id):
    settings, cameras, cipher, data = load_fixture(directory)
    camera = next(c for c in cameras if c.camera_id == camera_id)
    backend = FixtureBackend(camera, directory, data)
    cancel = threading.Event()
    rows = [r for r in data['recordings'] if r['camera_id'] == camera_id]
    result = backend.list_recordings(min(r['start'] for r in rows), max(r['end'] for r in rows), cancel)
    if len(result.records) < 2:
        raise PlaybackError('two-native-archives-required')
    store = Store(settings, cipher)
    report = []
    try:
        store.record_search(camera_id, min(r['start'] for r in rows), max(r['end'] for r in rows), backend.device, result)
        for r in result.records:
            dest = store.path(r.key)
            dest.mkdir(exist_ok=True)
            original = dest/'original.bin'
            if not original.exists():
                shutil.copyfile(directory/r.locator, original)
            info = probe(original, settings, cancel)
            try:
                bounds = packet_bounds(original, settings, cancel)
                bounds_reason = ''
            except PlaybackError as exc:
                bounds, bounds_reason = None, exc.code
            # Each run gets a new bench directory; completed derivatives survive
            # repeated --warm. Never clear original evidence to force a cold run.
            segments, complete = read_segments(dest, r, timing=True, video_offset=info.get('video_start_offset', 0.))
            if not complete:
                info = prepare(original, dest, r, info, settings, cancel, lambda *_:None, lambda:None)
                segments, complete = read_segments(dest, r, timing=True, video_offset=info.get('video_start_offset', 0.))
            store.state(r.key, 'prepared', info)
            report.append(dict(archive_id=r.key[:12], start=r.start, source_duration=info['duration'],
                container=info['container'], video=info['video'], audio=info['audio'],
                source_start=info['source_start'], source_video_packet_bounds=bounds,
                source_packet_audit_error=bounds_reason,
                segment_count=len(segments), first_pts=segments[0].first_pts,
                last_segment_pts=segments[-1].first_pts,
                prepared_start=segments[0].start, prepared_end=segments[-1].start+segments[-1].duration))
        output = directory/f'boundary-audit-{time.time_ns()}.json'
        output.write_text(json.dumps(report, indent=2), encoding='utf-8')
        print('Prepared each archive separately. Inspect boundary-audit JSON; decoding/OSD/audio still require your observation.')
    finally:
        store.close()


def serve(directory, camera_id, legacy, before):
    settings, _, cipher, data = load_fixture(directory)
    store = Store(settings, cipher)
    server = SessionServer()
    try:
        rows = [r for r in data['recordings'] if r['camera_id'] == camera_id]
        entries = store.entries((camera_id,), min(r['start'] for r in rows), max(r['end'] for r in rows))
        if len(entries) < 2 or any(e.state != 'prepared' for e in entries):
            raise PlaybackError('run-warm-first')
        groups = [(e.recording.key, read_segments(store.path(e.recording.key), e.recording, timing=True,
                   video_offset=e.media.get('video_start_offset', 0.))[0]) for e in entries]
        boundary = entries[1].recording.start
        playlist = Playlist(server, boundary-before)
        # Both native files are already complete: know maximum GOP before URL
        # publication, so this bench isolates joins from generation rebasing.
        import math
        playlist.target_duration = math.ceil(max(s.duration for _, group in groups for s in group))
        for key, segments in groups:
            playlist.update(key, tuple(replace(s, first_pts=None) for s in segments) if legacy else segments)
        playlist.finish()
        print('Mode:', 'legacy transport clocks' if legacy else 'continuous transport clocks')
        print('Boundary media seconds:', boundary-playlist.start)
        print('Start media seconds:', playlist.media_offset(boundary-before))
        print('Open this URL yourself in VLC at 1x; this tool never opens a player:')
        print(playlist.url, flush=True)
        print('Keep this terminal running; Ctrl+C stops only this bench server.', flush=True)
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True)
    parser.add_argument('--camera', required=True, type=int)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--from-cache', action='store_true')
    action.add_argument('--warm', action='store_true')
    action.add_argument('--serve', action='store_true')
    parser.add_argument('--legacy-clocks', action='store_true')
    parser.add_argument('--before', type=float, default=20.)
    args = parser.parse_args()
    directory = Path(args.directory).resolve()
    if not 1 <= args.before <= 120:
        parser.error('--before must be between 1 and 120 seconds')
    if args.from_cache:
        copy_pair(directory, args.camera)
    elif args.warm:
        warm(directory, args.camera)
    else:
        serve(directory, args.camera, args.legacy_clocks, args.before)


if __name__ == '__main__':
    main()
