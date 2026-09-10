"""Camera-state and delayed-network regressions without a physical camera."""
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from ptz_command_worker import ControlState, PTZCommandWorker, select_move_timeout
from ptz_diagnostics import PTZDiagnostics


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class Camera:
    """Simulate standard ONVIF and devices that replace omitted movement groups."""
    def __init__(self):
        self.motion = [0, 0, 0]
        self.replaces_omitted_groups = False
        self.retains_zero = True
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
        velocity = dict(request.Velocity)
        if self.replaces_omitted_groups:
            self.motion[:] = [0, 0, 0]
        pan_tilt = velocity.get('PanTilt', {})
        values = (pan_tilt.get('x'), pan_tilt.get('y'), velocity.get('Zoom', {}).get('x'))
        self.calls.append(('move', values))
        for index, value in enumerate(values):
            if value is not None and (value != 0 or not self.retains_zero):
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
                                        'profile', 'source', move_timeout=1, clock=self.clock,
                                        conservative_stops=True)

    def drain(self):
        for _ in range(20):
            if not self.worker.step():
                return
        self.fail('Command loop failed to settle')

    def send(self, **values):
        self.worker.submit(ControlState(**values))
        self.drain()

    def test_diagonal_and_zoom_on_camera_that_replaces_omitted_groups(self):
        self.camera.replaces_omitted_groups = True
        for pan in (-0.5, 0.5):
            for tilt in (-0.5, 0.5):
                for zoom in (-0.5, 0.5):
                    with self.subTest(pan=pan, tilt=tilt, zoom=zoom):
                        self.send()
                        self.send(pan=pan, tilt=tilt, zoom=zoom)
                        self.assertEqual(self.camera.motion, [pan, tilt, zoom])

    def test_starting_zoom_transmits_the_held_diagonal_in_the_same_request(self):
        self.send(pan=0.5, tilt=-0.5)
        self.send(pan=0.5, tilt=-0.5, zoom=0.5)
        request = self.camera.ptz.ContinuousMove.call_args.args[0]
        self.assertEqual(request.Velocity, {'PanTilt': {'x': 0.5, 'y': -0.5}, 'Zoom': {'x': 0.5}})
        self.assertEqual(self.camera.motion, [0.5, -0.5, 0.5])
        self.camera.ptz.Stop.assert_not_called()

    def test_standard_camera_keeps_omitted_groups_and_stops_released_controls(self):
        self.camera.retains_zero = False
        for pan in (-0.5, 0.5):
            for tilt in (-0.5, 0.5):
                for zoom in (-0.5, 0.5):
                    with self.subTest(pan=pan, tilt=tilt, zoom=zoom):
                        self.send(pan=pan, tilt=tilt, zoom=zoom)
                        self.assertEqual(self.camera.motion, [pan, tilt, zoom])
                        self.send(pan=pan, zoom=zoom)
                        self.assertEqual(self.camera.motion, [pan, 0, zoom])
                        self.send(zoom=zoom)
                        self.assertEqual(self.camera.motion, [0, 0, zoom])
                        self.send()
                        self.assertEqual(self.camera.motion, [0, 0, 0])

    def test_starting_diagonal_transmits_held_zoom_in_the_same_request(self):
        self.send(zoom=-0.5)
        self.send(pan=-0.5, tilt=0.5, zoom=-0.5)
        request = self.camera.ptz.ContinuousMove.call_args.args[0]
        self.assertEqual(request.Velocity, {'PanTilt': {'x': -0.5, 'y': 0.5}, 'Zoom': {'x': -0.5}})
        self.assertEqual(self.camera.motion, [-0.5, 0.5, -0.5])
        self.camera.ptz.Stop.assert_not_called()

    def test_renewals_keep_all_held_controls_together_without_alternation(self):
        self.camera.replaces_omitted_groups = True
        self.send(pan=0.5, tilt=0.5)
        self.clock.now = 0.2
        self.send(pan=0.5, tilt=0.5, zoom=0.5)
        self.clock.now = 0.4
        self.send(pan=0.5, tilt=0.5, zoom=0.5)
        self.assertEqual(self.camera.calls[-1], ('move', (0.5, 0.5, 0.5)))
        self.clock.now = 0.6
        self.send(pan=0.5, tilt=0.5, zoom=0.5)
        self.assertEqual(self.camera.calls[-1], ('move', (0.5, 0.5, 0.5)))
        self.assertEqual(self.camera.ptz.ContinuousMove.call_count, 3)
        self.assertEqual(self.camera.motion, [0.5, 0.5, 0.5])

    def test_slow_replies_do_not_starve_either_active_group(self):
        state = ControlState(pan=0.5, tilt=-0.5, zoom=0.5)

        def slow_move(request):
            self.camera.move(request)
            self.clock.now += 0.4  # Longer than the renewal interval.
            self.worker.submit(state)  # Tk still publishes the held keys.

        self.camera.ptz.ContinuousMove.side_effect = slow_move
        self.worker.submit(state)
        for _ in range(6):
            self.worker.step()
            self.clock.now += 0.4
            self.worker.submit(state)
        self.assertEqual(self.camera.calls, [('move', (0.5, -0.5, 0.5))] * 6)
        self.assertEqual(self.camera.motion, [0.5, -0.5, 0.5])
        self.worker.submit(ControlState())
        self.drain()
        self.assertEqual(self.camera.motion, [0, 0, 0])

    def test_zoom_released_during_combined_reply_stops_without_replaying_it(self):
        def move_then_release_zoom(request):
            self.camera.move(request)
            self.worker.submit(ControlState(pan=0.5, tilt=-0.5))

        self.camera.ptz.ContinuousMove.side_effect = move_then_release_zoom
        self.send(pan=0.5, tilt=-0.5, zoom=0.5)
        self.assertEqual(self.camera.motion, [0.5, -0.5, 0])
        self.camera.ptz.ContinuousMove.assert_called_once()
        self.assertEqual(self.camera.calls[-1], ('stop', False, True))

    def test_zoom_reversal_preserves_diagonal(self):
        self.send(pan=0.5, tilt=-0.5, zoom=0.5)
        self.send(pan=0.5, tilt=-0.5, zoom=-0.5)
        self.assertEqual(self.camera.calls[-2:], [
            ('stop', False, True), ('move', (0.5, -0.5, -0.5))])
        self.assertEqual(self.camera.motion, [0.5, -0.5, -0.5])

    def test_failed_zoom_request_does_not_mark_pan_tilt_unknown(self):
        self.camera.ptz.ContinuousMove.side_effect = RuntimeError('timeout')
        self.send(zoom=0.5)
        self.assertEqual(self.worker.motion, (0, 0, None))
        self.worker.submit(ControlState())
        self.clock.now += 0.21
        self.camera.ptz.ContinuousMove.side_effect = self.camera.move
        self.drain()
        self.assertEqual(self.camera.calls[-1], ('stop', True, True))
        self.assertEqual(self.camera.motion, [0, 0, 0])

    def test_failed_combined_request_stops_all_groups_before_resuming_latest(self):
        self.camera.ptz.ContinuousMove.side_effect = RuntimeError('timeout')
        self.send(pan=0.5, tilt=-0.5, zoom=0.5)
        self.assertEqual(self.worker.motion, (None, None, None))
        self.worker.submit(ControlState(pan=0.5))
        self.clock.now += 0.21
        self.camera.ptz.ContinuousMove.side_effect = self.camera.move
        self.drain()
        self.assertEqual(self.camera.calls, [
            ('stop', True, True), ('move', (0.5, 0, None))])
        self.assertEqual(self.camera.motion, [0.5, 0, 0])

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
        self.assertEqual(self.camera.calls, [('move', (0.5, 0, None))])

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
        self.assertEqual(self.camera.calls[-1], ('move', (-0.5, 0.5, None)))
        self.assertNotIn(('move', (0.5, 0, None)), self.camera.calls)

    def test_new_input_during_move_is_reconciled_immediately_after_response(self):
        def move_then_change(request):
            self.camera.move(request)
            if len(self.camera.calls) == 1:
                self.worker.submit(ControlState(pan=0.5))

        self.camera.ptz.ContinuousMove.side_effect = move_then_change
        self.send(pan=0.5, tilt=-0.5)
        self.assertEqual(self.camera.motion, [0.5, 0, 0])
        self.assertEqual(self.camera.calls, [('move', (0.5, -0.5, None)),
                                           ('stop', True, False), ('move', (0.5, 0, None))])

    def test_rapid_changes_after_network_error_do_not_replay_failed_move(self):
        self.camera.ptz.ContinuousMove.side_effect = RuntimeError('timeout')
        self.send(pan=0.5, tilt=-0.5)
        self.worker.submit(ControlState(pan=-0.5, tilt=0.5))
        self.worker.submit(ControlState(tilt=0.5))
        self.clock.now += 0.21
        self.camera.ptz.ContinuousMove.side_effect = self.camera.move
        self.drain()
        self.assertEqual(self.camera.calls, [('stop', True, False), ('move', (0, 0.5, None))])

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
        self.assertEqual(self.camera.calls[-1], ('move', (0, -0.5, None)))

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
        self.assertEqual(self.camera.calls[-2:], [('stop', True, False), ('move', (-0.5, 0, None))])

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
        self.assertEqual(self.camera.calls[-1], ('move', (0.5, 0, None)))

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
        self.assertEqual(self.camera.calls[-2:], [('stop', True, True), ('move', (0.5, 0, None))])

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


