"""Local video-window placement. Only the Tk adapter touches widgets.

SQLite and monitor enumeration run on background threads. A reset generation
prevents another player (or a closing one) from resurrecting an old position.
"""
from concurrent.futures import Future
from contextlib import closing
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import queue
import sqlite3
import threading
import time


def positions_path():
    return Path(os.environ.get('CAMERA_WINDOW_POSITIONS_FILE') or
                Path(__file__).resolve().with_name('camera_window_positions.db'))


@dataclass(frozen=True, order=True)
class Screen:
    name: str
    bounds: tuple
    work: tuple
    primary: bool = False


def screens_signature(screens):
    return json.dumps([(s.name, s.bounds, s.work, s.primary) for s in sorted(screens)],
                      separators=(',', ':'))


def windows_screens():
    """Work areas in virtual-desktop coordinates, including negative origins.

    Do not change DPI awareness: enumeration and Tk must use the same process
    coordinate space. No Tk or libVLC calls are made here.
    """
    if os.name != 'nt':
        return ()

    class MonitorInfo(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('monitor', wintypes.RECT),
                    ('work', wintypes.RECT), ('flags', wintypes.DWORD),
                    ('device', wintypes.WCHAR * 32)]

    user32 = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR,
                                      wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                                      wintypes.LPARAM)
    user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MonitorInfo)]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                                         callback_type, wintypes.LPARAM]
    user32.EnumDisplayMonitors.restype = wintypes.BOOL
    screens = []

    def collect(handle, _dc, _rect, _data):
        info = MonitorInfo()
        info.size = ctypes.sizeof(info)
        if not user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            return False
        rect = lambda r: (r.left, r.top, r.right, r.bottom)
        screens.append(Screen(info.device, rect(info.monitor), rect(info.work), bool(info.flags & 1)))
        return True

    if not user32.EnumDisplayMonitors(None, None, callback_type(collect), 0):
        raise OSError('Monitor enumeration unavailable')
    return tuple(sorted(screens))


def visible_geometry(camera_id, position, size, screens, decorations=(16, 39)):
    """Fit the whole decorated window to a real work area, never a desktop gap."""
    primary = next((s for s in screens if s.primary), screens[0])
    if position is None:
        offset = 24 + (int(hashlib.sha256(str(camera_id).encode()).hexdigest()[:8], 16) % 8) * 28
        x, y = primary.work[0] + offset, primary.work[1] + offset
        screen = primary
    else:
        x, y = position
        # Nearest work area, even if the saved point is completely off screen.
        def distance(s):
            l, t, r, b = s.work
            return max(l-x, 0, x-(r-1))**2 + max(t-y, 0, y-(b-1))**2
        screen = min(screens, key=distance)
    left, top, right, bottom = screen.work
    width = max(1, min(size[0], right-left-decorations[0]))
    height = max(1, min(size[1], bottom-top-decorations[1]))
    x = max(left, min(x, right-width-decorations[0]))
    y = max(top, min(y, bottom-height-decorations[1]))
    return width, height, x, y


class PositionStore:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else positions_path()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=0.2)
        try:
            if connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                                  "AND name IN ('layout_state','window_positions')").fetchone()[0] == 2:
                return connection
            with connection:
                connection.execute('CREATE TABLE IF NOT EXISTS layout_state '
                                   '(id INTEGER PRIMARY KEY CHECK(id=1), generation INTEGER NOT NULL)')
                connection.execute('INSERT OR IGNORE INTO layout_state VALUES (1, 0)')
                connection.execute('CREATE TABLE IF NOT EXISTS window_positions '
                                   '(camera_id TEXT PRIMARY KEY, screens TEXT NOT NULL, '
                                   'x INTEGER NOT NULL, y INTEGER NOT NULL)')
        except Exception:
            connection.close()
            raise
        return connection

    def read(self, camera_id):
        with closing(self._connect()) as db:
            # Both reads belong to one snapshot, also while another process resets.
            db.execute('BEGIN')
            generation = db.execute('SELECT generation FROM layout_state WHERE id=1').fetchone()[0]
            row = db.execute('SELECT screens,x,y FROM window_positions WHERE camera_id=?',
                             (str(camera_id),)).fetchone()
            if row and not all(type(v) is int and abs(v) < 10_000_000 for v in row[1:]):
                row = None
            return generation, row

    def save(self, camera_id, generation, signature, x, y):
        with closing(self._connect()) as db, db:
            # Atomic compare-and-write: a pre-reset sample cannot reappear later.
            db.execute('INSERT OR REPLACE INTO window_positions '
                       'SELECT ?,?,?,? WHERE (SELECT generation FROM layout_state WHERE id=1)=?',
                       (str(camera_id), signature, x, y, generation))

    def reset(self):
        with closing(self._connect()) as db, db:
            db.execute('UPDATE layout_state SET generation=generation+1 WHERE id=1')
            db.execute('DELETE FROM window_positions')


def reset_positions(path=None):
    """Return a future; never wait for a database lock on Tk."""
    future = Future()

    def reset():
        try:
            PositionStore(path).reset()
            future.set_result(True)
        except (OSError, sqlite3.Error):
            future.set_result(False)

    threading.Thread(target=reset, name='Reset window positions', daemon=True).start()
    return future


