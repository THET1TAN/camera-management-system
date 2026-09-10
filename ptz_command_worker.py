"""Serialized ONVIF commands driven by the latest keyboard state, never a FIFO."""
from dataclasses import dataclass
from datetime import timedelta
import math
import threading
import time


@dataclass(frozen=True)
class ControlState:
    pan: float = 0
    tilt: float = 0
    zoom: float = 0
    focus: float = 0
    preset: object = None  # (request number, token); repeat presets remain distinct.

    @property
    def motion(self):
        return self.pan, self.tilt, self.zoom


def select_move_timeout(service, profile):
    """Choose a short duration inside the camera's advertised timeout range."""
    def seconds(value, allow_zero=False):
        try:
            result = float(value.total_seconds())
            return result if math.isfinite(result) and (result > 0 or (allow_zero and result == 0)) else None
        except (AttributeError, TypeError, ValueError):
            return None

    config = getattr(profile, 'PTZConfiguration', None)
    default = seconds(getattr(config, 'DefaultPTZTimeout', None))
    try:
        options = service.GetConfigurationOptions({'ConfigurationToken': config.token})
        minimum = seconds(options.PTZTimeout.Min, allow_zero=True)
        maximum = seconds(options.PTZTimeout.Max)
        if minimum is not None and maximum is not None and minimum <= maximum:
            return max(minimum, min(1.0, maximum))
    except Exception:
        pass
    return default  # None means keep the device default; no unsupported duration.


