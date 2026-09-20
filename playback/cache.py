"""Owned cache leases, reservations and lifecycle maintenance (no Tk calls)."""
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import threading
import time
import uuid

from .model import PlaybackError

GIB = 1024**3
OWNED = re.compile(r'(original\.(bin|part)|source\.m3u8(?:\.tmp)?|segment-\d+\.ts(?:\.tmp)?|export-[\w.-]+|preview-[\w.-]+)')


def linked(path):
    """Windows junctions are reparse points, even when is_symlink is false."""
    try:
        return path.is_symlink() or bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400)
    except FileNotFoundError:
        return False


@dataclass
class Reservation:
    store: object
    owner: str
    key: str
    total: int
    baseline: int
    written: int = 0
    checked: float = 0.

    def check(self, force=False):
        if force or time.monotonic()-self.checked >= 1:
            actual = self.store._directory_size(self.key)
            with self.store.lock:
                self.checked = time.monotonic()
                self.written = max(self.written, actual-self.baseline)
                if self.written > self.total:
                    raise PlaybackError('cache-reservation-exceeded')
        if shutil.disk_usage(self.store.root).free < self.store.settings.free_gib*GIB:
            raise PlaybackError('cache-full')

    @property
    def remaining(self):
        return max(0, self.total-self.written)


