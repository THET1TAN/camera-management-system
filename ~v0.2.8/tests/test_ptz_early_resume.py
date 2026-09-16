"""Control response/execution order independently for the opt-in C experiment."""
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ptz_command_worker import ControlState
from ptz_early_resume import EarlyResumeWorker
from test_ptz_command_worker import Camera, FakeClock


class EarlyResumeTests(unittest.TestCase):
    def setUp(self):
        self.camera = Camera()
        self.camera.retains_zero = False
        self.clock = FakeClock()
        self.entered = threading.Event()
        self.reply = threading.Event()
        self.apply_late = False
        self.fail_neutral = False
        self.neutral = Mock()
        self.neutral.create_type.side_effect = lambda name: SimpleNamespace()
        self.neutral.ContinuousMove.side_effect = self.neutral_request
        self.worker = EarlyResumeWorker(self.camera.ptz, self.camera.imaging, 'p', 's',
                                        clock=self.clock, move_timeout=60, neutral_service=self.neutral)
        self.addCleanup(self.finish_helper)

    def neutral_request(self, request):
        if not self.apply_late:
            self.camera.move(request)
        self.entered.set()
        if not self.reply.wait(2):
            raise RuntimeError('Test did not release neutral reply')
        if self.apply_late:
            self.camera.move(request)
        if self.fail_neutral:
            raise RuntimeError('Lost neutral reply')

    def finish_helper(self):
        self.reply.set()
        pending = self.worker._pending_neutral
        if pending is not None:
            self.assertTrue(pending.done.wait(1))

    def begin(self):
        self.worker.submit(ControlState(.5, -.5, .5))
        self.worker.step()
        self.worker.submit(ControlState(.5, 0, .5))
        self.worker.step()
        self.assertTrue(self.entered.wait(1))

    def early(self, state=ControlState(.5, 0, .5)):
        self.clock.now = .061
        self.worker.submit(state)
        self.worker.step()

    def settle(self):
        self.finish_helper()
        for _ in range(12):
            if not self.worker.step():
                return
        self.fail('Worker did not settle')

    def test_resumes_held_axes_before_neutral_reply_then_reconciles(self):
        self.begin()
        self.assertEqual(self.camera.motion, [0, 0, 0])
        self.early()
        self.assertFalse(self.reply.is_set())
        self.assertEqual(self.camera.motion, [.5, 0, .5])
        self.settle()
        self.assertEqual(self.camera.motion, [.5, 0, .5])
        self.assertEqual(self.camera.calls[-2:], [('move', (.5, 0, .5)), ('move', (.5, 0, .5))])

    def test_late_neutral_execution_cannot_leave_held_axes_stopped(self):
        self.apply_late = True
        self.begin()
        self.early()
        self.finish_helper()  # camera neutralizes after the early resume
        self.assertEqual(self.camera.motion, [0, 0, 0])
        self.settle()
        self.assertEqual(self.camera.motion, [.5, 0, .5])

    def test_only_one_early_move_and_latest_rapid_change_wins(self):
        self.begin()
        self.early()
        before = len(self.camera.calls)
        for _ in range(100):
            self.worker.submit(ControlState(-.5, .5, -.5))
            self.worker.step()
            self.worker.submit(ControlState(0, .25, -.25))
            self.worker.step()
        self.assertEqual(len(self.camera.calls), before)
        self.neutral.ContinuousMove.assert_called_once()
        self.settle()
        self.assertEqual(self.camera.motion, [0, .25, -.25])

    def test_release_before_early_resume_never_sends_nonzero(self):
        self.begin()
        before = self.camera.ptz.ContinuousMove.call_count
        self.early(ControlState())
        self.settle()
        self.assertEqual(self.camera.ptz.ContinuousMove.call_count, before)
        self.assertEqual(self.camera.calls[-1], ('stop', True, True))

    def test_release_after_early_resume_and_late_neutral_ends_with_stop(self):
        self.apply_late = True
        self.begin()
        self.early()
        self.worker.submit(ControlState())
        self.settle()
        self.assertEqual(self.camera.calls[-1], ('stop', True, True))
        self.assertEqual(self.camera.motion, [0, 0, 0])

    def test_expired_input_never_resumes(self):
        self.begin()
        self.clock.now = .51
        self.worker.step()
        self.settle()
        self.assertEqual(self.camera.motion, [0, 0, 0])
        self.assertEqual(self.camera.calls[-1], ('stop', True, True))

    def test_halt_then_new_input_waits_for_stop_before_resume(self):
        self.begin()
        self.worker.halt()
        self.early(ControlState(tilt=.25))
        self.assertEqual(self.camera.ptz.ContinuousMove.call_count, 1)
        self.settle()
        self.assertEqual(self.camera.calls[-2:], [('stop', True, True), ('move', (0, .25, 0))])

    def test_close_during_neutral_never_resumes(self):
        self.begin()
        self.worker.close()
        self.early()
        self.settle()
        self.assertEqual(self.camera.motion, [0, 0, 0])
        self.assertEqual(self.camera.calls[-1], ('stop', True, True))

    def test_lost_neutral_reply_forces_stop_before_latest_resume(self):
        self.fail_neutral = True
        self.begin()
        self.early()
        self.settle()
        self.assertEqual(self.worker.motion, (None, None, None))
        self.clock.now += .21
        self.worker.submit(ControlState(tilt=.25))
        self.settle()
        self.assertEqual(self.camera.calls[-2:], [('stop', True, True), ('move', (0, .25, 0))])

    def test_lost_early_reply_forces_stop_even_if_neutral_succeeded(self):
        self.begin()
        self.camera.ptz.ContinuousMove.side_effect = RuntimeError('Lost resume reply')
        self.early()
        self.settle()
        self.assertEqual(self.worker.motion, (None, None, None))
        self.camera.ptz.ContinuousMove.side_effect = self.camera.move
        self.clock.now += .21
        self.worker.submit(ControlState(.25))
        self.settle()
        self.assertEqual(self.camera.calls[-2:], [('stop', True, True), ('move', (.25, 0, 0))])

    def test_quick_neutral_reply_uses_normal_resume_without_overlap(self):
        self.begin()
        self.settle()
        self.assertEqual(self.camera.motion, [.5, 0, .5])
        self.assertEqual(self.camera.ptz.ContinuousMove.call_count, 2)

    def test_no_resume_before_neutral_dispatch_or_delay(self):
        self.begin()
        pending = self.worker._pending_neutral
        pending.dispatched.clear()
        self.early()
        self.assertFalse(pending.early_sent)
        pending.dispatched.set()
        self.clock.now = .04
        self.worker.step()
        self.assertFalse(pending.early_sent)

    def test_preset_waits_until_transition_and_stop_complete(self):
        self.begin()
        self.early(ControlState(preset=(1, 'preset')))
        self.camera.ptz.GotoPreset.assert_not_called()
        self.settle()
        self.assertEqual(self.camera.calls[-2:], [('stop', True, True), ('preset', 'preset')])

    def test_shared_client_is_rejected(self):
        with self.assertRaises(ValueError):
            EarlyResumeWorker(self.camera.ptz, self.camera.imaging, 'p', 's',
                              neutral_service=self.camera.ptz)

    def test_helper_start_failure_recovers_with_stop(self):
        self.worker.submit(ControlState(.5, -.5, .5))
        self.worker.step()
        self.worker.submit(ControlState(.5, 0, .5))
        with patch('ptz_early_resume.threading.Thread.start', side_effect=RuntimeError('Unavailable')):
            self.worker.step()
        self.worker.step()
        self.assertEqual(self.worker.motion, (None, None, None))
        self.clock.now += .21
        self.settle()
        self.assertEqual(self.camera.calls[-2:], [('stop', True, True), ('move', (.5, 0, .5))])


if __name__ == '__main__':
    unittest.main()
