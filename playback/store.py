"""Separate encrypted archive index and bounded, locally managed media cache."""
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import threading
import time

from .model import Recording, PlaybackError
from .cache import CacheManager, linked


@dataclass(frozen=True)
class Entry:
    recording: Recording
    state: str = 'indexed'
    media: dict = None
    remote: bool = True

    @property
    def end(self):
        return (self.recording.start + self.media['duration']) if self.media else self.recording.end


class Store(CacheManager):
    def __init__(self, settings, cipher, allow_sync=False, maintenance=True):
        self.settings, self.cipher = settings, cipher
        self.root = settings.cache_path
        # Refuse linked ancestors before resolving them. Never treat the project,
        # its credentials or an arbitrary drive root as a disposable cache.
        if any(linked(p) for p in (self.root, *self.root.parents)) or self.root.parent == self.root:
            raise PlaybackError('cache-path')
        if any((self.root/name).exists() for name in ('camera_credentials.db', '.camera_encryption.key', 'playback.json')):
            raise PlaybackError('cache-path')
        if any(linked(self.root/name) for name in ('owner.lock','index.sqlite3','index.sqlite3-wal','index.sqlite3-shm')):
            raise PlaybackError('cache-path')
        if not allow_sync and any(p.name.lower().startswith('onedrive') for p in (self.root, *self.root.parents)):
            raise PlaybackError('cache-path')
        if self.root.exists() and not (self.root/'index.sqlite3').exists() and any(self.root.iterdir()):
            raise PlaybackError('cache-directory-not-empty')
        self.root.mkdir(parents=True, exist_ok=True)
        self.root = self.root.resolve()
        self.lock = threading.RLock()
        self.pins = set()
        self._owner = (self.root/'owner.lock').open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                self._owner.seek(0)
                if self._owner.read(1) == b'':
                    self._owner.write(b'0')
                    self._owner.flush()
                self._owner.seek(0)
                msvcrt.locking(self._owner.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._owner.close()
            raise PlaybackError('cache-in-use') from None
        self.db = sqlite3.connect(self.root/'index.sqlite3', check_same_thread=False, timeout=2)
        try:
            version = self.db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1):
                raise PlaybackError('cache-version')
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA journal_size_limit=16777216')
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS recordings (
                    key TEXT PRIMARY KEY, identity TEXT NOT NULL, camera INTEGER NOT NULL,
                    device TEXT NOT NULL, start REAL NOT NULL, end REAL, sealed BLOB NOT NULL,
                    state TEXT NOT NULL DEFAULT 'indexed', media TEXT, remote INTEGER NOT NULL DEFAULT 1,
                    used REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS by_time ON recordings(camera,start,end);
                CREATE TABLE IF NOT EXISTS searches (
                    camera INTEGER, start REAL, end REAL, checked REAL, complete INTEGER,
                    reason TEXT, device TEXT, PRIMARY KEY(camera,start,end));
                PRAGMA user_version=1;
            ''')
            # Interrupted writers are never published as readable. Valid prepared
            # entries survive restart and remain available without any camera.
            self.db.execute("UPDATE recordings SET state='failed' WHERE state IN ('partial','preparing')")
            self.db.commit()
            self._cache_init(maintenance)
        except Exception:
            self.close()
            raise

    def close(self):
        self._cache_close()
        with self.lock:
            if getattr(self, 'db', None) is not None:
                self.db.close()
                self.db = None
            if not self._owner.closed:
                self._owner.close()

    def path(self, key):
        if not re.fullmatch('[0-9a-f]{64}', key):
            raise PlaybackError('cache-path')
        path = self.root/key
        if linked(path) or path.resolve().parent != self.root:
            raise PlaybackError('cache-path')
        return path

    def record_search(self, camera, start, end, device, result):
        # Stop growing metadata honestly rather than deleting availability to
        # make a full index appear empty. Logs rotate independently.
        index_bytes = sum(p.stat().st_size for p in (self.root/'index.sqlite3', self.root/'index.sqlite3-wal') if p.exists())
        if index_bytes > 128*1024**2:
            raise PlaybackError('index-capacity')
        with self.lock, self.db:
            self.db.execute('UPDATE recordings SET remote=0 WHERE camera=? AND device<>?', (camera, device))
            if result.complete:
                self.db.execute('UPDATE recordings SET remote=0 WHERE camera=? AND start<? AND (end>? OR end IS NULL)',
                                (camera, end, start))
            for r in result.records:
                sealed = self.cipher.encrypt(json.dumps(asdict(r)).encode())
                self.db.execute('''INSERT INTO recordings(key,identity,camera,device,start,end,sealed,used)
                    VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET sealed=excluded.sealed,remote=1''',
                    (r.key, r.identity, r.camera_id, r.device, r.start, r.end, sealed, time.time()))
                # Size/end changes identify a different observed revision. Retain
                # older downloaded bytes, but never treat them as the current file.
                self.db.execute('UPDATE recordings SET remote=0 WHERE identity=? AND key<>?', (r.identity, r.key))
            self.db.execute('INSERT OR REPLACE INTO searches VALUES(?,?,?,?,?,?,?)',
                (camera, start, end, result.observed, int(result.complete), result.reason, device))

    def search_error(self, camera, start, end, reason):
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO searches VALUES(?,?,?,?,?,?,?)',
                            (camera, start, end, time.time(), 0, reason, ''))

    def search_status(self, camera, start, end):
        with self.lock:
            return self.db.execute('SELECT checked,complete,reason,device FROM searches WHERE camera=? AND start=? AND end=?',
                                   (camera, start, end)).fetchone()

    def invalidate_searches(self, camera):
        """Forget lookup freshness after local settings change; keep all media."""
        with self.lock, self.db:
            self.db.execute('DELETE FROM searches WHERE camera=?', (camera,))

    def entries(self, camera_ids, start, end):
        if not camera_ids:
            return ()
        with self.lock:
            rows = self.db.execute('SELECT sealed,state,media,remote,end FROM recordings WHERE camera IN ('+
                ','.join('?' for _ in camera_ids)+') AND (remote=1 OR state IN (\'prepared\',\'downloaded\',\'preparing\')) '
                'AND start<? AND (end>? OR (end IS NULL AND start>=?)) ORDER BY start LIMIT ?',
                (*camera_ids, end, start, start-86400, self.settings.max_index_entries+1)).fetchall()
        if len(rows)>self.settings.max_index_entries:
            raise PlaybackError('index-view-limit')
        entries = []
        for sealed, state, media, remote, known_end in rows:
            try:
                r = Recording(**json.loads(self.cipher.decrypt(sealed)))
                if r.end is None and known_end is not None:
                    r = replace(r, end=known_end)
                entries.append(Entry(r, state, json.loads(media) if media else None, bool(remote)))
            except Exception:
                raise PlaybackError('cache-key') from None
        return tuple(entries)

    def entry(self, key):
        with self.lock:
            row = self.db.execute('SELECT sealed,state,media,remote,end FROM recordings WHERE key=?', (key,)).fetchone()
        if not row:
            raise PlaybackError('recording-unavailable')
        recording = Recording(**json.loads(self.cipher.decrypt(row[0])))
        if recording.end is None and row[4] is not None:
            recording = replace(recording, end=row[4])
        return Entry(recording, row[1],
                     json.loads(row[2]) if row[2] else None, bool(row[3]))

    def state(self, key, state, media=None):
        with self.lock, self.db:
            if media is None:
                self.db.execute('UPDATE recordings SET state=?,used=? WHERE key=?', (state, time.time(), key))
            else:
                self.db.execute('UPDATE recordings SET state=?,media=?,end=start+?,used=? WHERE key=?',
                    (state, json.dumps(media), media['duration'], time.time(), key))
