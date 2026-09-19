"""Explicit offline bench. It never reads camera credentials or contacts a camera."""
from datetime import datetime
import json
from pathlib import Path
import threading
import time

from .config import Settings
from .model import Camera, Recording, SearchResult, PlaybackError, check_cancel


def load_fixture(directory):
    from cryptography.fernet import Fernet
    directory = Path(directory).resolve()
    data = json.loads((directory/'fixture.json').read_text(encoding='utf-8'))
    settings = Settings(cache_directory=str(directory/'cache'),display_zone=data.get('zone','America/Toronto'),free_gib=.25)
    cipher = Fernet((directory/'fixture.key').read_bytes())
    cameras = tuple(Camera(int(cid),'fixture.invalid','','',zone=settings.display_zone) for cid in sorted({str(r['camera_id']) for r in data['recordings']}))
    return settings,cameras,cipher,data


class FixtureBackend:
    def __init__(self,camera,directory,data):
        self.camera,self.directory,self.data=camera,Path(directory).resolve(),data
        self.device='synthetic-fixture'

    def list_recordings(self,start,end,cancel):
        check_cancel(cancel)
        records=[]
        for row in self.data['recordings']:
            if row['camera_id']!=self.camera.camera_id or row['start']>=end or row['end']<=start:
                continue
            path=self.directory/row['file']
            if path.is_symlink() or path.resolve().parent!=self.directory or not path.is_file():
                raise PlaybackError('fixture-invalid')
            records.append(Recording(self.camera.camera_id,self.device,'fixture','1',row['file'],
                row['start'],row['end'],path.stat().st_size,row['file'],str(row['start']),str(row['end']),time.time(),'synthetic'))
        return SearchResult(tuple(records),True,'',time.time())

    def download(self,recording,target,cancel,limit,progress):
        source=self.directory/recording.locator
        if source.is_symlink() or source.resolve().parent!=self.directory:
            raise PlaybackError('fixture-invalid')
        size=source.stat().st_size
        if size>limit:
            raise PlaybackError('archive-too-large')
        received=0
        began=time.monotonic()
        with source.open('rb') as inp,target.open('wb') as out:
            for chunk in iter(lambda:inp.read(256*1024),b''):
                check_cancel(cancel)
                out.write(chunk)
                received+=len(chunk)
                progress(received,size,max(.001,time.monotonic()-began))
        return received

    def close(self):
        pass
