"""PTZ regressions: no camera, credentials, or ONVIF installation required."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import ptz_keyboard_control as ptz


class KeyboardTests(unittest.TestCase):
    def setUp(self):
        self.keyboard = ptz.KeyboardManager()

    def test_diagonal_release_each_direction_in_either_order(self):
        for horizontal, pan in [('left', -1), ('right', 1), ('a', -1), ('d', 1)]:
            for vertical, tilt in [('up', 1), ('down', -1), ('w', 1), ('s', -1)]:
                for first, second in [(horizontal, vertical), (vertical, horizontal)]:
                    with self.subTest(keys=(horizontal, vertical), release=first):
                        self.keyboard.clear()
                        self.keyboard.press_key(horizontal)
                        self.keyboard.press_key(vertical)
                        self.assertEqual(self.keyboard.get_movement(), (pan, tilt, 0))
                        self.keyboard.release_key(first)
                        expected = (0, tilt, 0) if first == horizontal else (pan, 0, 0)
                        self.assertEqual(self.keyboard.get_movement(), expected)
                        self.keyboard.release_key(second)
                        self.assertEqual(self.keyboard.get_movement(), (0, 0, 0))

    def test_alias_release_keeps_other_key_for_same_direction(self):
        self.keyboard.press_key('s')
        self.keyboard.press_key('down')
        self.keyboard.release_key('s')
        self.assertEqual(self.keyboard.get_movement(), (0, -1, 0))

    def test_autorepeat_does_not_steal_priority_from_opposite_key(self):
        self.keyboard.press_key('left')
        self.keyboard.press_key('right')
        self.assertFalse(self.keyboard.press_key('left'))
        self.assertEqual(self.keyboard.get_movement(), (1, 0, 0))
        self.keyboard.release_key('right')
        self.assertEqual(self.keyboard.get_movement(), (-1, 0, 0))

    def test_latest_remaining_physical_key_wins_after_alias_release(self):
        for key in ['left', 'right', 'a']:
            self.keyboard.press_key(key)
        self.keyboard.release_key('a')
        self.assertEqual(self.keyboard.get_movement(), (1, 0, 0))

    def test_both_shift_keys_are_independent(self):
        self.keyboard.press_key('shift_l', 160)
        self.keyboard.press_key('shift_r', 161)
        self.keyboard.release_key('shift_l', 160)
        self.assertEqual(self.keyboard.get_movement(), (0, 0, 1))
        self.keyboard.release_key('shift_r', 161)
        self.assertEqual(self.keyboard.get_movement(), (0, 0, 0))

    def test_release_uses_physical_code_despite_changed_symbol(self):
        self.keyboard.press_key('s', 83)
        self.keyboard.release_key('different_symbol', 83)
        self.assertEqual(self.keyboard.get_movement(), (0, 0, 0))

    def test_missed_release_is_recovered_without_releasing_other_axis(self):
        self.keyboard.press_key('down', 40)
        self.keyboard.press_key('right', 39)
        self.keyboard.synchronize(lambda code: code == 39)
        self.assertEqual(self.keyboard.get_movement(), (1, 0, 0))

    def test_poll_does_not_activate_keys_pressed_outside_window(self):
        self.keyboard.synchronize(lambda code: True)
        self.assertEqual(self.keyboard.get_movement(), (0, 0, 0))

    def test_clear_resets_movement_and_focus(self):
        for key in ['right', 'down', 'shift_l', 'q']:
            self.keyboard.press_key(key)
        self.keyboard.clear()
        self.assertEqual(self.keyboard.get_movement(), (0, 0, 0))
        self.assertEqual(self.keyboard.get_focus(), 0)

    def test_focus_returns_to_held_opposite_key(self):
        self.keyboard.press_key('q')
        self.keyboard.press_key('e')
        self.assertEqual(self.keyboard.get_focus(), -1)
        self.keyboard.release_key('e')
        self.assertEqual(self.keyboard.get_focus(), 1)


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.service = Mock()
        self.service.create_type.side_effect = lambda name: SimpleNamespace()
        self.imaging = Mock()
        self.imaging.create_type.side_effect = lambda name: SimpleNamespace()
        self.root = Mock()
        self.held = set()
        values = dict(
            keyboard=ptz.KeyboardManager(), current_pan=0, current_tilt=0,
            current_zoom=0, current_focus=0, speed=0.5, ptz_service=self.service,
            imaging_service=self.imaging, media_profile=SimpleNamespace(token='test-profile'),
            video_source_token='test-source', PTZSpeed=SimpleNamespace,
            Vector2D=SimpleNamespace, Vector1D=SimpleNamespace, root=self.root,
            speed_value_label=Mock(), speed_progress={}, key_is_down=self.held.__contains__,
        )
        self.state = patch.multiple(ptz, create=True, **values)
        self.state.start()
        self.addCleanup(self.state.stop)

    def press(self, key, code):
        self.held.add(code)
        ptz.on_key_press(SimpleNamespace(keysym=key, keycode=code))

    def release(self, key, code):
        self.held.discard(code)
        ptz.on_key_release(SimpleNamespace(keysym=key, keycode=code))

    def velocity(self):
        request = self.service.ContinuousMove.call_args.args[0]
        self.assertEqual(request.ProfileToken, 'test-profile')
        return (request.Velocity.PanTilt.x, request.Velocity.PanTilt.y,
                request.Velocity.Zoom.x)

    def test_down_right_then_release_down_sends_horizontal_only(self):
        self.press('Down', 40)
        self.press('Right', 39)
        self.assertEqual(self.velocity(), (0.5, -0.5, 0))
        self.release('Down', 40)
        self.assertEqual(self.velocity(), (0.5, 0, 0))
        self.service.Stop.assert_not_called()

    def test_down_right_then_release_right_sends_vertical_only(self):
        self.press('Down', 40)
        self.press('Right', 39)
        self.release('Right', 39)
        self.assertEqual(self.velocity(), (0, -0.5, 0))
        self.service.Stop.assert_not_called()

    def test_final_release_sends_explicit_stop_once(self):
        self.press('Right', 39)
        self.release('Right', 39)
        ptz.poll_keyboard()
        self.service.Stop.assert_called_once_with({
            'ProfileToken': 'test-profile', 'PanTilt': True, 'Zoom': True})

    def test_poll_recovers_missing_release_event_in_diagonal(self):
        self.press('Down', 40)
        self.press('Right', 39)
        self.held.remove(40)  # Windows key-up state, but no Tk KeyRelease.
        ptz.poll_keyboard()
        self.assertEqual(self.velocity(), (0.5, 0, 0))
        self.service.Stop.assert_not_called()
        self.root.after.assert_called_with(30, ptz.poll_keyboard)

    def test_queued_repeat_cannot_restart_physically_released_key(self):
        self.press('Down', 40)
        self.press('Right', 39)
        self.held.remove(40)
        ptz.on_key_press(SimpleNamespace(keysym='Down', keycode=40))
        self.assertEqual(self.velocity(), (0.5, 0, 0))

    def test_zoom_release_preserves_pan_and_tilt(self):
        self.press('s', 83)
        self.press('d', 68)
        self.press('shift', 16)
        self.assertEqual(self.velocity(), (0.5, -0.5, 0.5))
        self.release('shift', 16)
        self.assertEqual(self.velocity(), (0.5, -0.5, 0))
        self.service.Stop.assert_not_called()

    def test_direction_release_preserves_zoom_and_focus(self):
        self.press('s', 83)
        self.press('shift', 16)
        self.press('q', 81)
        self.release('s', 83)
        self.assertEqual(self.velocity(), (0, 0, 0.5))
        self.imaging.Stop.assert_not_called()
        self.release('q', 81)
        self.imaging.Stop.assert_called_once()
        self.assertEqual(self.velocity(), (0, 0, 0.5))

    def test_speed_change_applies_immediately_to_held_axes(self):
        self.press('Down', 40)
        self.press('Right', 39)
        self.press('m', 77)
        self.assertEqual(self.velocity(), (0.6, -0.6, 0))

    def test_unchanged_keys_do_not_send_redundant_requests(self):
        self.press('Right', 39)
        self.press('Right', 39)
        ptz.poll_keyboard()
        self.service.ContinuousMove.assert_called_once()

    def test_lost_focus_stops_all_and_clears_keys(self):
        controller = ptz.PTZController(self.root, 'test-camera', 'test-ip')
        self.press('Down', 40)
        self.press('q', 81)
        self.root.focus_get.return_value = None
        controller.check_focus()
        self.service.Stop.assert_called_once()
        self.imaging.Stop.assert_called_once()
        self.assertEqual(ptz.keyboard.get_movement(), (0, 0, 0))
        self.assertEqual(ptz.keyboard.get_focus(), 0)
        ptz.poll_keyboard()
        self.service.ContinuousMove.assert_called_once()

    def test_internal_focus_transfer_preserves_movement(self):
        controller = ptz.PTZController(self.root, 'test-camera', 'test-ip')
        self.press('Down', 40)
        self.root.focus_get.return_value = Mock()
        controller.on_focus_out(None)
        self.root.after_idle.assert_called_once_with(controller.check_focus)
        controller.check_focus()
        self.service.Stop.assert_not_called()

    def test_escape_clears_keys_and_stops_even_after_fast_key_tap(self):
        self.press('Right', 39)
        ptz.on_key_press(SimpleNamespace(keysym='Escape', keycode=27))
        self.service.Stop.assert_called_once()
        self.imaging.Stop.assert_called_once()
        self.root.quit.assert_called_once()
        self.assertFalse(ptz.keyboard.pressed_keys)

    def test_failed_partial_move_is_retried(self):
        self.press('Down', 40)
        self.press('Right', 39)
        self.service.ContinuousMove.side_effect = [RuntimeError('test timeout'), None]
        with patch('builtins.print'):
            self.release('Down', 40)
        self.assertIsNone(ptz.current_tilt)
        ptz.poll_keyboard()
        self.assertEqual(self.velocity(), (0.5, 0, 0))
        self.assertEqual((ptz.current_pan, ptz.current_tilt), (0.5, 0))

    def test_failed_first_move_still_sends_stop_on_release(self):
        self.service.ContinuousMove.side_effect = RuntimeError('test timeout')
        with patch('builtins.print'):
            self.press('Right', 39)
        self.release('Right', 39)
        self.service.Stop.assert_called_once()

    def test_failed_stop_is_retried_on_next_poll(self):
        self.press('Right', 39)
        self.service.Stop.side_effect = [RuntimeError('test timeout'), None]
        with patch('builtins.print'):
            self.release('Right', 39)
        ptz.poll_keyboard()
        self.assertEqual(self.service.Stop.call_count, 2)
        self.assertEqual(ptz.current_pan, 0)


class PlatformTests(unittest.TestCase):
    def test_windows_modifier_sides_have_distinct_codes(self):
        with patch.object(ptz.sys, 'platform', 'win32'):
            for key, expected in [('Shift_L', 160), ('Shift_R', 161),
                                  ('Control_L', 162), ('Control_R', 163)]:
                self.assertEqual(ptz.event_keycode(SimpleNamespace(keysym=key, keycode=16)), expected)

    def test_non_windows_uses_tk_events(self):
        with patch.object(ptz.sys, 'platform', 'linux'):
            self.assertIsNone(ptz.create_key_state_reader())


if __name__ == '__main__':
    unittest.main()
