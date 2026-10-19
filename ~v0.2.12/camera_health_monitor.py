"""A bounded probe pool isolated from Tk and forcibly stoppable during shutdown."""
from dataclasses import dataclass
import multiprocessing
from queue import Queue, Empty, Full
import threading
import time

from camera_health import CameraProbe, Cancelled, HealthSettings, HealthState, Observation


@dataclass(frozen=True)
class HealthUpdate:
    camera_id: int
    state: str
    detail: str
    checked_at: float
    changed_at: float


def run_monitor(targets, settings, stop, output, probe_factory=CameraProbe, clock=time.monotonic):
    """No Tk imports; only status messages cross back to the Viewer."""
    jobs = Queue(maxsize=settings.workers)
    completed = Queue(maxsize=settings.workers)
    probes = {t.camera_id: probe_factory(t, settings) for t in targets}
    states = {t.camera_id: HealthState(settings.failures) for t in targets}
    due = {t.camera_id: 0.0 for t in targets}
    changes = {}
    pending = set()

    def work():
        while not stop.is_set():
            try:
                camera_id = jobs.get(timeout=0.1)
            except Empty:
                continue
            try:
                result = probes[camera_id].check(stop)
            except Cancelled:
                return
            except Exception:
                result = Observation('unknown', 'Verification could not complete; check camera configuration.')
            if not stop.is_set():
                completed.put((camera_id, result))

    for index in range(min(settings.workers, len(targets))):
        threading.Thread(target=work, name=f'Camera health {index + 1}', daemon=True).start()
    while not stop.is_set():
        try:
            while True:
                camera_id, result = completed.get_nowait()
                pending.discard(camera_id)
                due[camera_id] = clock() + settings.interval
                previous = states[camera_id].current.state
                result = states[camera_id].apply(result)
                now = time.time()
                if previous != result.state or camera_id not in changes:
                    changes[camera_id] = now
                update = HealthUpdate(camera_id, result.state, result.detail, now, changes[camera_id])
                try:
                    output.put(update, timeout=0.1)
                except Full:
                    pass  # UI is unavailable; never let it stall the probe pool.
        except Empty:
            pass
        for camera_id in sorted(due, key=due.get):
            if len(pending) >= settings.workers:
                break
            if camera_id not in pending and due[camera_id] <= clock():
                jobs.put_nowait(camera_id)
                pending.add(camera_id)
        stop.wait(0.05)


def _entry(targets, settings, stop, output):
    # Do not wait for a queue feeder if the parent has already disappeared.
    output.cancel_join_thread()
    def watch_parent():
        parent = multiprocessing.parent_process()
        while not stop.wait(0.2):
            if parent is not None and not parent.is_alive():
                stop.set()
    threading.Thread(target=watch_parent, name='Health parent lifetime', daemon=True).start()
    run_monitor(targets, settings, stop, output)


class _Lifetime:
    """ChildProcesses' pipe contract, backed by a multiprocessing Event."""
    def __init__(self, owner):
        self.owner = owner
        self.closed = False

    def close(self):
        if not self.closed:
            self.closed = True
            self.owner.stop_event.set()
            self.owner.stop_deadline = time.monotonic() + 0.5


class HealthMonitor:
    def __init__(self, targets, settings=HealthSettings()):
        context = multiprocessing.get_context('spawn')
        self.stop_event = context.Event()
        self.output = context.Queue(maxsize=max(8, len(targets) * 2))
        self.process = context.Process(target=_entry, args=(targets, settings, self.stop_event, self.output),
                                       name='Camera availability', daemon=True)
        self.stdin = _Lifetime(self)
        self.stop_deadline = None
        self._released = False
        self.process.start()

    def updates(self):
        if self._released:
            return []
        items = []
        try:
            while True:
                items.append(self.output.get_nowait())
        except Empty:
            return items

    def poll(self):
        if self.process.is_alive():
            if self.stop_deadline is not None and time.monotonic() >= self.stop_deadline:
                self.process.terminate()
            return None
        self.process.join(timeout=0)
        if not self._released:
            self.output.close()
            self._released = True
        return self.process.exitcode

    def kill(self):
        if self.process.is_alive():
            self.process.kill()
