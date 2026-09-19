"""Observable player behavior retained from v0.2.8, independent of native I/O."""
import gc
import unittest
from unittest.mock import Mock, patch

from player_supervisor import PlayerSnapshot
from player_vilkin_hikvision import VideoPlayer


class PlayerPresentationTests(unittest.TestCase):
    def setUp(self):
        gc.collect()
        self.owner = Mock()
        self.owner.snapshot = PlayerSnapshot()
        self.owner.closed.is_set.return_value = True
        self.factory = Mock(return_value=self.owner)
        self.app = VideoPlayer('presentation', supervisor_factory=self.factory)
        self.app.root.withdraw()

    def tearDown(self):
        self.app.on_closing()
        del self.app
        gc.collect()

    def poll_status(self):
        self.app.root.after_cancel(self.app._timer)
        self.app.check_stream_status()

    def refresh_bitrate(self):
        self.app.root.after_cancel(self.app._bitrate_timer)
        self.app.update_bitrate()

    def test_fast_health_polls_do_not_refresh_bitrate_until_its_own_timer(self):
        self.owner.snapshot = PlayerSnapshot(state='PLAYING', bitrate=2.345)
        self.refresh_bitrate()
        self.assertEqual(self.app.bitrate_label.cget('text'), '2.35 Mbps')
        self.owner.snapshot = PlayerSnapshot(state='PLAYING', bitrate=9.876)
        for _ in range(10):
            self.poll_status()
        self.assertEqual(self.app.bitrate_label.cget('text'), '2.35 Mbps')
        with patch.object(self.app.root, 'after', wraps=self.app.root.after) as after:
            self.refresh_bitrate()
            self.assertEqual(after.call_args.args[0], 1000)
        self.assertEqual(self.app.bitrate_label.cget('text'), '9.88 Mbps')

    def test_outage_clears_the_previous_rate_immediately_and_zero_is_unknown(self):
        self.owner.snapshot = PlayerSnapshot(state='PLAYING', bitrate=5)
        self.refresh_bitrate()
        self.owner.snapshot = PlayerSnapshot(state='RECONNECTING', generation=2)
        self.poll_status()
        self.assertEqual(self.app.bitrate_label.cget('text'), '-- Mbps')
        self.owner.snapshot = PlayerSnapshot(state='PLAYING', generation=2, bitrate=0)
        self.refresh_bitrate()
        self.assertEqual(self.app.bitrate_label.cget('text'), '-- Mbps')

    def test_initial_aspect_ratio_and_user_size_survive_reconnection(self):
        hwnd = self.app.video_surface.winfo_id()
        with patch.object(self.app.root, 'geometry', wraps=self.app.root.geometry) as geometry:
            self.owner.snapshot = PlayerSnapshot(state='PLAYING', width=1920, height=1080)
            self.poll_status()
            geometry.assert_called_once_with('800x490')
            geometry.reset_mock()
            self.app.root.geometry('640x520')
            geometry.reset_mock()
            self.owner.snapshot = PlayerSnapshot(state='RECONNECTING', generation=2)
            self.poll_status()
            self.owner.snapshot = PlayerSnapshot(state='PLAYING', generation=2, width=1280, height=720)
            self.poll_status()
            geometry.assert_not_called()
        self.assertEqual(self.app.video_surface.winfo_id(), hwnd)
        self.factory.assert_called_once()

    def test_legacy_mute_visuals_and_title_remain_selected_through_an_outage(self):
        self.assertEqual(self.app.mute_button.cget('text'), '')
        self.assertEqual(self.app.volume_up_icon.width(), 16)
        self.assertEqual(self.app.volume_mute_icon.width(), 16)
        self.app.mute_button.invoke()
        self.owner.snapshot = PlayerSnapshot(state='RECONNECTING', generation=2)
        self.poll_status()
        self.assertEqual(self.app.root.title(), 'Camera presentation (Muted)')
        self.assertEqual(self.app.mute_button.cget('image'), str(self.app.volume_mute_icon))
        self.assertEqual(self.app.mute_button.cget('background'), '#E0E0E0')
        self.assertEqual(self.app.mute_button.cget('relief'), 'sunken')
        self.app.mute_button.invoke()
        self.assertEqual(self.app.mute_button.cget('image'), str(self.app.volume_up_icon))
        self.assertEqual(self.app.root.title(), 'Camera presentation')
        self.owner.set_audio.assert_called_with(False, 100)


if __name__ == '__main__':
    unittest.main()
