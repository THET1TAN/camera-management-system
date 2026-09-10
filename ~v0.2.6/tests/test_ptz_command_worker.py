"""Camera-state and delayed-network regressions without a physical camera."""
from datetime import timedelta
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from ptz_command_worker import ControlState, PTZCommandWorker, select_move_timeout


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class Camera:
    """The camera retains zero velocity components until an explicit Stop."""
    def __init__(self):
        self.motion = [0, 0, 0]
        self.focus = 0
        self.calls = []
        self.ptz = Mock()
        self.imaging = Mock()
        self.ptz.create_type.side_effect = lambda name: SimpleNamespace()
        self.ptz.ContinuousMove.side_effect = self.move
        self.ptz.Stop.side_effect = self.stop
        self.ptz.GotoPreset.side_effect = lambda request: self.calls.append(('preset', request['PresetToken']))
        self.imaging.Move.side_effect = self.move_focus
        self.imaging.Stop.side_effect = self.stop_focus

    def move(self, request):
        values = (request.Velocity['PanTilt']['x'], request.Velocity['PanTilt']['y'],
                  request.Velocity['Zoom']['x'])
        self.calls.append(('move', values))
        for index, value in enumerate(values):
            if value != 0:
                self.motion[index] = value

    def stop(self, request):
        self.calls.append(('stop', request['PanTilt'], request['Zoom']))
        if request['PanTilt']:
            self.motion[:2] = [0, 0]
        if request['Zoom']:
            self.motion[2] = 0

    def move_focus(self, request):
        self.focus = request['Focus']['Continuous']['Speed']
        self.calls.append(('focus', self.focus))

    def stop_focus(self, request):
        self.focus = 0
        self.calls.append(('focus_stop',))


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.camera = Camera()
        self.clock = FakeClock()
        self.worker = PTZCommandWorker(self.camera.ptz, self.camera.imaging,
                                        'profile', 'source', move_timeout=1, clock=self.clock)

    def drain(self):
        for _ in range(20):
            if not self.worker.step():
                return
        self.fail('Command loop failed to settle')

    def send(self, **values):
        self.worker.submit(ControlState(**values))
        self.drain()

    def test_diagonal_release_stops_group_then_resumes_each_remaining_axis(self):
        for pan in (-0.5, 0.5):
            for tilt in (-0.5, 0.5):
                for remaining in ({'pan': pan}, {'tilt': tilt}):
                    with self.subTest(pan=pan, tilt=tilt, remaining=remaining):
                        self.send()
                        self.send(pan=pan, tilt=tilt)
                        self.send(**remaining)
                        self.assertEqual(self.camera.motion, [remaining.get('pan', 0), remaining.get('tilt', 0), 0])
                        self.assertEqual(self.camera.calls[-2][0:3], ('stop', True, False))

    def test_zoom_release_does_not_stop_pan_tilt(self):
        self.send(pan=0.5, tilt=-0.5, zoom=0.5)
        self.send(pan=0.5, tilt=-0.5)
        self.assertEqual(self.camera.motion, [0.5, -0.5, 0])
        self.assertEqual(self.camera.calls[-1], ('stop', False, True))

    def test_direction_release_preserves_zoom_and_focus(self):
        self.send(pan=0.5, tilt=-0.5, zoom=0.5, focus=0.5)
        self.send(pan=0.5, zoom=0.5, focus=0.5)
        self.assertEqual(self.camera.motion, [0.5, 0, 0.5])
        self.assertEqual(self.camera.focus, 0.5)
        self.camera.imaging.Stop.assert_not_called()

    def test_final_release_sends_explicit_stop(self):
        self.send(pan=0.5, tilt=-0.5)
        self.send()
        self.assertEqual(self.camera.motion, [0, 0, 0])
        self.assertEqual(self.camera.calls[-1], ('stop', True, True))

    def test_rapid_unprocessed_changes_collapse_to_last_request(self):
        for state in [ControlState(pan=-0.5, tilt=-0.5), ControlState(tilt=0.5),
                      ControlState(pan=0.5, tilt=0.5), ControlState(pan=0.5)]:
            self.worker.submit(state)
        self.drain()
        self.assertEqual(self.camera.calls, [('move', (0.5, 0, 0))])

    def test_release_during_successful_stop_does_not_restart_old_direction(self):
        self.send(pan=0.5, tilt=-0.5)

        def stop_then_release(request):
            self.camera.stop(request)
            self.worker.submit(ControlState())

        self.camera.ptz.Stop.side_effect = stop_then_release
        self.send(pan=0.5)
        self.assertEqual(self.camera.motion, [0, 0, 0])
        self.camera.ptz.ContinuousMove.assert_called_once()

    def test_new_diagonal_during_stop_replaces_old_resume(self):
        self.send(pan=0.5, tilt=-0.5)

        def stop_then_change(request):
            self.camera.stop(request)
            self.worker.submit(ControlState(pan=-0.5, tilt=0.5))

        self.camera.ptz.Stop.side_effect = stop_then_change
        self.send(pan=0.5)
        self.assertEqual(self.camera.motion, [-0.5, 0.5, 0])
        self.assertEqual(self.camera.calls[-1], ('move', (-0.5, 0.5, 0)))
        self.assertNotIn(('move', (0.5, 0, 0)), self.camera.calls)

    def test_new_input_during_move_is_reconciled_immediately_after_response(self):
        def move_then_change(request):
            self.camera.move(request)
            if len(self.camera.calls) == 1:
                self.worker.submit(ControlState(pan=0.5))

        self.camera.ptz.ContinuousMove.side_effect = move_then_change
        self.send(pan=0.5, tilt=-0.5)
        self.assertEqual(self.camera.motion, [0.5, 0, 0])
        self.assertEqual(self.camera.calls, [('move', (0.5, -0.5, 0)),
                                           ('stop', True, False), ('move', (0.5, 0, 0))])

    def test_rapid_changes_after_network_error_do_not_replay_failed_move(self):
        self.camera.ptz.ContinuousMove.side_effect = RuntimeError('timeout')
        self.send(pan=0.5, tilt=-0.5)
        self.worker.submit(ControlState(pan=-0.5, tilt=0.5))
        self.worker.submit(ControlState(tilt=0.5))
        self.clock.now += 0.21
        self.camera.ptz.ContinuousMove.side_effect = self.camera.move
        self.drain()
        self.assertEqual(self.camera.calls, [('stop', True, True), ('move', (0, 0.5, 0))])

    def test_failed_targeted_stop_blocks_resume_and_preserves_zoom(self):
        self.send(pan=0.5, tilt=-0.5, zoom=0.5)
        self.camera.ptz.Stop.side_effect = RuntimeError('timeout')
        self.send(pan=0.5, zoom=0.5)
        self.camera.ptz.ContinuousMove.assert_called_once()
        self.assertEqual(self.worker.motion[2], 0.5)
        self.clock.now += 0.21
        self.camera.ptz.Stop.side_effect = self.camera.stop
        self.drain()
        self.assertEqual(self.camera.motion, [0.5, 0, 0.5])

    def test_all_released_after_failed_stop_never_resume(self):
        self.send(pan=0.5, tilt=-0.5)
        self.camera.ptz.Stop.side_effect = RuntimeError('timeout')
        self.send(pan=0.5)
        self.worker.submit(ControlState())
        self.clock.now += 0.21
        self.camera.ptz.Stop.side_effect = self.camera.stop
        self.drain()
        self.camera.ptz.ContinuousMove.assert_called_once()
        self.assertEqual(self.camera.motion, [0, 0, 0])

    def test_move_requests_have_negotiated_timeout(self):
        self.send(pan=0.5)
        request = self.camera.ptz.ContinuousMove.call_args.args[0]
        self.assertEqual(request.Timeout, timedelta(seconds=1))

    def test_held_direction_renews_only_when_due(self):
        self.send(pan=0.5)
        self.clock.now = 0.2
        self.send(pan=0.5)
        self.camera.ptz.ContinuousMove.assert_called_once()
        self.clock.now = 0.4
        self.send(pan=0.5)
        self.assertEqual(self.camera.ptz.ContinuousMove.call_count, 2)
        self.camera.ptz.Stop.assert_not_called()

    def test_renewal_uses_latest_direction(self):
        self.send(pan=0.5, tilt=-0.5)
        self.clock.now = 0.4
        self.send(tilt=-0.5)
        self.assertEqual(self.camera.calls[-1], ('move', (0, -0.5, 0)))

    def test_stalled_input_heartbeat_stops_motion_and_focus(self):
        self.send(pan=0.5, tilt=-0.5, focus=0.5)
        self.clock.now = 0.51
        self.drain()
        self.assertEqual(self.camera.motion, [0, 0, 0])
        self.assertEqual(self.camera.focus, 0)

    def test_speed_change_does_not_stop_direction(self):
        self.send(pan=0.5, tilt=-0.5)
        self.send(pan=0.6, tilt=-0.6)
        self.camera.ptz.Stop.assert_not_called()
        self.assertEqual(self.camera.motion, [0.6, -0.6, 0])

    def test_reversal_stops_old_direction(self):
        self.send(pan=0.5)
        self.send(pan=-0.5)
        self.assertEqual(self.camera.calls[-2:], [('stop', True, False), ('move', (-0.5, 0, 0))])

    def test_halt_during_move_cancels_every_pending_control(self):
        def move_then_halt(request):
            self.camera.move(request)
            self.worker.halt()

        self.camera.ptz.ContinuousMove.side_effect = move_then_halt
        self.send(pan=0.5, tilt=-0.5, focus=0.5)
        self.assertEqual(self.camera.motion, [0, 0, 0])
        self.assertEqual(self.camera.focus, 0)
        self.camera.imaging.Move.assert_not_called()

    def test_halt_stops_before_new_direction_even_if_it_arrives_immediately(self):
        self.send(pan=0.5)
        self.worker.halt()
        self.send(pan=0.5)
        self.assertIn(('stop', True, True), self.camera.calls)
        self.assertEqual(self.camera.calls[-1], ('move', (0.5, 0, 0)))

    def test_focus_release_is_not_starved_by_ptz_changes(self):
        self.send(focus=0.5)
        self.worker.submit(ControlState(pan=0.5))
        self.worker.step()
        self.assertEqual(self.camera.calls[-1], ('focus_stop',))

    def test_focus_failure_does_not_prevent_ptz_stop(self):
        self.send(pan=0.5, focus=0.5)
        self.camera.imaging.Stop.side_effect = RuntimeError('timeout')
        self.worker.halt()
        self.drain()
        self.assertEqual(self.camera.motion, [0, 0, 0])

    def test_preset_runs_once_and_new_manual_input_cancels_it(self):
        self.send(preset=(1, 'preset1'))
        self.send(preset=(1, 'preset1'))
        self.camera.ptz.GotoPreset.assert_called_once()
        self.send(pan=0.5)
        self.assertEqual(self.camera.calls[-2:], [('stop', True, True), ('move', (0.5, 0, 0))])

    def test_halt_cancels_pending_preset(self):
        self.worker.submit(ControlState(preset=(1, 'preset1')))
        self.worker.halt()
        self.drain()
        self.camera.ptz.GotoPreset.assert_not_called()

    def test_close_rejects_new_inputs_and_stops(self):
        self.send(pan=0.5)
        self.worker.close()
        self.worker.submit(ControlState(pan=-0.5, tilt=0.5))
        self.drain()
        self.assertEqual(self.camera.motion, [0, 0, 0])
        self.camera.ptz.ContinuousMove.assert_called_once()


