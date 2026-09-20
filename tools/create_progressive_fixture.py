"""User-run FFmpeg fixture with frame counter and slow transfer; never opens a player."""
import argparse
from datetime import datetime, time as daytime
import json
from pathlib import Path
import sys
import threading
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cryptography.fernet import Fernet
from playback.processes import run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, help='New directory; existing data is never replaced')
    parser.add_argument('--ffmpeg', default='ffmpeg')
    parser.add_argument('--font-file', default='C:/Windows/Fonts/consola.ttf')
    args = parser.parse_args()
    directory = Path(args.directory).resolve()
    if directory.exists():
        parser.error('Choose a new directory for a cold cache and preserved evidence.')
    font = Path(args.font_file).resolve()
    if not font.is_file():
        parser.error('Font file not found; set --font-file to an installed TrueType font.')
    directory.mkdir(parents=True)
    start = datetime.combine(datetime.now(ZoneInfo('America/Toronto')).date(), daytime(12),
                             ZoneInfo('America/Toronto')).timestamp()
    rows = []
    escaped_font = font.as_posix().replace(':', '\\:').replace("'", "\\'")
    for cid, suffix, gop, options in ((901, 'ts', 48, []), (902, 'ts', 480, []),
                                     (903, 'mp4', 48, []), (904, 'mp4', 48, ['-movflags', '+faststart'])):
        for index, label in enumerate(('A', 'B')):
            filename = f'C{cid}-{label}.{suffix}'
            counter = (f"drawtext=fontfile='{escaped_font}':text='C{cid} archive {label}  %{{pts\\:hms}}  frame %{{n}}':"
                       'x=12:y=12:fontsize=24:fontcolor=white:box=1:boxcolor=black@0.8')
            run([args.ffmpeg, '-nostdin', '-hide_banner', '-v', 'warning', '-n',
                 '-f', 'lavfi', '-i', 'testsrc2=size=640x360:rate=24:duration=180',
                 '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=180',
                 '-vf', counter, '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
                 '-g', str(gop), '-keyint_min', str(gop), '-sc_threshold', '0',
                 '-b:v', '2M', '-minrate', '2M', '-maxrate', '2M', '-bufsize', '4M',
                 '-x264-params', 'nal-hrd=cbr', '-c:a', 'aac', '-b:a', '64k', '-threads', '2',
                 *options, str(directory/filename)], threading.Event(), timeout=300)
            rows.append({'camera_id': cid, 'file': filename, 'start': start+index*180, 'end': start+(index+1)*180})
    (directory/'fixture.key').write_bytes(Fernet.generate_key())
    (directory/'fixture.json').write_text(json.dumps({'zone': 'America/Toronto', 'chunk_delay': .3,
        'recordings': rows}, indent=2), encoding='utf-8')
    print('Created TWO consecutive native archives per camera: C901 MPEG-TS, C902 long GOP, C903 tail-metadata MP4, C904 faststart MP4.')
    print('Transfer is slowed by 0.3 seconds per 256 KiB. No player was opened.')
    print('Select 12:00:02. A starts at 12:00, B at 12:03; each displays its own frame counter; audio starts muted.')


if __name__ == '__main__':
    main()