class SmoothWorkerTests(unittest.TestCase):
    def setUp(self):
        self.camera = Camera()
        self.camera.retains_zero = False
        self.clock = FakeClock()
        self.worker = PTZCommandWorker(self.camera.ptz, self.camera.imaging,
                                       'profile', 'source', move_timeout=1, clock=self.clock)

    def send(self, **values):
        self.worker.submit(ControlState(**values))
        for _ in range(20):
            if not self.worker.step():
                return
        self.fail('Command loop failed to settle')

    def test_diagonal_release_preserves_remaining_axis_without_stop(self):
        for pan in (-0.5, 0.5):
            for tilt in (-0.5, 0.5):
                for zoom in (-0.5, 0, 0.5):
                    for remaining in ({'pan': pan}, {'tilt': tilt}):
                        with self.subTest(pan=pan, tilt=tilt, zoom=zoom, remaining=remaining):
                            self.send(pan=pan, tilt=tilt, zoom=zoom)
                            before = len(self.camera.calls)
                            self.send(**remaining, zoom=zoom)
                            expected = (remaining.get('pan', 0), remaining.get('tilt', 0), zoom)
                            self.assertEqual(tuple(self.camera.motion), expected)
                            self.assertEqual(len(self.camera.calls[before:]), 1)
                            self.assertEqual(self.camera.calls[-1][0], 'move')
        self.camera.ptz.Stop.assert_not_called()

    def test_released_zoom_is_explicitly_zeroed_in_same_movement_request(self):
        self.send(pan=0.5, tilt=-0.5, zoom=0.5)
        self.send(pan=0.5, tilt=-0.5)
        self.assertEqual(self.camera.calls[-1], ('move', (0.5, -0.5, 0)))
        self.assertEqual(self.camera.motion, [0.5, -0.5, 0])
        self.camera.ptz.Stop.assert_not_called()

    def test_released_pan_tilt_is_zeroed_while_zoom_continues(self):
        self.send(pan=0.5, tilt=-0.5, zoom=-0.5)
        self.send(zoom=-0.5)
        self.assertEqual(self.camera.calls[-1], ('move', (0, 0, -0.5)))
        self.assertEqual(self.camera.motion, [0, 0, -0.5])
        self.camera.ptz.Stop.assert_not_called()

    def test_all_axes_can_reverse_without_intermediate_stop(self):
        self.send(pan=0.5, tilt=-0.5, zoom=0.5)
        self.send(pan=-0.5, tilt=0.5, zoom=-0.5)
        self.assertEqual(self.camera.motion, [-0.5, 0.5, -0.5])
        self.assertEqual(len(self.camera.calls), 2)
        self.camera.ptz.Stop.assert_not_called()

    def test_camera_replacing_omitted_groups_keeps_every_held_axis(self):
        self.camera.replaces_omitted_groups = True
        for state in [dict(pan=0.5, tilt=-0.5, zoom=0.5),
                      dict(pan=0.5, zoom=0.5), dict(zoom=0.5),
                      dict(tilt=-0.5, zoom=0.5), dict(tilt=-0.5)]:
            self.send(**state)
            self.assertEqual(self.camera.motion, [state.get(k, 0) for k in ('pan', 'tilt', 'zoom')])
        self.camera.ptz.Stop.assert_not_called()

    def test_rapid_changes_during_response_use_only_latest_complete_vector(self):
        def move_then_change(request):
            self.camera.move(request)
            if len(self.camera.calls) == 1:
                for _ in range(100):
                    self.worker.submit(ControlState(pan=-0.5, tilt=0.5, zoom=-0.5))
                    self.worker.submit(ControlState(pan=0.5, tilt=-0.5, zoom=0.5))
                self.worker.submit(ControlState(pan=0.5))
        self.camera.ptz.ContinuousMove.side_effect = move_then_change
        self.send(pan=0.5, tilt=-0.5, zoom=0.5)
        self.assertEqual(self.camera.calls, [('move', (0.5, -0.5, 0.5)), ('move', (0.5, 0, 0))])
        self.assertEqual(self.camera.motion, [0.5, 0, 0])

    def test_full_release_during_response_sends_stop_without_old_resume(self):
        def move_then_release(request):
            self.camera.move(request)
            self.worker.submit(ControlState())
        self.camera.ptz.ContinuousMove.side_effect = move_then_release
        self.send(pan=0.5, tilt=-0.5, zoom=0.5)
        self.assertEqual(self.camera.calls, [('move', (0.5, -0.5, 0.5)), ('stop', True, True)])
        self.assertEqual(self.camera.motion, [0, 0, 0])

    def test_explicit_zero_with_lost_reply_marks_all_sent_groups_unknown(self):
        self.send(pan=0.5, tilt=-0.5, zoom=0.5)
        self.camera.ptz.ContinuousMove.side_effect = RuntimeError('lost response')
        self.send(pan=0.5)
        self.assertEqual(self.worker.motion, (None, None, None))
        self.worker.submit(ControlState(tilt=0.5))
        self.camera.ptz.ContinuousMove.side_effect = self.camera.move
        self.clock.now += 0.21
        self.send(tilt=0.5)
        self.assertEqual(self.camera.calls[-2:], [('stop', True, True), ('move', (0, 0.5, None))])
        self.assertEqual(self.camera.motion, [0, 0.5, 0])

    def test_full_release_after_failed_transition_never_resumes(self):
        self.send(pan=0.5, tilt=-0.5, zoom=0.5)
        self.camera.ptz.ContinuousMove.side_effect = RuntimeError('lost response')
        self.send(pan=0.5)
        self.clock.now += 0.21
        self.send()
        self.assertEqual(self.camera.calls[-1], ('stop', True, True))
        self.assertEqual(self.camera.motion, [0, 0, 0])

    def test_input_expiry_still_stops_all_axes(self):
        self.send(pan=0.5, tilt=-0.5, zoom=0.5)
        self.clock.now += 0.51
        self.worker.step()
        self.assertEqual(self.camera.calls[-1], ('stop', True, True))
        self.assertEqual(self.camera.motion, [0, 0, 0])

    def test_focus_loss_and_close_still_force_full_stop(self):
        for action in (self.worker.halt, self.worker.close):
            self.send(pan=0.5, tilt=0.5, zoom=-0.5)
            action()
            for _ in range(4):
                self.worker.step()
            self.assertEqual(self.camera.motion, [0, 0, 0])
            self.assertEqual(self.camera.calls[-1], ('stop', True, True))

    def test_slow_responses_do_not_leave_gap_in_camera_timeout(self):
        state = ControlState(pan=0.5, tilt=-0.5, zoom=0.5)
        deadline = None
        expired = []

        def move_with_delayed_reply(request):
            nonlocal deadline
            self.camera.move(request)
            deadline = self.clock.now + request.Timeout.total_seconds()
            self.clock.now += 0.8  # Device already accepted the move, reply arrives late.
            self.worker.submit(state)  # Independent Tk heartbeat remains active.

        self.camera.ptz.ContinuousMove.side_effect = move_with_delayed_reply
        while self.clock.now < 6:
            self.worker.submit(state)
            if not self.worker.step():
                self.clock.now += 0.02
            if deadline is not None and self.clock.now >= deadline:
                expired.append(self.clock.now)
        self.assertFalse(expired, 'A response delay must not create a gap after the camera timeout')
        self.assertEqual(self.camera.motion, [0.5, -0.5, 0.5])
        self.send()
        self.assertEqual(self.camera.motion, [0, 0, 0])

    def test_new_input_replaces_overdue_renewal_after_slow_reply(self):
        def delayed_reply_then_change(request):
            self.camera.move(request)
            self.clock.now += 0.8
            self.worker.submit(ControlState(pan=0.5))
        self.camera.ptz.ContinuousMove.side_effect = delayed_reply_then_change
        self.worker.submit(ControlState(pan=0.5, tilt=-0.5, zoom=0.5))
        self.worker.step()
        self.worker.step()
        self.assertEqual(self.camera.calls, [('move', (0.5, -0.5, 0.5)), ('move', (0.5, 0, 0))])

    def test_due_renewals_do_not_starve_a_held_focus_key(self):
        state = ControlState(pan=0.5, tilt=-0.5, zoom=0.5, focus=0.5)
        def delayed_reply(request):
            self.camera.move(request)
            self.clock.now += 0.4
            self.worker.submit(state)
        self.camera.ptz.ContinuousMove.side_effect = delayed_reply
        self.worker.submit(state)
        self.worker.step()
        self.worker.step()
        self.assertEqual(self.camera.focus, 0.5)
        self.assertEqual(self.camera.calls[-1], ('focus', 0.5))
        self.assertEqual(self.camera.ptz.ContinuousMove.call_count, 1)


