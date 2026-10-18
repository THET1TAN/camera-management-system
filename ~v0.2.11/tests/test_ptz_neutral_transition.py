"""Regressions for the opt-in r9 B sequence; no claim of camera validation."""
import unittest

from ptz_command_worker import ControlState, PTZCommandWorker
from ptz_velocity import VelocitySpaces
from test_ptz_command_worker import Camera, FakeClock


class NeutralTransitionTests(unittest.TestCase):
    def setUp(self):
        self.camera = Camera()
        self.clock = FakeClock()
        self.worker = PTZCommandWorker(self.camera.ptz, self.camera.imaging, 'p', 's',
                                       move_timeout=60, clock=self.clock, neutral_transitions=True)
        self.camera.ptz.ContinuousMove.side_effect = self.accept_neutral

    def accept_neutral(self, request):
        # Emulate partial zeros being ignored but a fully neutral vector working.
        self.camera.move(request)
        pt = request.Velocity.get('PanTilt', {})
        z = request.Velocity.get('Zoom', {})
        if not any((pt.get('x', 0), pt.get('y', 0), z.get('x', 0))):
            self.camera.motion[:] = [0, 0, 0]

    def send(self, state):
        self.worker.submit(state)
        for _ in range(10):
            if not self.worker.step():
                return
        self.fail('Worker did not settle')

    def test_each_diagonal_release_with_both_zoom_signs_reapplies_all_held_axes(self):
        for pan in (-.5, .5):
            for tilt in (-.5, .5):
                for zoom in (-.5, 0, .5):
                    for remaining in (ControlState(pan=pan, zoom=zoom), ControlState(tilt=tilt, zoom=zoom)):
                        self.send(ControlState(pan, tilt, zoom))
                        before = len(self.camera.calls)
                        self.send(remaining)
                        self.assertEqual(self.camera.motion, list(remaining.motion))
                        self.assertEqual([c[0] for c in self.camera.calls[before:]], ['move', 'move'])
                        self.send(ControlState())

    def test_zoom_alone_is_reapplied_after_releasing_pan_tilt(self):
        self.send(ControlState(.5, -.5, .5))
        self.send(ControlState(zoom=.5))
        self.assertEqual(self.camera.calls[-2:], [('move', (0, 0, 0)), ('move', (0, 0, .5))])
        self.assertEqual(self.camera.motion, [0, 0, .5])

    def test_latest_input_during_neutral_reply_wins(self):
        self.send(ControlState(.5, -.5, .5))
        def delayed(request):
            self.accept_neutral(request)
            if self.camera.motion == [0, 0, 0]:
                for _ in range(100):
                    self.worker.submit(ControlState(.5, -.5, .5))
                    self.worker.submit(ControlState(-.25, 0, -.25))
        self.camera.ptz.ContinuousMove.side_effect = delayed
        self.send(ControlState(pan=.5, zoom=.5))
        self.assertEqual(self.camera.calls[-1], ('move', (-.25, 0, -.25)))
        self.assertEqual(self.camera.motion, [-.25, 0, -.25])

    def test_release_lease_focus_loss_and_close_during_neutral_reply_send_explicit_stop(self):
        for action in ('release', 'expire', 'halt', 'close'):
            with self.subTest(action=action):
                self.setUp()
                self.send(ControlState(.5, -.5, .5))
                def delayed(request):
                    self.accept_neutral(request)
                    if action == 'release':
                        self.worker.submit(ControlState())
                    elif action == 'expire':
                        self.clock.now += .51
                    else:
                        getattr(self.worker, action)()
                self.camera.ptz.ContinuousMove.side_effect = delayed
                self.send(ControlState(pan=.5))
                self.assertEqual(self.camera.calls[-1], ('stop', True, True))
                self.assertEqual(self.camera.motion, [0, 0, 0])

    def test_lost_neutral_reply_requires_stop_before_latest_resume(self):
        self.send(ControlState(.5, -.5, .5))
        def lost(request):
            self.accept_neutral(request)
            raise RuntimeError('lost reply')
        self.camera.ptz.ContinuousMove.side_effect = lost
        self.send(ControlState(pan=.5))
        self.assertEqual(self.worker.motion, (None, None, None))
        self.camera.ptz.ContinuousMove.side_effect = self.accept_neutral
        self.clock.now += .21
        self.send(ControlState(tilt=.25))
        self.assertEqual(self.camera.calls[-2:], [('stop', True, True), ('move', (0, .25, 0))])

    def test_held_zoom_is_not_retriggered_every_quarter_second(self):
        state = ControlState(.5, -.5, .5)
        self.send(state)
        for _ in range(24):
            self.clock.now += .25
            self.send(state)
        self.camera.ptz.ContinuousMove.assert_called_once()
        self.clock.now = 20
        self.send(state)
        self.assertEqual(self.camera.ptz.ContinuousMove.call_count, 2)

    def test_analog_speed_updates_and_new_axis_do_not_insert_neutral(self):
        for value in (.1, .2, .4, .3, .15):
            self.send(ControlState(value, -value, value))
        self.assertEqual(len(self.camera.calls), 5)
        self.assertTrue(all(c[1] != (0, 0, 0) for c in self.camera.calls))
        self.camera.ptz.Stop.assert_not_called()

    def test_neutral_does_not_invent_unadvertised_unused_zoom(self):
        self.worker.velocity_spaces = VelocitySpaces(('pt', (-1, 1), (-1, 1)))
        self.send(ControlState(.5, -.5))
        self.send(ControlState(pan=.5))
        for call in self.camera.ptz.ContinuousMove.call_args_list:
            self.assertNotIn('Zoom', call.args[0].Velocity)

    def test_all_zero_ignoring_device_is_not_claimed_fixed(self):
        self.camera.ptz.ContinuousMove.side_effect = self.camera.move
        self.send(ControlState(.5, -.5))
        self.send(ControlState(pan=.5))
        self.assertEqual(self.camera.motion, [.5, -.5, 0])
        self.send(ControlState())
        self.assertEqual(self.camera.motion, [0, 0, 0])

    def test_preset_requested_during_neutral_is_stopped_before_goto(self):
        self.send(ControlState(.5, -.5))
        def delayed(request):
            self.accept_neutral(request)
            self.worker.submit(ControlState(preset=(1, 'preset')))
        self.camera.ptz.ContinuousMove.side_effect = delayed
        self.send(ControlState(pan=.5))
        self.assertEqual(self.camera.calls[-2:], [('stop', True, True), ('preset', 'preset')])


if __name__ == '__main__':
    unittest.main()
