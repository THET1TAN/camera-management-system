"""Killable camera I/O owners. Credentials and locators travel only over pipes."""
from dataclasses import asdict
from collections import deque
import json
from pathlib import Path
from queue import Queue, Empty
import subprocess
import sys
import threading
import time

from .model import Recording, SearchResult, PlaybackError, check_cancel
from .processes import Process
from .diagnostics import safe_fields

MAX_MESSAGE = 8*1024*1024


class RemoteBackend:
    def __init__(self, camera, cancel, diagnostic=None):
        self.diagnostic = diagnostic
        self.events = deque(maxlen=128)
        self.owned = Process([sys.executable, '-u', str(Path(__file__).resolve().parent.parent/'playback_io_worker.py')],
                             stdin=subprocess.PIPE)
        self.results = Queue(maxsize=2)
        self.progress = None
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.stderr = threading.Thread(target=self._drain, daemon=True)
        self.reader.start()
        self.stderr.start()
        try:
            result = self._call({'operation':'connect','camera':asdict(camera)}, cancel, 35)
            self.device = result['device']
            self.tracks = tuple(result['tracks'])
            self.track_info = tuple(result.get('track_info', ()))
            self.attempts = tuple(result.get('attempts', ()))
            self.name = result['backend']
            self.runtime = result['runtime']
            if self.diagnostic:
                self.diagnostic({'camera_id': camera.camera_id, 'backend': self.name, 'stage': 'python_runtime',
                    'python': self.runtime['version'], 'python_executable_matches_parent':
                    self.runtime['executable'] == sys.executable})
        except Exception:
            self.close()
            raise

    def _drain(self):
        try:
            while self.owned.process.stderr.read(8192):
                pass
        except (OSError, ValueError):
            pass

    def _read(self):
        try:
            while True:
                line = self.owned.process.stdout.readline(MAX_MESSAGE+1)
                if not line or len(line)>MAX_MESSAGE or not line.endswith(b'\n'):
                    return
                result=json.loads(line)
                if result.get('kind')=='diagnostic':
                    self.events.append(safe_fields(result.get('detail', {})))
                elif result.get('kind')=='progress':
                    self.progress=(int(result['received']),int(result['expected']),float(result['elapsed']))
                else:
                    self.results.put(result,timeout=1)
        except Exception:
            pass

    def _call(self, message, cancel, timeout, progress=None):
        check_cancel(cancel)
        self.progress=None
        line=(json.dumps(message)+'\n').encode()
        if len(line)>65536:
            raise PlaybackError('request-limit')
        remaining=memoryview(line)
        while remaining:
            count=self.owned.process.stdin.write(remaining)
            if not count:
                raise PlaybackError('camera-worker-failed')
            remaining=remaining[count:]
        self.owned.process.stdin.flush()
        deadline=time.monotonic()+timeout
        delivered=None
        while True:
            self._deliver_events()
            check_cancel(cancel)
            if progress and self.progress is not None and self.progress!=delivered:
                delivered=self.progress
                progress(*delivered)
            try:
                message=self.results.get(timeout=.05)
            except Empty:
                if self.owned.process.poll() is not None:
                    self.reader.join(timeout=.2)
                    if not self.results.empty():
                        continue
                    self._deliver_events()
                    raise PlaybackError('camera-worker-failed')
                if time.monotonic()>deadline:
                    raise PlaybackError('temporarily-unreachable')
                continue
            if message.get('kind')=='error':
                # Worker error text is produced from fixed codes, never exceptions.
                code=message.get('code','camera-worker-failed')
                if not isinstance(code,str) or len(code)>80 or not all(c.islower() or c=='-' for c in code):
                    code='camera-worker-failed'
                self._deliver_events()
                raise PlaybackError(code, tuple(safe_fields(d) for d in message.get('details', ())[:128]))
            self._deliver_events()
            return message['result']

    def _deliver_events(self):
        while self.events:
            item = self.events.popleft()
            if self.diagnostic:
                self.diagnostic(item)

    def list_recordings(self,start,end,cancel):
        data=self._call({'operation':'search','start':start,'end':end},cancel,120)
        return SearchResult(tuple(Recording(**r) for r in data['records']),data['complete'],data['reason'],data['observed'],
                            tuple(safe_fields(d) for d in data.get('failures', ())))

    def download(self,recording,target,cancel,limit,progress):
        return self._call({'operation':'download','recording':asdict(recording),'target':str(target),'limit':limit},
                          cancel,1800,progress)['received']

    def close(self):
        self._deliver_events()
        self.owned.close()
        self.reader.join(timeout=1)
        self.stderr.join(timeout=1)


def worker_main():
    from .backends import connect
    from .model import Camera
    backend=None
    cancel=threading.Event()
    def emit(message):
        print(json.dumps(message),flush=True)
    try:
        for line in sys.stdin.buffer:
            if len(line)>65536:
                break
            try:
                command=json.loads(line)
                operation=command.get('operation')
                if operation=='connect' and backend is None:
                    backend=connect(Camera(**command['camera']),cancel,
                        diagnostic=lambda detail:emit({'kind':'diagnostic','detail':detail}))
                    result={'device':backend.device,'tracks':backend.tracks,'track_info':backend.track_info,
                            'backend':backend.name,'attempts':backend.attempts,
                            'runtime':{'version':sys.version.split()[0],'executable':sys.executable}}
                elif operation=='search' and backend is not None:
                    found=backend.list_recordings(command['start'],command['end'],cancel)
                    result=asdict(found)
                    if len(json.dumps(result).encode())>MAX_MESSAGE-1024:
                        raise PlaybackError('response-limit')
                elif operation=='download' and backend is not None:
                    last=[0.]
                    def progress(received,expected,elapsed):
                        now=time.monotonic()
                        if now-last[0]>.1:
                            emit({'kind':'progress','received':received,'expected':expected,'elapsed':elapsed})
                            last[0]=now
                    received=backend.download(Recording(**command['recording']),Path(command['target']),cancel,
                                              int(command['limit']),progress)
                    result={'received':received}
                else:
                    raise PlaybackError('request-invalid')
                emit({'kind':'result','result':result})
            except PlaybackError as exc:
                if backend is not None and not exc.details:
                    backend.trace.reject(exc)
                emit({'kind':'error','code':exc.code,'details':exc.details[:128]})
            except Exception:
                emit({'kind':'error','code':'camera-worker-failed'})
    except Exception:
        pass
    finally:
        if backend:
            backend.close()