class PlacementWorker:
    INTERVAL = 0.25

    def __init__(self, camera_id, fallback, *, path=None, screen_provider=windows_screens):
        self.camera_id = str(camera_id)
        self.fallback = fallback
        self.store = PositionStore(path)
        self.screen_provider = screen_provider
        self.updates = queue.SimpleQueue()
        self.closed = threading.Event()
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.pending = None
        self.stopping = False
        self.thread = threading.Thread(target=self._run, name='Window positions', daemon=True)
        self.thread.start()

    def save(self, token, position):
        with self.lock:
            self.pending = (token, position)

    def close(self):
        with self.lock:
            self.stopping = True
        self.wake.set()

    def _run(self):
        token = None
        screens = self.fallback
        try:
            while True:
                try:
                    screens = self.screen_provider() or screens
                except OSError:
                    pass  # Retain the last usable topology during a transient failure.
                signature = screens_signature(screens)
                try:
                    generation, row = self.store.read(self.camera_id)
                except (OSError, sqlite3.Error):
                    generation, row = (token[0] if token else None), None
                new_token = (generation, signature)
                if new_token != token:
                    position = row[1:] if token is None and row and row[0] == signature else None
                    token = new_token
                    self.updates.put((token, screens, position))
                with self.lock:
                    pending, self.pending = self.pending, None
                    stopping = self.stopping
                if pending and pending[0] == token and generation is not None:
                    try:
                        self.store.save(self.camera_id, generation, signature, *pending[1])
                    except (OSError, sqlite3.Error):
                        # Retain the latest sample for a later attempt; never block Tk.
                        with self.lock:
                            if self.pending is None:
                                self.pending = pending
                if stopping:
                    break
                self.wake.wait(self.INTERVAL)
                self.wake.clear()
        finally:
            self.closed.set()


class WindowPlacement:
    """Sample normal window coordinates on Tk, hand only values to the worker."""
    def __init__(self, root, camera_id, *, path=None, screen_provider=windows_screens):
        self.root = root
        self.camera_id = str(camera_id)
        fallback = (Screen('primary', (0, 0, root.winfo_screenwidth(), root.winfo_screenheight()),
                           (0, 0, root.winfo_screenwidth(), root.winfo_screenheight()), True),)
        self.worker = PlacementWorker(camera_id, fallback, path=path, screen_provider=screen_provider)
        self.token = None
        self.screens = fallback
        self.last = self._geometry()
        self.user_placed = False
        self.settling = False
        self.stopping = False
        self.close_deadline = None
        self.timer = root.after(50, self._poll)
        root.bind('<Destroy>', self._destroyed, add='+')

    def _geometry(self):
        return (self.root.winfo_width(), self.root.winfo_height(),
                self.root.winfo_x(), self.root.winfo_y())

    def _fit(self, position, size):
        border = max(0, self.root.winfo_rootx() - self.root.winfo_x())
        title = max(0, self.root.winfo_rooty() - self.root.winfo_y())
        return visible_geometry(self.camera_id, position, size, self.screens,
                                (border*2, title+border))

    def _place(self, position):
        geometry = self._fit(position, self._geometry()[:2])
        w, h, x, y = geometry
        # '+-1920' is an absolute negative origin. '-1920' anchors to the right.
        self.root.geometry(f'{w}x{h}+{x}+{y}')
        self.last = geometry
        self.settling = True

    def _sample(self):
        state = self.root.state()
        if state in ('zoomed', 'iconic'):
            self.user_placed = True
        if state != 'normal':
            return
        geometry = self._geometry()
        self.settling = False
        if geometry != self.last:
            # Observe Windows Snap / manual resize without rewriting geometry.
            # Even a no-op wm geometry call can undo the shell's snapped state.
            self.user_placed = True
            if self.token is not None and geometry[2:] != self.last[2:]:
                self.worker.save(self.token, geometry[2:])
            self.last = geometry

    def resize_for_video(self, width, height):
        """Initial aspect ratio is optional once Windows/the user chose a layout."""
        self._sample()  # Catch a Snap occurring between placement polls.
        if self.user_placed:
            return
        current = self._geometry()
        w, h, x, y = self._fit(current[2:], (width, height))
        value = f'{w}x{h}'
        if (x, y) != current[2:]:
            value += f'+{x}+{y}'
        self.root.geometry(value)
        self.last = (w, h, x, y)
        self.settling = True

    def _poll(self):
        self.timer = None
        update = None
        while not self.worker.updates.empty():
            update = self.worker.updates.get_nowait()
        if update is not None:
            previous = self.token
            if previous is None:
                self._sample()
            self.token, self.screens, position = update
            if previous is None and self.user_placed:
                # A slow initial disk/display query must not undo an early Snap.
                if self.root.state() == 'normal':
                    self.worker.save(self.token, self._geometry()[2:])
            else:
                if previous is not None and self.root.state() in ('iconic', 'zoomed'):
                    self.root.state('normal')
                self._place(position)
        else:
            self._sample()
        self.timer = self.root.after(100, self._poll)

    def close(self):
        if self.stopping:
            return
        self.stopping = True
        self._sample()
        if self.timer is not None:
            self.root.after_cancel(self.timer)
            self.timer = None
        self.close_deadline = time.monotonic() + 0.75
        self.worker.close()

    def finished(self):
        return self.worker.closed.is_set() or (self.close_deadline is not None and
                                               time.monotonic() >= self.close_deadline)

    def _destroyed(self, event):
        if event.widget is self.root:
            # Destruction can be requested externally; the widget is already gone.
            self.stopping = True
            if self.timer is not None:
                self.root.after_cancel(self.timer)
                self.timer = None
            self.worker.close()