class PTZCommandWorker:
    INPUT_LEASE_SECONDS = 0.5
    RETRY_SECONDS = 0.2
    CLOSE_SECONDS = 6.0

    def __init__(self, ptz, imaging, profile_token, source_token,
                 move_timeout=None, clock=time.monotonic):
        self.ptz = ptz
        self.imaging = imaging
        self.profile_token = profile_token
        self.source_token = source_token
        self.move_timeout = move_timeout
        self.clock = clock
        self._condition = threading.Condition()
        self._desired = ControlState()
        self._last_input = clock()
        self._halt = 0
        self._ptz_halt_done = self._focus_halt_done = 0
        self._closing = False
        self._close_deadline = None
        self._thread = None
        self._status = 'PTZ request: idle'
        # Only the command thread (or deterministic step() tests) owns these.
        self.motion = (0, 0, 0)
        self.focus = 0
        self._preset_done = None
        self._preset_active = False
        self._renew_at = 0
        self._ptz_retry_at = self._focus_retry_at = 0

    @property
    def status(self):
        with self._condition:
            return self._status

    def _report(self, message):
        with self._condition:
            self._status = message

    def submit(self, state):
        with self._condition:
            if self._closing:
                return
            self._desired = state
            self._last_input = self.clock()
            self._condition.notify()

    def halt(self):
        with self._condition:
            self._desired = ControlState()
            self._halt += 1
            self._last_input = self.clock()
            self._condition.notify()

    def _snapshot(self):
        with self._condition:
            if self._closing or self.clock() - self._last_input >= self.INPUT_LEASE_SECONDS:
                return ControlState(), self._halt
            return self._desired, self._halt

    def start(self):
        if self._thread is not None:
            raise RuntimeError('PTZ worker already started')
        self._thread = threading.Thread(target=self._run, name='PTZ commands', daemon=True)
        self._thread.start()

    def close(self):
        with self._condition:
            if not self._closing:
                self._closing = True
                self._close_deadline = self.clock() + self.CLOSE_SECONDS
                self.halt()

    def is_alive(self):
        return self._thread is not None and self._thread.is_alive()

    def join(self, timeout=None):
        if self._thread is not None:
            self._thread.join(timeout)

    @staticmethod
    def _needs_stop(previous, requested):
        return previous is None or (previous != 0 and previous * requested <= 0)

    def _stop_ptz(self, pan_tilt, zoom, halt=None):
        try:
            self.ptz.Stop({'ProfileToken': self.profile_token,
                           'PanTilt': pan_tilt, 'Zoom': zoom})
        except Exception as error:
            self.motion = (None if pan_tilt else self.motion[0],
                           None if pan_tilt else self.motion[1],
                           None if zoom else self.motion[2])
            self._ptz_failed('Stop', error)
            return
        self.motion = (0 if pan_tilt else self.motion[0],
                       0 if pan_tilt else self.motion[1],
                       0 if zoom else self.motion[2])
        if pan_tilt:
            self._preset_active = False
        if halt is not None:
            self._ptz_halt_done = halt
        self._report('PTZ stop accepted: ' + ('pan/tilt + zoom' if pan_tilt and zoom
                                            else 'pan/tilt' if pan_tilt else 'zoom'))

    def _move(self, motion):
        try:
            request = self.ptz.create_type('ContinuousMove')
            request.ProfileToken = self.profile_token
            request.Velocity = {'PanTilt': {'x': motion[0], 'y': motion[1]},
                                'Zoom': {'x': motion[2]}}
            if self.move_timeout is not None:
                request.Timeout = timedelta(seconds=self.move_timeout)
            self.ptz.ContinuousMove(request)
        except Exception as error:
            self.motion = (None, None, None)
            self._ptz_failed('ContinuousMove', error)
            return
        self.motion = motion
        # Renewal is never a queued copy; the next step reads the latest state.
        self._renew_at = self.clock() + (self.move_timeout / 3 if self.move_timeout else 0.25)
        self._report('PTZ request accepted: pan {:+.1f}, tilt {:+.1f}, zoom {:+.1f}'.format(*motion))

    def _ptz_failed(self, operation, error):
        self._ptz_retry_at = self.clock() + self.RETRY_SECONDS
        self._report(f'PTZ {operation} failed; retry pending ({type(error).__name__})')

    def _focus_step(self, desired, halt):
        force_stop = self._focus_halt_done != halt
        if self.clock() < self._focus_retry_at:
            return False
        if not force_stop and self.focus == desired.focus:
            return False
        try:
            if force_stop or desired.focus == 0 or self.focus is None:
                self.imaging.Stop({'VideoSourceToken': self.source_token})
                self.focus = 0
                self._focus_halt_done = halt
            else:
                self.imaging.Move({'VideoSourceToken': self.source_token,
                                   'Focus': {'Continuous': {'Speed': desired.focus}}})
                self.focus = desired.focus
        except Exception as error:
            self.focus = None
            self._focus_retry_at = self.clock() + self.RETRY_SECONDS
            self._report(f'Focus request failed; retry pending ({type(error).__name__})')
        return True

    def _ptz_step(self, desired, halt):
        if self.clock() < self._ptz_retry_at:
            return False
        if self._ptz_halt_done != halt:
            self._stop_ptz(True, True, halt)
            return True
        if self._preset_active and desired.preset != self._preset_done:
            self._stop_ptz(True, True)
            return True
        if desired.preset is not None:
            if self.motion != (0, 0, 0):
                self._stop_ptz(True, True)
                return True
            if self.focus != 0 or self._preset_done == desired.preset:
                return False
            try:
                self.ptz.GotoPreset({'ProfileToken': self.profile_token,
                                     'PresetToken': desired.preset[1],
                                     'Speed': {'PanTilt': {'x': 1.0, 'y': 1.0}, 'Zoom': {'x': 1.0}}})
                self._preset_done = desired.preset
                self._preset_active = True
                self._report('PTZ preset request accepted')
            except Exception as error:
                # A lost response must still be followed by Stop if superseded.
                self._preset_active = True
                self._ptz_failed('GotoPreset', error)
            return True
        if desired.motion == (0, 0, 0):
            if self.motion != (0, 0, 0):
                self._stop_ptz(True, True)
                return True
            return False
        stop_pt = any(self._needs_stop(old, new)
                      for old, new in zip(self.motion[:2], desired.motion[:2]))
        stop_zoom = self._needs_stop(self.motion[2], desired.zoom)
        if stop_pt or stop_zoom:
            self._stop_ptz(stop_pt, stop_zoom)
            return True
        if self.motion != desired.motion or self.clock() >= self._renew_at:
            self._move(desired.motion)
            return True
        return False

    def step(self):
        """At most one network request; re-read input before the next request."""
        desired, halt = self._snapshot()
        # A released focus must not wait behind a stream of PTZ changes.
        if (desired.focus == 0 or self._focus_halt_done != halt) and self._focus_step(desired, halt):
            return True
        if self._ptz_step(desired, halt):
            return True
        return self._focus_step(*self._snapshot())

    def _run(self):
        while True:
            with self._condition:
                if self._closing:
                    if self._ptz_halt_done == self._halt and self._focus_halt_done == self._halt:
                        return
                    if self.clock() >= self._close_deadline:
                        self._report('Shutdown: camera stop not confirmed')
                        return
            if not self.step():
                with self._condition:
                    self._condition.wait(timeout=0.03)
