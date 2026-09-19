import importlib
import sys
import unittest
from unittest.mock import Mock, patch

from camera_health_monitor import HealthUpdate


class ViewerHealthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        crypto = Mock()
        crypto.Fernet.return_value.decrypt.return_value = b'test-password'
        with patch.dict(sys.modules, {'cryptography': Mock(), 'cryptography.fernet': crypto}), \
                patch('camera_key.load_encryption_key', return_value=None):
            cls.viewer = importlib.import_module('camera_viewer')

    def setUp(self):
        self.root = self.viewer.tk.Tk()
        self.root.withdraw()
        self.monitor = Mock()
        self.monitor.poll.return_value = None
        self.monitor.updates.return_value = []
        self.patches = [patch.object(self.viewer, 'HealthMonitor', return_value=self.monitor),
                        patch.object(self.viewer, 'get_cameras', return_value=[(1, 'camera', 'user', b'ciphertext', 1)]),
                        patch.object(self.viewer, 'load_overrides', return_value={})]
        for item in self.patches:
            item.start()
        self.app = self.viewer.CameraViewer(self.root)

    def tearDown(self):
        if self.app.health_after is not None:
            self.root.after_cancel(self.app.health_after)
        self.root.destroy()
        for item in reversed(self.patches):
            item.stop()

    def tick(self):
        self.root.after_cancel(self.app.health_after)
        self.app._poll_health()

    def test_canvas_and_text_update_only_when_tk_drains_messages(self):
        canvas, dot, label = self.app.health_widgets[1]
        self.assertEqual(label.cget('text'), 'Checking')
        self.monitor.updates.return_value = [HealthUpdate(1, 'degraded', 'Ping responds.', 100, 90)]
        self.assertEqual(label.cget('text'), 'Checking')
        self.tick()
        self.assertEqual(label.cget('text'), 'Degraded')
        self.assertEqual(canvas.itemcget(dot, 'fill'), '#b77900')
        self.assertEqual(self.app.status_detail.cget('text'), '1 degraded')
        self.assertNotIn('Ping responds.', self.app.status_detail.cget('text'))

    def test_crashed_monitor_clears_stale_green_status(self):
        self.monitor.updates.return_value = [HealthUpdate(1, 'online', 'Responds.', 100, 90)]
        self.tick()
        self.monitor.updates.return_value = []
        self.monitor.poll.return_value = 1
        self.tick()
        self.assertEqual(self.app.health_widgets[1][2].cget('text'), 'Unknown')
        self.assertEqual(self.app.health_updates, {})

    def test_reload_stops_previous_monitor_and_replaces_old_widgets(self):
        canvas = self.app.health_widgets[1][0]
        self.app.load_cameras()
        self.monitor.stdin.close.assert_called_once()
        self.assertFalse(canvas.winfo_exists())
        self.assertEqual(len(self.app.health_widgets), 1)

    def test_close_cancels_tk_poll_and_notifies_all_children(self):
        with patch.object(self.app.children, 'close') as close:
            self.app.on_closing()
            close.assert_called_once()
            self.assertIsNone(self.app.health_after)

    def test_manager_exit_reloads_configuration(self):
        manager = Mock()
        manager.poll.return_value = 0
        self.app.managers.append(manager)
        with patch.object(self.app, 'load_cameras') as reload:
            self.tick()
            reload.assert_called_once()

    def test_controls_and_indicator_fit_the_preview_window(self):
        self.root.geometry('470x290')
        self.root.update_idletasks()
        canvas, _, label = self.app.health_widgets[1]
        frame = label.master
        buttons = [w.cget('text') for w in frame.winfo_children() if isinstance(w, self.viewer.tk.Button)]
        self.assertEqual(buttons, ['Play', 'PTZ'])
        self.assertLess(frame.winfo_reqwidth() + 95, 450)
        self.assertEqual(self.app.manage_button.cget('text'), 'Manage Cameras')

    def test_summary_covers_all_cameras_without_hover_or_expanding_the_footer(self):
        cameras = [(i, 'camera', 'user', b'ciphertext', 1) for i in (1, 2, 3)]
        with patch.object(self.viewer, 'get_cameras', return_value=cameras):
            self.app.load_cameras()
        self.assertEqual(set(self.app.health_widgets), {1, 2, 3})
        self.assertEqual(self.app.status_detail.cget('text'), '3 checking')
        self.root.geometry('470x290')
        self.root.update_idletasks()
        initial_height = self.app.status_detail.winfo_reqheight()
        self.monitor.updates.return_value = [
            HealthUpdate(1, 'online', 'RTSP service available.', 100, 90),
            HealthUpdate(2, 'online', 'RTSP service available.', 100, 90),
            HealthUpdate(3, 'offline', 'Detailed diagnostic text. ' * 20, 100, 90)]
        self.tick()
        self.assertEqual(self.app.status_detail.cget('text'), '2 online · 1 unreachable')
        self.app.health_widgets[1][2].event_generate('<Enter>')
        self.app.health_widgets[2][2].event_generate('<FocusIn>')
        self.root.update_idletasks()
        self.assertEqual(self.app.status_detail.cget('text'), '2 online · 1 unreachable')
        self.assertEqual(self.app.status_detail.winfo_reqheight(), initial_height)
        rows = self.app.camera_list.get('1.0', 'end').splitlines()
        for index in (1, 2, 3):
            self.assertIn(f'Camera : {index} ', rows)

    def test_summary_refreshes_on_recovery_and_empty_list(self):
        self.monitor.updates.return_value = [HealthUpdate(1, 'offline', '', 100, 90)]
        self.tick()
        self.assertEqual(self.app.status_detail.cget('text'), '1 unreachable')
        self.monitor.updates.return_value = [HealthUpdate(1, 'online', '', 110, 110)]
        self.tick()
        self.assertEqual(self.app.status_detail.cget('text'), '1 online')
        with patch.object(self.viewer, 'get_cameras', return_value=[]):
            self.app.load_cameras()
        self.assertEqual(self.app.status_detail.cget('text'), 'No cameras configured.')


if __name__ == '__main__':
    unittest.main()