class CacheManager:
    def _cache_init(self, maintenance=True):
        self.pins = {}
        self.reservations = {}
        self.deleting = set()
        self.writers = {}
        self.touches = {}
        self.cache_stop = threading.Event()
        self.cache_wake = threading.Event()
        self.inventory_lock = threading.Lock()
        self.allocation_lock = threading.RLock()
        self.cache_action = None
        self.cache_result = None
        self.cache_snapshot = {}
        self.cache_revision = 0
        self.last_cleanup = ''
        self.db.execute('CREATE TABLE IF NOT EXISTS workspaces (key TEXT PRIMARY KEY, used REAL)')
        self.db.execute('UPDATE workspaces SET used=0')
        self.db.execute('CREATE TABLE IF NOT EXISTS maintenance (id INTEGER PRIMARY KEY CHECK(id=1), stamp TEXT, result TEXT)')
        previous = self.db.execute('SELECT stamp,result FROM maintenance WHERE id=1').fetchone()
        self.last_result = json.loads(previous[1]) if previous else None
        self.last_cleanup = previous[0] if previous else ''
        self.db.commit()
        # A prior process cannot own a lease after the exclusive owner.lock was
        # obtained. Evidence holds are persistent settings, deliberately separate.
        self.reconcile()
        self.cache_thread = threading.Thread(target=self._maintenance, name='Archive cache', daemon=True)
        if maintenance:
            self.cache_thread.start()

    def _cache_close(self):
        if hasattr(self, 'cache_stop'):
            self.cache_stop.set()
            self.cache_wake.set()
            if getattr(self, 'cache_thread', None) and self.cache_thread.ident:
                self.cache_thread.join()
            if getattr(self, 'db', None) is not None:
                self.flush_touches()

    def _safe_children(self, path):
        if linked(path) or path.resolve().parent != self.root:
            raise PlaybackError('cache-path')
        children = tuple(p for p in path.iterdir() if p.exists() or linked(p)) if path.exists() else ()
        with self.lock:
            workspace = self.db.execute('SELECT 1 FROM workspaces WHERE key=?',(path.name,)).fetchone() is not None
        if any(linked(p) or not p.is_file() or not OWNED.fullmatch(p.name) or
               (p.name.startswith('export-') != workspace) for p in children):
            raise PlaybackError('cache-unknown-files')
        return children

    def _directory_size(self, key):
        size = 0
        for path in self._safe_children(self.path(key)):
            try:
                size += path.stat().st_size
            except FileNotFoundError:
                pass  # Atomic HLS temp-file publication may rename this file.
        return size

    def file(self, key, name):
        directory = self.path(key)
        path = directory/name
        if not OWNED.fullmatch(name) or linked(path) or path.resolve().parent != directory:
            raise PlaybackError('cache-path')
        return path

    def _inventory(self):
        # Full inventories run on workers, never in the Tk polling callback.
        with self.inventory_lock:
            with self.lock:
                rows = self.db.execute('SELECT key,used,camera FROM recordings UNION ALL SELECT key,used,0 FROM workspaces').fetchall()
            known = {r[0] for r in rows}
            sizes, uncertain, other, unsafe = {}, 0, 0, set()
            for p in self.root.iterdir():
                if linked(p):
                    uncertain += 1
                    continue
                if p.is_dir():
                    try:
                        size = sum(c.stat().st_size for c in p.iterdir() if c.is_file() and not linked(c))
                    except OSError:
                        uncertain += 1
                        continue
                    sizes[p.name] = size
                    if p.name not in known:
                        uncertain += 1
                    else:
                        try:
                            self._safe_children(p)
                        except PlaybackError:
                            unsafe.add(p.name)
                            uncertain += 1
                elif p.is_file():
                    other += p.stat().st_size
            with self.lock:
                protected = set(self.pins) | set(self.settings.protected_archives) | unsafe
                for key in sizes:
                    if key not in known:
                        protected.add(key)
                value = dict(media=sum(sizes.values()), other=other, total=sum(sizes.values())+other,
                    protected=sum(sizes.get(k, 0) for k in protected),
                    reclaimable=sum(sizes.get(k, 0) for k in known-protected),
                    reserved=sum(r.remaining for r in self.reservations.values()),
                    free=shutil.disk_usage(self.root).free, limit=int(self.settings.cache_gib*GIB),
                    uncertain=uncertain, path=str(self.root), last_cleanup=self.last_cleanup, last_result=self.last_result,
                    retention=self.settings.ttl_days, margin=self.settings.free_gib, sizes=sizes,
                    scanned=time.monotonic())
                self.cache_snapshot = value
            return rows, value

    def reconcile(self):
        with self.lock:
            rows = self.db.execute("SELECT key,state FROM recordings WHERE state IN ('prepared','downloaded')").fetchall()
        for key, state in rows:
            try:
                directory = self.path(key)
                if not (directory/'original.bin').is_file():
                    self.state(key, 'expired')
                elif state == 'prepared' and not (directory/'source.m3u8').is_file():
                    self.state(key, 'downloaded')
            except (OSError, PlaybackError):
                pass  # Unknown or unsafe paths are preserved and reported.

    def touch(self, key):
        with self.lock:
            self.touches[key] = time.time()

    def flush_touches(self):
        with self.lock, self.db:
            self.db.executemany('UPDATE recordings SET used=? WHERE key=?', [(v,k) for k,v in self.touches.items()])
            self.touches.clear()

    def pin(self, key, owner='legacy'):
        self.path(key)
        with self.lock:
            if key in self.deleting:
                raise PlaybackError('cache-cleaning')
            owners = self.pins.setdefault(key, {})
            owners[owner] = owners.get(owner, 0)+1
            self.touches[key] = time.time()

    def unpin(self, key, owner='legacy'):
        with self.lock:
            owners = self.pins.get(key, {})
            count = owners.get(owner, 0)
            if count > 1:
                owners[owner] = count-1
            else:
                owners.pop(owner, None)
            if not owners:
                self.pins.pop(key, None)

    @contextmanager
    def lease(self, key, owner):
        self.pin(key, owner)
        try:
            yield
        finally:
            self.unpin(key, owner)

    @contextmanager
    def writer(self, key, cancel):
        with self.lock:
            lock, users = self.writers.get(key, (threading.Lock(),0))
            self.writers[key] = (lock,users+1)
        acquired = False
        try:
            while not lock.acquire(timeout=.1):
                if cancel.is_set():
                    from .model import Cancelled
                    raise Cancelled()
            acquired = True
            yield
        finally:
            if acquired:
                lock.release()
            with self.lock:
                _, users = self.writers[key]
                if users == 1:
                    del self.writers[key]
                else:
                    self.writers[key] = (lock,users-1)

    @contextmanager
    def reserve(self, owner, key, amount):
        amount = max(0, int(amount))
        with self.allocation_lock:
            self.ensure_space(amount)
            baseline = self._directory_size(key)
            reservation = Reservation(self, owner, key, amount, baseline)
            with self.lock:
                if owner in self.reservations:
                    raise PlaybackError('cache-reservation-owner')
                self.reservations[owner] = reservation
        try:
            yield reservation
        finally:
            with self.lock:
                self.reservations.pop(owner, None)
            self.cache_wake.set()

    @contextmanager
    def workspace(self, owner):
        key = uuid.uuid4().hex+uuid.uuid4().hex
        with self.lock, self.db:
            self.db.execute('INSERT INTO workspaces VALUES(?,?)', (key, time.time()))
        with self.lease(key, owner):
            self.path(key).mkdir()
            try:
                yield key, self.path(key)
            finally:
                with self.lock, self.db:
                    self.db.execute('UPDATE workspaces SET used=0 WHERE key=?', (key,))
                self.cache_wake.set()

    def usage(self):
        return self._inventory()[1]['media']

    def plan_cleanup(self, camera=None, automatic=False, reserve=0):
        self.flush_touches()
        rows, snapshot = self._inventory()
        now = time.time()
        budget = snapshot['media']+snapshot['reserved']+reserve
        pressure = budget >= snapshot['limit']*self.settings.cleanup_high or snapshot['free']-snapshot['reserved']-reserve < self.settings.free_gib*GIB
        target = snapshot['limit']*self.settings.cleanup_low
        candidates = []
        with self.lock:
            protected = set(self.pins) | set(self.settings.protected_archives)
        for key, used, cid in sorted(rows, key=lambda r:r[1]):
            if key in protected or (camera is not None and camera != cid):
                continue
            if automatic and now-used < self.settings.ttl_days*86400 and not pressure:
                continue
            try:
                self._safe_children(self.path(key))
            except (PlaybackError, OSError):
                continue
            size = snapshot['sizes'].get(key, 0)
            if size:
                candidates.append((key, size))
                budget -= size
                if budget <= target and snapshot['free']+sum(s for _,s in candidates)-snapshot['reserved']-reserve >= self.settings.free_gib*GIB:
                    pressure = False
        return dict(kind='plan', camera=camera, automatic=automatic, planned_at=now, candidates=candidates,
                    estimated=sum(size for _,size in candidates), protected=snapshot['protected'])

    def execute_cleanup(self, plan):
        freed, protected, errors = 0, 0, 0
        for key, _ in plan['candidates']:
            if self.cache_stop.is_set():
                break
            with self.lock:
                if key in self.pins or key in self.settings.protected_archives or key in self.deleting:
                    protected += 1
                    continue
                if plan.get('automatic') and self.touches.get(key,0)>plan['planned_at']:
                    protected += 1
                    continue
                self.deleting.add(key)
            try:
                directory = self.path(key)
                children = self._safe_children(directory)
                for child in children:
                    # Recheck every path immediately before unlink, never follow a
                    # link, recurse, or remove an unknown file / user document.
                    if self.cache_stop.is_set():
                        raise PlaybackError('cache-maintenance-deferred')
                    if (linked(directory) or directory.resolve().parent != self.root or linked(child) or
                            not child.is_file() or child.resolve().parent != directory):
                        raise PlaybackError('cache-path')
                    size = child.stat().st_size
                    child.unlink()
                    freed += size
                if directory.exists():
                    directory.rmdir()
                with self.lock, self.db:
                    self.db.execute("UPDATE recordings SET state='expired',media=NULL WHERE key=?", (key,))
                    self.db.execute('DELETE FROM workspaces WHERE key=?', (key,))
            except (OSError, PlaybackError):
                errors += 1  # Windows sharing violations are deferred, never killed.
                try:
                    directory = self.path(key)
                    state = 'downloaded' if (directory/'original.bin').is_file() else 'failed'
                    self.state(key, state)
                except (OSError, PlaybackError):
                    pass
            finally:
                with self.lock:
                    self.deleting.discard(key)
        self.last_cleanup = time.strftime('%Y-%m-%d %H:%M:%S')
        self.last_result = dict(kind='result',freed=freed,protected=protected,errors=errors)
        with self.lock,self.db:
            self.db.execute('INSERT OR REPLACE INTO maintenance VALUES(1,?,?)',
                            (self.last_cleanup,json.dumps(self.last_result)))
        self.cache_revision += 1
        if not self.cache_stop.is_set():
            self._inventory()
        return self.last_result

    def ensure_space(self, reserve=0):
        with self.allocation_lock:
            # Reconcile written bytes with each owner's remaining reservation;
            # the written bytes are already part of the physical inventory.
            with self.lock:
                reservations = tuple(self.reservations.values())
            for item in reservations:
                item.check(force=True)
            plan = self.plan_cleanup(automatic=True, reserve=reserve)
            if plan['candidates']:
                self.execute_cleanup(plan)
            snapshot = self.cache_snapshot
            if (snapshot['media']+snapshot['reserved']+reserve > snapshot['limit'] or
                    snapshot['free']-snapshot['reserved']-reserve < self.settings.free_gib*GIB):
                raise PlaybackError('cache-full')

    def request_cleanup(self, camera=None, plan=None):
        with self.lock:
            if self.cache_action is not None:
                return
            self.cache_result = None
            self.cache_action = ('execute', plan) if plan is not None else ('plan', camera)
        self.cache_wake.set()

    def _maintenance(self):
        last = 0.
        while not self.cache_stop.is_set():
            try:
                self.flush_touches()
                with self.lock:
                    action, self.cache_action = self.cache_action, None
                if action:
                    self.cache_result = self.execute_cleanup(action[1]) if action[0]=='execute' else self.plan_cleanup(action[1])
                elif time.monotonic()-last >= 300 or self.cache_wake.is_set():
                    with self.allocation_lock:
                        plan = self.plan_cleanup(automatic=True)
                        if plan['candidates']:
                            self.execute_cleanup(plan)
                    last = time.monotonic()
                # WAL is separate from the media budget. Passive checkpoint is
                # bounded by SQLite's busy timeout and does not block readers.
                with self.lock:
                    self.db.execute('PRAGMA wal_checkpoint(PASSIVE)')
                    # Bounded batches retire search freshness, not remote archive
                    # availability. Cached/remote recording rows are preserved.
                    self.db.execute('DELETE FROM searches WHERE rowid IN (SELECT rowid FROM searches WHERE checked<? LIMIT 500)',
                                    (time.time()-90*86400,))
                    self.db.commit()
            except (OSError, PlaybackError, sqlite3.Error):
                self.cache_result = dict(kind='error', reason='cache-maintenance-deferred')
            finally:
                self.cache_wake.clear()
            self.cache_wake.wait(30)
