"""Small, rotating diagnostics with an explicit public-field allowlist."""
import json
import logging
from logging.handlers import RotatingFileHandler
import time

FIELDS = {'camera_id', 'generation', 'elapsed', 'received', 'count', 'complete', 'reason',
          'state', 'position', 'rate', 'decoded', 'displayed', 'python', 'python_vlc', 'libvlc'}


class Diagnostics:
    def __init__(self, directory):
        self.logger = logging.Logger('archive-playback')
        self.handler = RotatingFileHandler(directory/'events.jsonl',maxBytes=512*1024,backupCount=2,encoding='utf-8')
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.INFO)

    def event(self, event, **values):
        safe = {key:value for key,value in values.items() if key in FIELDS and isinstance(value,(str,int,float,bool))}
        self.logger.info(json.dumps(dict(event=event,monotonic=time.monotonic(),**safe),ensure_ascii=False))

    def close(self):
        self.handler.close()
        self.logger.removeHandler(self.handler)
