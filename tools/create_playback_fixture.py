"""Explicit fixture generation: creates files, never opens a player or camera."""
import argparse
from datetime import datetime, time as daytime, timedelta
import json
from pathlib import Path
import subprocess
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from cryptography.fernet import Fernet
from playback.processes import run
import threading


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',default='playback-fixtures')
    parser.add_argument('--ffmpeg',default='ffmpeg')
    parser.add_argument('--codec',choices=('h264','hevc'),default='h264')
    args=parser.parse_args()
    directory=Path(args.directory).resolve()
    directory.mkdir(parents=True,exist_ok=True)
    if (directory/'fixture.json').exists() or (directory/'fixture.key').exists():
        parser.error('Ce dossier contient déjà une fixture. Choisissez un nouveau dossier.')
    zone=ZoneInfo('America/Toronto')
    start=datetime.combine(datetime.now(zone).date(),daytime(12),zone).timestamp()
    recordings=[]
    cancel=threading.Event()
    for cid,audio in ((901,'aac'),(902,'pcm_mulaw')):
        for number,offset in enumerate((0,24,70)):
            name=f'C{cid}-{number}.mkv'
            path=directory/name
            if path.exists():
                parser.error('Un média de fixture existe déjà dans ce dossier.')
            video=['-c:v','libx264','-preset','ultrafast','-g','48'] if args.codec=='h264' else [
                '-c:v','libx265','-preset','ultrafast','-x265-params','keyint=48:min-keyint=48:scenecut=0:pools=2']
            run([args.ffmpeg,'-nostdin','-hide_banner','-v','warning','-n',
                '-f','lavfi','-i',f'testsrc2=size=640x360:rate=24:duration=24',
                '-f','lavfi','-i',f'sine=frequency={440 if cid==901 else 660}:sample_rate=8000:duration=24',
                *video,'-pix_fmt','yuv420p','-c:a',audio,'-threads','2',str(path)],cancel,timeout=180)
            recordings.append({'camera_id':cid,'file':name,'start':start+offset,'end':start+offset+24})
    (directory/'fixture.key').write_bytes(Fernet.generate_key())
    (directory/'fixture.json').write_text(json.dumps({'zone':'America/Toronto','recordings':recordings},indent=2),encoding='utf-8')
    print('Fixture créée : deux caméras synthétiques, deux archives jointives puis un trou de 22 secondes.')
    print('Aucun lecteur lancé. Le son synthétique sera audible uniquement si vous activez le son dans le lecteur.')


if __name__=='__main__':
    main()
