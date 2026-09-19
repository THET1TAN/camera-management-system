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


@dataclass(frozen=True)
class Entry:
    recording: Recording
    state: str = 'indexed'
    media: dict = None
    remote: bool = True

    @property
    def end(self):
        return (self.recording.start + self.media['duration']) if self.media else self.recording.end


class Store:
    def __init__(self, settings, cipher):
        self.settings, self.cipher = settings, cipher
        self.root = settings.cache_path
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
        except Exception:
            self.close()
            raise

    def close(self):
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
        if path.is_symlink() or path.resolve().parent != self.root:
            raise PlaybackError('cache-path')
        return path

    def record_search(self, camera, start, end, device, result):
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

    def entries(self, camera_ids, start, end):
        if not camera_ids:
            return ()
        with self.lock:
            rows = self.db.execute('SELECT sealed,state,media,remote FROM recordings WHERE camera IN ('+
                ','.join('?' for _ in camera_ids)+') AND (remote=1 OR state IN (\'prepared\',\'downloaded\',\'preparing\')) '
                'AND start<? AND (end>? OR (end IS NULL AND start>=?)) ORDER BY start LIMIT ?',
                (*camera_ids, end, start, start-86400, self.settings.max_index_entries+1)).fetchall()
        if len(rows)>self.settings.max_index_entries:
            raise PlaybackError('index-view-limit')
        entries = []
        for sealed, state, media, remote in rows:
            try:
                r = Recording(**json.loads(self.cipher.decrypt(sealed)))
                entries.append(Entry(r, state, json.loads(media) if media else None, bool(remote)))
            except Exception:
                raise PlaybackError('cache-key') from None
        return tuple(entries)

    def entry(self, key):
        with self.lock:
            row = self.db.execute('SELECT sealed,state,media,remote FROM recordings WHERE key=?', (key,)).fetchone()
        if not row:
            raise PlaybackError('recording-unavailable')
        return Entry(Recording(**json.loads(self.cipher.decrypt(row[0]))), row[1],
                     json.loads(row[2]) if row[2] else None, bool(row[3]))

    def state(self, key, state, media=None):
        with self.lock, self.db:
            if media is None:
                self.db.execute('UPDATE recordings SET state=?,used=? WHERE key=?', (state, time.time(), key))
            else:
                self.db.execute('UPDATE recordings SET state=?,media=?,end=start+?,used=? WHERE key=?',
                    (state, json.dumps(media), media['duration'], time.time(), key))

    def pin(self, key):
        with self.lock:
            self.pins.add(key)

    def unpin(self, key):
        with self.lock:
            self.pins.discard(key)

    def usage(self):
        total = 0
        for directory in self.root.iterdir():
            if re.fullmatch('[0-9a-f]{64}', directory.name) and directory.is_dir() and not directory.is_symlink():
                for path in directory.iterdir():
                    if path.is_file() and not path.is_symlink():
                        total += path.stat().st_size
        return total

    def ensure_space(self, reserve=0):
        """Only inactive, owned cache directories are evicted; never exports."""
        with self.lock:
            quota = int(self.settings.cache_gib*1024**3)
            free_floor = int(self.settings.free_gib*1024**3)
            used = self.usage()
            rows = self.db.execute('SELECT key,used FROM recordings ORDER BY used').fetchall()
            for key, last_used in rows:
                free = shutil.disk_usage(self.root).free
                expired = time.time()-last_used > self.settings.ttl_days*86400
                if not expired and used+reserve <= quota and free-reserve >= free_floor:
                    break
                if key in self.pins:
                    continue
                path = self.path(key)
                if path.exists():
                    # No recursive deletion: only regular files in a validated
                    # direct child created by this cache are eligible.
                    for child in path.iterdir():
                        if child.is_symlink() or not child.is_file():
                            raise PlaybackError('cache-path')
                    for child in path.iterdir():
                        size = child.stat().st_size
                        child.unlink()
                        used -= size
                    path.rmdir()
                self.db.execute("UPDATE recordings SET state='expired',media=NULL WHERE key=?", (key,))
            self.db.commit()
            if used+reserve > quota or shutil.disk_usage(self.root).free-reserve < free_floor:
                raise PlaybackError('cache-full')
