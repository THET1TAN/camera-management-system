"""Read existing redacted logs; never launches an application, media process or camera."""
import argparse
import json
from pathlib import Path

MILESTONES = {'first-byte', 'prefix-analysis', 'prefix-insufficient', 'prefix-sufficient',
    'mode-selected', 'producer-start', 'first-segment-published', 'target-available',
    'player-open', 'native-new-frame', 'screen-frame-observed', 'download-complete',
    'producer-failed', 'progressive-failed', 'long-gop-rebase', 'seek-request'}


def summarize(events, camera):
    reports = []
    current = None
    for row in sorted(events, key=lambda row: row.get('monotonic', 0)):
        if row.get('camera_id') != camera:
            continue
        if row.get('event') == 'request':
            current = {'session': row.get('generation'), 'start': row.get('requested_monotonic', row['monotonic']), 'events': []}
            reports.append(current)
        elif current and row.get('event') in MILESTONES:
            if row.get('session_id', row.get('generation')) != current['session']:
                continue  # A superseded owner is not evidence for a new request.
            current['events'].append(dict(event=row['event'],
                seconds=round(row['monotonic']-current['start'], 3),
                **{k: row[k] for k in ('reason','mode','container','received','seek_id','segment_duration',
                   'target_duration','reserve','evidence') if k in row}))
    for report in reports:
        events = report['events']
        download = next((e['seconds'] for e in events if e['event'] == 'download-complete'), None)
        frame = next((e['seconds'] for e in events if e['event'] == 'native-new-frame'), None)
        observed = next((e['seconds'] for e in events if e['event'] == 'screen-frame-observed'), None)
        report['native_frame_before_download_end'] = frame < download if frame is not None and download is not None else None
        report['user_observation_before_download_end'] = observed < download if observed is not None and download is not None else None
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
    print(json.dumps({'camera_id': args.camera, 'sessions': summarize(events, args.camera),
        'note': 'Native counters are not screen observations. F8 records a user observation, including reaction delay.'}, indent=2))


if __name__ == '__main__':
    main()
