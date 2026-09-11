"""Opt-in experiment: one early resume while a neutral reply is pending.

An HTTP reply does not establish physical execution order. Always reconcile the
latest input after BOTH requests finish; never overlap two neutral transitions.
The helper owns an independent ONVIF client and only ever sends zero velocity.
"""
from dataclasses import dataclass, field
from datetime import timedelta
import threading

from ptz_command_worker import PTZCommandWorker


@dataclass
class PendingNeutral:
    started_at: float
    halt: int
    done: threading.Event = field(default_factory=threading.Event)
    dispatched: threading.Event = field(default_factory=threading.Event)
    error: object = None
    early_sent: bool = False
    early_failed: bool = False


class EarlyResumeWorker(PTZCommandWorker):
    early_resume_enabled = True
    EARLY_RESUME_SECONDS = 0.060

    def __init__(self, *args, neutral_service, **kwargs):
        kwargs['neutral_transitions'] = True
        super().__init__(*args, **kwargs)
        if neutral_service is self.ptz:
            raise ValueError('Early resume requires an independent neutral client')
        self.neutral_service = neutral_service
        self._pending_neutral = None

    def _move(self, motion):
        if motion != (0, 0, 0) or not self._neutral_pending:
            return super()._move(motion)
        # Only the owner thread changes worker state. The helper publishes its
        # result before setting done; it never resumes movement or sends Stop.
        pending = PendingNeutral(self.clock(), self._ptz_halt_done)
        self._pending_neutral = pending
        velocity = self.velocity_spaces.build(
            (0, 0, 0), stop_pan_tilt=self._direct_pan_tilt or self.motion[:2] != (0, 0),
            stop_zoom=self._direct_zoom or self.motion[2] != 0)

        def neutral_only():
            try:
                request = self.neutral_service.create_type('ContinuousMove')
                request.ProfileToken = self.profile_token
                request.Velocity = velocity
                if self.move_timeout is not None:
                    request.Timeout = timedelta(seconds=self.move_timeout)
                pending.started_at = self.clock()
                self._trace('early_neutral_send')
                pending.dispatched.set()
                self.neutral_service.ContinuousMove(request)
                self._trace('early_neutral_accepted',
                            response_seconds=round(self.clock() - pending.started_at, 4))
            except Exception as error:
                pending.error = error
                self._trace('early_neutral_failed', error_type=type(error).__name__)
            finally:
                pending.done.set()
                with self._condition:
                    self._condition.notify()

        try:
            threading.Thread(target=neutral_only, name='PTZ neutral only', daemon=True).start()
        except Exception as error:
            pending.error = error
            pending.done.set()

    def step(self):
        pending = self._pending_neutral
        if pending is None:
            return super().step()
        if pending.done.is_set():
            self._pending_neutral = None
            self._neutral_pending = True
            if pending.error is not None or pending.early_failed:
                # A lost reply means unknown camera state, even if the other
                # request succeeded. The ordinary recovery sends Stop first.
                self.motion = (None, None, None)
                if pending.error is not None:
                    self._ptz_failed('Early neutral', pending.error)
            else:
                # Either request may have taken effect last. Reassert the full
                # latest vector, or explicit Stop for release/expiry/halt/preset.
                # Zero here is a scheduling state, not physical confirmation.
                self.motion = (0, 0, 0)
                self._trace('early_reconcile', attempted_early_resume=pending.early_sent)
            return True

        desired, halt = self._snapshot()
        # Focus release can proceed on its own imaging service while neutral is
        # pending. No new PTZ/preset transition starts until this one settles.
        if (desired.focus == 0 or self._focus_halt_done != halt) and self._focus_step(desired, halt):
            return True
        if (pending.dispatched.is_set() and not pending.early_sent
                and self.clock() - pending.started_at >= self.EARLY_RESUME_SECONDS
                and desired.motion != (0, 0, 0) and desired.preset is None and halt == pending.halt):
            # Check again after processing input; a completed neutral needs no
            # speculative resume. At most one nonzero request overlaps it.
            if pending.done.is_set():
                return True
            pending.early_sent = True
            self._trace('early_resume_send', pan=desired.pan, tilt=desired.tilt, zoom=desired.zoom,
                        elapsed_seconds=round(self.clock() - pending.started_at, 4))
            super()._move(desired.motion)
            pending.early_failed = any(value is None for value in self.motion)
            return True
        return False
