"""PTZ regressions: no camera, credentials, or ONVIF installation required."""
import unittest
from itertools import permutations
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


class UICommandTests(unittest.TestCase):
    def setUp(self):
        self.worker = Mock()
        self.root = Mock()
        self.held = set()
        self.state = patch.multiple(ptz, create=True, keyboard=ptz.KeyboardManager(),
            command_worker=self.worker, root=self.root, key_is_down=self.held.__contains__,
            closing=False,
            window_active=True, preset_request=None, preset_sequence=0, speed=0.5,
            speed_value_label=Mock(), speed_progress={})
        self.state.start()
        self.addCleanup(self.state.stop)

    def press(self, key, code):
        self.held.add(code)
        ptz.on_key_press(SimpleNamespace(keysym=key, keycode=code))

    def release(self, key, code):
        self.held.discard(code)
        ptz.on_key_release(SimpleNamespace(keysym=key, keycode=code))

    def desired(self):
        return self.worker.submit.call_args.args[0]

    def test_diagonal_release_publishes_remaining_direction(self):
        self.press('Down', 40)
        self.press('Right', 39)
        self.release('Down', 40)
        self.assertEqual(self.desired().motion, (0.5, 0, 0))

    def test_each_event_publishes_motion_and_focus_in_one_snapshot(self):
        self.press('Right', 39)
        self.press('q', 81)
        before = self.worker.submit.call_count
        self.press('shift', 16)
        self.assertEqual(self.worker.submit.call_count, before + 1)
        self.assertEqual(self.desired(), ptz.ControlState(pan=0.5, zoom=0.5, focus=0.5))

    def test_diagonal_and_zoom_survive_every_key_order_and_modifier_side(self):
        horizontals = [('Left', 37, -0.5), ('Right', 39, 0.5), ('a', 65, -0.5), ('d', 68, 0.5)]
        verticals = [('Up', 38, 0.5), ('Down', 40, -0.5), ('w', 87, 0.5), ('s', 83, -0.5)]
        modifiers = [('Shift_L', 160, 0.5), ('Shift_R', 161, 0.5),
                     ('Control_L', 162, -0.5), ('Control_R', 163, -0.5)]
        with patch.object(ptz.sys, 'platform', 'win32'):
            for horizontal in horizontals:
                for vertical in verticals:
                    for modifier in modifiers:
                        for order in permutations([horizontal, vertical, modifier]):
                            with self.subTest(order=order):
                                self.held.clear()
                                ptz.keyboard.clear()
                                for key, code, _ in order:
                                    self.press(key, code)
                                ptz.poll_keyboard()
                                self.assertEqual(self.desired().motion,
                                                 (horizontal[2], vertical[2], modifier[2]))
                                self.release(modifier[0], modifier[1])
                                self.assertEqual(self.desired().motion, (horizontal[2], vertical[2], 0))

    def test_poll_recovers_missing_release_without_network_io(self):
        self.press('Down', 40)
        self.press('Right', 39)
        self.held.remove(40)
        ptz.poll_keyboard()
        self.assertEqual(self.desired().motion, (0.5, 0, 0))
        self.root.after.assert_called_with(30, ptz.poll_keyboard)
        self.assertEqual({call[0] for call in self.worker.mock_calls}, {'submit'})

    def test_stale_press_for_released_key_is_ignored(self):
        self.press('Down', 40)
        self.press('Right', 39)
        self.held.remove(40)
        ptz.on_key_press(SimpleNamespace(keysym='Down', keycode=40))
        self.assertEqual(self.desired().motion, (0.5, 0, 0))

    def test_speed_change_publishes_current_axes_and_focus(self):
        self.press('Right', 39)
        self.press('q', 81)
        self.press('m', 77)
        self.assertEqual(self.desired(), ptz.ControlState(pan=0.6, focus=0.6))

    def test_focus_loss_cancels_pending_input_and_preset(self):
        controller = ptz.PTZController(self.root, 'camera', 'ip')
        self.press('Right', 39)
        self.root.focus_get.return_value = None
        controller.check_focus()
        self.worker.halt.assert_called_once()
        self.assertFalse(ptz.keyboard.pressed_keys)
        self.assertFalse(ptz.window_active)
        ptz.poll_keyboard()
        self.assertEqual(self.desired(), ptz.ControlState())

    def test_inactive_window_does_not_arm_keys(self):
        ptz.window_active = False
        self.press('Right', 39)
        self.worker.submit.assert_not_called()

    def test_internal_focus_transfer_preserves_input(self):
        controller = ptz.PTZController(self.root, 'camera', 'ip')
        self.root.focus_get.return_value = Mock()
        controller.check_focus()
        self.worker.halt.assert_not_called()

    def test_close_waits_asynchronously_and_drops_further_key_events(self):
        self.worker.is_alive.return_value = True
        self.press('Right', 39)
        ptz.on_key_press(SimpleNamespace(keysym='Escape', keycode=27))
        self.worker.close.assert_called_once()
        self.root.after.assert_called_with(30, ptz.finish_close)
        self.root.quit.assert_not_called()
        before = self.worker.submit.call_count
        self.press('Down', 40)
        self.release('Right', 39)
        ptz.poll_keyboard()
        self.assertEqual(self.worker.submit.call_count, before)
        self.worker.is_alive.return_value = False
        ptz.finish_close()
        self.root.quit.assert_called_once()

    def test_preset_survives_key_release_and_poll_until_manual_control(self):
        self.press('1', 49)
        preset = self.desired().preset
        self.release('1', 49)
        ptz.poll_keyboard()
        self.assertEqual(self.desired().preset, preset)
        self.press('Right', 39)
        self.assertIsNone(self.desired().preset)

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