class ThreadTests(unittest.TestCase):
    def test_input_remains_responsive_during_slow_stop_and_no_obsolete_resume(self):
        camera = Camera()
        worker = PTZCommandWorker(camera.ptz, camera.imaging, 'profile', 'source', move_timeout=1)
        started = threading.Event()
        stopping = threading.Event()
        unblock = threading.Event()
        finished = threading.Event()
        camera.ptz.ContinuousMove.side_effect = lambda request: (camera.move(request), started.set())

        def slow_stop(request):
            stopping.set()
            if not unblock.wait(2):
                raise RuntimeError('test never released network')
            camera.stop(request)
            finished.set()

        camera.ptz.Stop.side_effect = slow_stop
        worker.start()
        try:
            worker.submit(ControlState(pan=0.5, tilt=-0.5))
            self.assertTrue(started.wait(1))
            worker.submit(ControlState(pan=0.5))
            self.assertTrue(stopping.wait(1))
            # If submit held a network lock this thread would deadlock here.
            for _ in range(100):
                worker.submit(ControlState(pan=-0.5, tilt=0.5))
                worker.submit(ControlState(pan=0.5, tilt=-0.5))
            worker.submit(ControlState())
            unblock.set()
            self.assertTrue(finished.wait(1))
        finally:
            unblock.set()
            worker.close()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        camera.ptz.ContinuousMove.assert_called_once()
        self.assertEqual(camera.motion, [0, 0, 0])


class TimeoutTests(unittest.TestCase):
    def test_duration_is_clamped_to_advertised_range(self):
        for minimum, maximum, expected in [(0, 10, 1), (0.1, 10, 1), (2, 10, 2), (0.1, 0.4, 0.4)]:
            service = Mock()
            service.GetConfigurationOptions.return_value = SimpleNamespace(PTZTimeout=SimpleNamespace(
                Min=timedelta(seconds=minimum), Max=timedelta(seconds=maximum)))
            profile = SimpleNamespace(PTZConfiguration=SimpleNamespace(token='configuration'))
            self.assertEqual(select_move_timeout(service, profile), expected)

    def test_failed_discovery_uses_declared_default(self):
        service = Mock()
        service.GetConfigurationOptions.side_effect = RuntimeError('unsupported')
        profile = SimpleNamespace(PTZConfiguration=SimpleNamespace(
            token='configuration', DefaultPTZTimeout=timedelta(seconds=10)))
        self.assertEqual(select_move_timeout(service, profile), 10)

    def test_missing_capabilities_do_not_invent_unsupported_timeout(self):
        self.assertIsNone(select_move_timeout(Mock(), SimpleNamespace()))


if __name__ == '__main__':
    unittest.main()