class ThreadTests(unittest.TestCase):
    def test_input_remains_responsive_during_slow_stop_and_no_obsolete_resume(self):
        camera = Camera()
        worker = PTZCommandWorker(camera.ptz, camera.imaging, 'profile', 'source', move_timeout=1,
                                  conservative_stops=True)
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


class DiagnosticsTests(unittest.TestCase):
    def test_trace_records_requested_axes_and_results_without_private_details(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'ptz.log'
            diagnostics = PTZDiagnostics(path)
            camera = Camera()
            worker = PTZCommandWorker(camera.ptz, camera.imaging, 'private-profile',
                                      'private-source', diagnostics=diagnostics)
            try:
                worker.submit(ControlState(pan=0.5, tilt=0.5, zoom=0.5))
                worker.step()
                camera.ptz.Stop.side_effect = RuntimeError('private-password private-address')
                worker.halt()
                worker.step()  # Focus halt.
                worker.step()  # PTZ halt fails; only the exception class is logged.
            finally:
                diagnostics.close()
            data = path.read_text()
            self.assertNotIn('private-', data)
            records = [json.loads(line) for line in data.splitlines()]
            move = next(record for record in records if record['event'] == 'move_send')
            self.assertEqual((move['pan'], move['tilt'], move['zoom']), (0.5, 0.5, 0.5))
            self.assertIn('move_accepted', [record['event'] for record in records])
            self.assertEqual(records[-1]['error_type'], 'RuntimeError')

    def test_trace_failure_cannot_prevent_camera_stop(self):
        camera = Camera()
        diagnostics = Mock()
        diagnostics.record.side_effect = OSError('disk full')
        worker = PTZCommandWorker(camera.ptz, camera.imaging, 'profile', 'source',
                                  diagnostics=diagnostics)
        worker.submit(ControlState(pan=0.5, tilt=0.5, zoom=0.5))
        worker.step()
        worker.submit(ControlState())
        worker.step()
        self.assertEqual(camera.motion, [0, 0, 0])


if __name__ == '__main__':
    unittest.main()
