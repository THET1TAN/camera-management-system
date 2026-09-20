"""Explicit diagnostics. Environment mode uses only the standard library."""
import argparse
from datetime import date
import importlib.util
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import struct
import sys

ROOT = Path(__file__).resolve().parent.parent
DEPENDENCIES = {'cryptography': 'cryptography', 'requests': 'requests', 'vlc': 'python-vlc',
                'onvif': 'onvif-zeep', 'zeep': 'zeep', 'lxml': 'lxml', 'tzdata': 'tzdata',
                'tkinter': None, 'zoneinfo': None}


def environment_snapshot():
    dependencies = {}
    for module, distribution in DEPENDENCIES.items():
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            found = False
        try:
            version = importlib.metadata.version(distribution) if distribution else 'stdlib'
        except importlib.metadata.PackageNotFoundError:
            version = None
        dependencies[module] = {'available_on_path': found, 'distribution_version': version}
    return {'mode': 'environment', 'executable': sys.executable, 'python_version': sys.version.split()[0],
        'architecture_bits': struct.calcsize('P')*8, 'prefix': sys.prefix, 'base_prefix': sys.base_prefix,
        'virtual_environment': sys.prefix != sys.base_prefix,
        'python_on_PATH': shutil.which('python'),
        'launcher_interpreter': os.environ.get('CAMERA_PYTHON') or str(Path(os.environ.get('LOCALAPPDATA', ''))/
            'Programs/Python/Python39/python.exe'),
        'PYTHONPATH_present': bool(os.environ.get('PYTHONPATH')),
        'temporary_test_dependencies_on_path': any('.release-work' in str(p) and 'test-deps' in str(p) for p in sys.path),
        'dependencies': dependencies,
        'ffmpeg_on_PATH': shutil.which('ffmpeg'), 'ffprobe_on_PATH': shutil.which('ffprobe'),
        'note': 'Presence/version only; no application, camera, FFmpeg, VLC or test has been started.'}


def camera_diagnostic(args):
    # This subcommand is the explicit consent to read ONE camera's metadata.
    # No media is downloaded, no player is created, no setting/index is written.
    sys.path.insert(0, str(ROOT))
    from dataclasses import replace
    import threading
    from playback.config import load_cameras, load_settings
    from playback.model import PlaybackError, day_bounds, iso_utc
    from playback.remote import RemoteBackend
    from playback.diagnostics import safe_fields
    settings = load_settings(ROOT)
    cameras, _ = load_cameras(settings, ROOT)
    camera = next((c for c in cameras if c.camera_id == args.camera), None)
    if camera is None:
        emit({'event': 'error', 'reason': 'camera-id-not-found', 'camera_id': args.camera})
        return 1
    start, end = day_bounds(args.date, settings.display_zone)
    cancel = threading.Event()
    failed = False

    def search(label, selected):
        nonlocal failed
        backend = None
        discovered = ()
        emit({'event': 'begin', 'comparison': label, 'camera_id': selected.camera_id,
              'backend': selected.backend, 'track': safe_fields({'requested_track': selected.track})['requested_track'] if selected.track else 'all',
              'date': args.date.isoformat(), 'display_zone': settings.display_zone,
              'camera_zone_configured': bool(selected.zone)})
        try:
            backend = RemoteBackend(selected, cancel, diagnostic=lambda d: emit(dict(event='protocol', **d)))
            discovered = backend.tracks
            emit({'event': 'connected', 'camera_id': selected.camera_id, 'backend': backend.name,
                  'tracks': backend.track_info, 'previous_attempts': backend.attempts,
                  'worker_interpreter_matches_parent': backend.runtime['executable'] == sys.executable})
            result = backend.list_recordings(start, end, cancel)
            emit({'event': 'search-result', 'comparison': label, 'camera_id': selected.camera_id,
                'count': len(result.records), 'complete': result.complete, 'reason': result.reason,
                'failures': result.failures,
                'sample': [{'camera_id': r.camera_id, 'track': safe_fields({'returned_track': r.track})['returned_track'], 'start_utc': iso_utc(r.start),
                            'end_utc': iso_utc(r.end) if r.end else None} for r in result.records[:8]]})
            if result.failures:
                failed = True
        except PlaybackError as exc:
            failed = True
            emit({'event': 'error', 'comparison': label, 'camera_id': selected.camera_id,
                  'reason': exc.code, 'details': exc.details})
        finally:
            if backend:
                backend.close()  # Reap this camera owner before any next comparison.
        return discovered

    try:
        if args.compare_tracks:
            tracks = search('A-primary', replace(camera, backend='isapi', track=args.track))
            others = [track for track in tracks if track != args.track]
            for track in others[:args.max_tracks]:
                search('B-track-'+track, replace(camera, backend='isapi', track=track))
            if len(others) > args.max_tracks:
                emit({'event': 'limit', 'reason': 'comparison-track-limit', 'remaining': len(others)-args.max_tracks})
                failed = True
            # Explicit automatic mode uses the configured selection by default;
            # here an empty track deliberately compares discovery of all tracks.
            search('C-auto', replace(camera, backend='auto', track=''))
        else:
            search('single', replace(camera, backend=args.backend or camera.backend,
                                    track=args.track if args.track is not None else camera.track))
    except KeyboardInterrupt:
        cancel.set()
        emit({'event': 'cancelled'})
        return 130
    return 1 if failed else 0


def emit(value):
    print(json.dumps(value, ensure_ascii=True), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('environment', help='Inspect only this Python installation; no third-party imports')
    camera = commands.add_parser('camera', help='Explicit metadata-only requests for one camera/day')
    camera.add_argument('--camera', type=int, required=True, help='Actual database ID, not address suffix')
    camera.add_argument('--date', type=date.fromisoformat, required=True)
    camera.add_argument('--backend', choices=('auto', 'isapi', 'videolink'))
    camera.add_argument('--track')
    camera.add_argument('--compare-tracks', action='store_true', help='ISAPI primary, others separately, then auto')
    camera.add_argument('--max-tracks', type=int, default=8, help='Maximum secondary tracks, 1..128')
    args = parser.parse_args()
    if args.command == 'environment':
        print(json.dumps(environment_snapshot(), ensure_ascii=True, indent=2))
        return 0
    if args.compare_tracks and not args.track:
        parser.error('--compare-tracks requires an explicit --track; no universal track is assumed.')
    if not 1 <= args.max_tracks <= 128:
        parser.error('--max-tracks must be between 1 and 128.')
    try:
        return camera_diagnostic(args)
    except ImportError as exc:
        emit({'event': 'dependency-missing', 'module': exc.name, 'executable': sys.executable})
        return 1
    except Exception as exc:
        # No exception body: requests can embed an authenticated URL in it.
        emit({'event': 'diagnostic-failed', 'error_type': type(exc).__name__})
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
