"""Local session membership and audio; geometry stays in window_positions.

Only IDs, window kinds, instance numbers and mute flags reach SQLite. Each
Viewer owns a new token: late reports from older viewers cannot erase its session.
All application database operations are submitted off the Tk thread.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, replace
import os
import sqlite3
import time
import uuid

from window_positions import PositionStore


SESSION_ENV = ('CAMERA_SESSION_OWNER', 'CAMERA_SESSION_WINDOW',
               'CAMERA_SESSION_SLOT', 'CAMERA_SESSION_MUTED')


@dataclass(frozen=True)
class SessionEntry:
    window_id: str
    camera_id: str
    kind: str
    slot: int = 0
    muted: bool = False

    @property
    def placement_key(self):
        return f'{self.kind}:{self.camera_id}:{self.slot}'

    def environment(self, owner):
        return dict(zip(SESSION_ENV, (owner, self.window_id, str(self.slot),
                                     '1' if self.muted else '0')))

    @classmethod
    def create(cls, camera_id, kind, slot=0, muted=False):
        return cls(uuid.uuid4().hex, str(camera_id), kind, slot, bool(muted))


def valid_entry(entry):
    return (entry.kind in ('video', 'ptz') and isinstance(entry.camera_id, str)
            and entry.camera_id.isascii() and entry.camera_id.isdecimal()
            and 0 < len(entry.camera_id) <= 18 and int(entry.camera_id) > 0
            and type(entry.slot) is int and 0 <= entry.slot < 1000
            and isinstance(entry.window_id, str) and len(entry.window_id) == 32
            and all(c in '0123456789abcdef' for c in entry.window_id)
            and type(entry.muted) is bool)


class SessionStore:
    def __init__(self, path=None):
        self.positions = PositionStore(path)

    def _connect(self):
        db = self.positions._connect()
        try:
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS viewer_session '
                           '(id INTEGER PRIMARY KEY CHECK(id=1), owner TEXT NOT NULL, '
                           'preserve INTEGER NOT NULL DEFAULT 0)')
                db.execute('CREATE TABLE IF NOT EXISTS session_windows '
                           '(window_id TEXT PRIMARY KEY, camera_id TEXT NOT NULL, '
                           'kind TEXT NOT NULL, slot INTEGER NOT NULL, muted INTEGER NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS window_audio '
                           '(placement_key TEXT PRIMARY KEY, muted INTEGER NOT NULL)')
            return db
        except Exception:
            db.close()
            raise

    def load(self):
        with closing(self._connect()) as db:
            rows = db.execute('SELECT window_id,camera_id,kind,slot,muted '
                              'FROM session_windows ORDER BY kind,camera_id,slot').fetchall()
        entries = []
        keys = set()
        for window_id, camera_id, kind, slot, muted in rows:
            if type(muted) is not int or muted not in (0, 1):
                continue
            entry = SessionEntry(window_id, camera_id, kind, slot, bool(muted))
            if valid_entry(entry) and entry.placement_key not in keys:
                entries.append(entry)
                keys.add(entry.placement_key)
        return entries

    def add(self, owner, entry, *, restore=False, claim=False):
        if not valid_entry(entry):
            raise ValueError('Invalid session entry')
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if claim:
                # Replacing a previous session and adding its first window is atomic.
                db.execute('DELETE FROM session_windows')
                db.execute('INSERT OR REPLACE INTO viewer_session VALUES (1,?,0)', (owner,))
            if db.execute('SELECT owner FROM viewer_session WHERE id=1').fetchone() != (owner,):
                raise RuntimeError('Another Viewer owns the saved session')
            previous = db.execute('SELECT muted FROM window_audio WHERE placement_key=?',
                                  (entry.placement_key,)).fetchone()
            if not restore and previous and previous[0] in (0, 1):
                entry = replace(entry, muted=bool(previous[0]))
            # Retrying a crashed window replaces its identity for this slot.
            # Its late close/audio reports must not affect the replacement.
            db.execute('DELETE FROM session_windows WHERE camera_id=? AND kind=? AND slot=?',
                       (entry.camera_id, entry.kind, entry.slot))
            db.execute('INSERT OR REPLACE INTO session_windows VALUES (?,?,?,?,?)',
                       (entry.window_id, entry.camera_id, entry.kind, entry.slot, int(entry.muted)))
            db.execute('INSERT OR REPLACE INTO window_audio VALUES (?,?)',
                       (entry.placement_key, int(entry.muted)))
        return entry

    def report(self, owner, window_id, *, muted=None, closed=False, preserve=False):
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            state = db.execute('SELECT owner,preserve FROM viewer_session WHERE id=1').fetchone()
            if not state or state[0] != owner:
                return
            row = db.execute('SELECT camera_id,kind,slot FROM session_windows WHERE window_id=?',
                             (window_id,)).fetchone()
            if row is None:
                return
            if muted is not None:
                db.execute('UPDATE session_windows SET muted=? WHERE window_id=?',
                           (int(bool(muted)), window_id))
                db.execute('INSERT OR REPLACE INTO window_audio VALUES (?,?)',
                           (f'{row[1]}:{row[0]}:{row[2]}', int(bool(muted))))
            if closed and not preserve and not state[1]:
                db.execute('DELETE FROM session_windows WHERE window_id=?', (window_id,))

    def freeze(self, owner):
        with closing(self._connect()) as db, db:
            db.execute('UPDATE viewer_session SET preserve=1 WHERE id=1 AND owner=?', (owner,))


class SessionWorkspace:
    """Serial Viewer I/O. Startup reads the previous session without replacing it."""
    def __init__(self, path=None):
        self.store = SessionStore(path)
        self.owner = uuid.uuid4().hex
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='Viewer session')
        self.claimed = False
        self.closed = False
        self.loaded = self.pool.submit(self.store.load)

    def load(self):
        return self.pool.submit(self.store.load)

    def open(self, entry, *, restore=False):
        def register():
            value = self.store.add(self.owner, entry, restore=restore, claim=not self.claimed)
            self.claimed = True
            return value
        return self.pool.submit(register)

    def remove(self, entry):
        return self.pool.submit(self.store.report, self.owner, entry.window_id, closed=True)

    def freeze(self):
        future = self.pool.submit(self.store.freeze, self.owner)
        self.shutdown()
        return future

    def shutdown(self):
        if not self.closed:
            self.closed = True
            self.pool.shutdown(wait=False)


class SessionWindow:
    """A child's nonblocking audio/close reporter; never owns camera credentials."""
    def __init__(self, owner, entry, path=None):
        self.owner, self.entry = owner, entry
        self.muted = entry.muted
        self.store = SessionStore(path)
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='Window session')
        self.pending = None
        self.stopping = False
        self.deadline = None

    @classmethod
    def from_environment(cls, camera_id, kind):
        values = [os.environ.pop(name, '') for name in SESSION_ENV]
        owner, window_id, slot, muted = values
        try:
            entry = SessionEntry(window_id, str(camera_id), kind, int(slot), muted == '1')
        except ValueError:
            return None
        if (not valid_entry(entry) or muted not in ('0', '1') or len(owner) != 32
                or any(c not in '0123456789abcdef' for c in owner)):
            return None
        return cls(owner, entry)

    def audio(self, muted):
        if not self.stopping:
            self.muted = bool(muted)
            self.pending = self.pool.submit(self._report, muted=self.muted)

    def _report(self, **changes):
        for attempt in range(3):
            try:
                self.store.report(self.owner, self.entry.window_id, **changes)
                return True
            except (OSError, sqlite3.Error):
                if attempt < 2:
                    time.sleep(.05)
        print('[Session] Unable to save window state.', flush=True)
        return False

    def close(self, *, preserve=False):
        if self.stopping:
            return
        self.stopping = True
        self.deadline = time.monotonic() + .75
        self.pending = self.pool.submit(self._report, muted=self.muted,
                                       closed=True, preserve=preserve)
        self.pool.shutdown(wait=False)

    def finished(self):
        return self.pending is None or self.pending.done() or (
            self.deadline is not None and time.monotonic() >= self.deadline)
