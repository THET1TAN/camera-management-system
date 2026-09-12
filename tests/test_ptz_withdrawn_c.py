"""A retired launcher or stale setting must not reactivate overlapping PTZ."""
import contextlib
import io
import os
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import camera_viewer_direct_test as candidate_b
import camera_viewer_early_resume_test as retired_c
import ptz_keyboard_control as ptz
from ptz_command_worker import PTZCommandWorker


class WithdrawnCTests(unittest.TestCase):
    def test_old_launcher_runs_b_and_disables_inherited_overlap(self):
        seen = []
        def viewer(*args, **kwargs):
            seen.append((os.environ['CAMERA_PTZ_EARLY_RESUME'],
                         os.environ['CAMERA_PTZ_NEUTRAL_TRANSITIONS'],
                         os.environ['CAMERA_PTZ_CONSERVATIVE_STOPS']))
        with patch.dict(os.environ, {'CAMERA_PTZ_EARLY_RESUME': '1'}), \
                patch.object(candidate_b.runpy, 'run_path', side_effect=viewer), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            retired_c.main()
        self.assertIn('withdrawn', output.getvalue())
        self.assertEqual(seen, [('0', '1', '0')])

    def test_stale_environment_on_direct_ptz_start_creates_only_b_worker(self):
        self.assert_b_startup({'CAMERA_PTZ_EARLY_RESUME': '1',
                               'CAMERA_PTZ_NEUTRAL_TRANSITIONS': '0',
                               'CAMERA_PTZ_CONSERVATIVE_STOPS': '0'})

    def test_normal_startup_without_test_environment_uses_released_b(self):
        self.assert_b_startup({})

    def assert_b_startup(self, environment):
        class ReachedWindow(Exception):
            pass
        service = Mock()
        camera = Mock()
        camera.create_ptz_service.return_value = service
        camera.create_imaging_service.return_value = Mock()
        camera.create_media_service().GetProfiles.return_value = [SimpleNamespace(
            token='p', VideoSourceConfiguration=SimpleNamespace(SourceToken='s'), PTZConfiguration=None)]
        service.GetConfigurationOptions.side_effect = RuntimeError('No optional configuration')
        previous = ptz.command_worker
        try:
            with patch.dict(os.environ, environment, clear=True), \
                    patch.dict(sys.modules, {'onvif': SimpleNamespace(ONVIFCamera=Mock(return_value=camera))}), \
                    patch.object(ptz, 'PTZDiagnostics', return_value=Mock()), \
                    patch.object(ptz, 'create_key_state_reader', return_value=None), \
                    patch.object(ptz.tk, 'Tk', side_effect=ReachedWindow):
                with self.assertRaises(ReachedWindow):
                    ptz.main(['test', 'test-host', 'test-user', 'test-password'])
            self.assertIs(type(ptz.command_worker), PTZCommandWorker)
            self.assertTrue(ptz.command_worker.neutral_transitions)
            self.assertFalse(ptz.command_worker.conservative_stops)
            self.assertIsNone(ptz.command_worker._thread)
            camera.get_definition.assert_not_called()
            service.ContinuousMove.assert_not_called()
        finally:
            ptz.command_worker = previous
