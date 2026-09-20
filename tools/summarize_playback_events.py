"""Read existing redacted logs; never launches media, a window or a camera."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from playback.diagnostics import safe_fields

MILESTONES = {'first-byte', 'first-progress', 'prefix-analysis', 'prefix-result',
    'prefix-insufficient', 'prefix-sufficient', 'mode-selected', 'producer-start',
    'first-segment-published', 'segments-ready', 'target-available', 'target-wait',
    'player-open', 'native-new-frame', 'screen-frame-observed', 'download-complete',
    'producer-failed', 'progressive-failed', 'long-gop-rebase', 'seek-request',
    'error', 'native-failure', 'native-sample', 'native-seek-issued', 'native-seek-evidence',
    'archive-prepare', 'archive-boundary', 'prefetch-start', 'preparation-failed',
    'successor-unavailable', 'loopback-response', 'loopback-transfer-failed', 'native-clock-regression'}


def measurements(events):
    def first(name):
        return next((e['seconds'] for e in events if e['event'] == name), None)
    download, frame, observed = first('download-complete'), first('native-new-frame'), first('screen-frame-observed')
    result = dict(native_frame_before_download_end=frame < download if frame is not None and download is not None else None,
                  user_observation_before_download_end=observed < download if observed is not None and download is not None else None)
    names = {e['event'] for e in events}
    if any(e.get('mode') == 'complete' for e in events):
        classification = 'A: complete-file path selected'
    elif 'native-failure' in names or ('player-open' in names and frame is None):
        classification = 'D: player opened, no confirmed new image'
    elif 'target-wait' in names or (first('first-segment-published') is not None and first('target-available') is not None
                                  and first('target-available')-first('first-segment-published') > 2):
        classification = 'E: requested position awaited prepared coverage (legacy inference when target-wait absent)'
    elif 'first-segment-published' in names and 'player-open' not in names:
        classification = 'C: segments published, player not opened'
    elif 'producer-start' in names and 'first-segment-published' not in names:
        classification = 'B: producer started, no published segment recorded'
    else:
        classification = 'insufficient evidence or no initial wait'
    result['classification'] = classification
    result['terminal_errors'] = [e for e in events if e['event'] in ('error','native-failure','preparation-failed')]
    return result


def summarize(events, camera):
    reports, active = [], {}
    current = None
    for row in sorted(events, key=lambda row: row.get('monotonic', 0)):
        if row.get('camera_id') != camera:
            continue
        run = row.get('run_id')
        if row.get('event') == 'request':
            current = {'session': row.get('generation'), 'run_id': run,
                'requested_position': row.get('position'),
                'start': row.get('requested_monotonic', row['monotonic']), 'events': []}
            reports.append(current)
            active[(run, current['session'])] = current
        elif current and row.get('event') in MILESTONES:
            session = row.get('session_id')
            inferred = session is None
            # Legacy native generation != controller session. Keep untagged
            # terminal errors, but explicitly label the attribution as inferred.
            report = current if session is None else active.get((run, session))
            if report is None:
                continue
            values = safe_fields(row)
            values.pop('run_id', None)
            report['events'].append(dict(event=row['event'],
                seconds=round(row['monotonic']-report['start'], 3), session_inferred=inferred, **values))
    for report in reports:
        archives = {}
        for event in report['events']:
            archives.setdefault(event.get('archive_id', 'unattributed'), []).append(event)
        report['archives'] = {key: dict(events=rows, **measurements(rows)) for key, rows in archives.items()}
        initial = next((e['archive_id'] for e in report['events'] if 'archive_id' in e and
                        (e['event'] == 'archive-prepare' and e.get('origin') == 'initial')), None)
        initial = initial or next((e['archive_id'] for e in report['events'] if 'archive_id' in e), None)
        evidence = [e for e in report['events'] if e.get('archive_id', initial) == initial] if initial else report['events']
        report.update(measurements(evidence))
        report['terminal_errors'] = [e for e in report['events'] if e['event'] in ('error','native-failure','preparation-failed')]
        del report['start']
    return reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--log', required=True)
    parser.add_argument('--camera', type=int, required=True)
    args = parser.parse_args()
    path = Path(args.log)
    events = []
    for item in (path.with_name(path.name+'.2'), path.with_name(path.name+'.1'), path):
        if item.is_file():
            for line in item.read_text(encoding='utf-8').splitlines():
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        events.append(value)
                except ValueError:
                    pass
    print(json.dumps({'camera_id': args.camera, 'builds': [safe_fields(e) for e in events if e.get('event') == 'runtime-build'],
        'sessions': summarize(events, args.camera),
        'note': 'Native counters are not screen observations. F8 records user reaction time. Legacy attribution can be incomplete.'}, indent=2))


if __name__ == '__main__':
    main()
