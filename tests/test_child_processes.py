"""Graceful parent/child shutdown, including a real process tree and Tk PTZ."""
import io
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from child_processes import ChildProcesses, ParentLifetime, PARENT_PIPE_ENV


class ChildProcessesTests(unittest.TestCase):
    def setUp(self):
        self.root = Mock()
        self.now = 0
        self.children = ChildProcesses(self.root, clock=lambda: self.now)

    def child(self):
        process = Mock()
        process.poll.return_value = None
        process.stdin = io.BytesIO()
        self.children.processes.append(process)
        return process

    def test_closing_notifies_all_children_before_waiting(self):
        children = [self.child() for _ in range(3)]
        self.children.close()
        for child in children:
            self.assertTrue(child.stdin.closed)
            child.kill.assert_not_called()
            child.wait.assert_not_called()
        self.root.withdraw.assert_called_once()
        self.root.destroy.assert_not_called()
        self.root.after.assert_called_once()

    def test_window_is_destroyed_after_every_child_exits(self):
        child = self.child()
        self.children.close()
        child.poll.return_value = 0
        self.root.after.call_args.args[1]()
        self.root.destroy.assert_called_once()
        self.assertEqual(self.children.processes, [])

    def test_unresponsive_owned_child_is_killed_after_grace_period(self):
        child = self.child()
        self.children.close()
        self.now = self.children.GRACE_SECONDS
        self.root.after.call_args.args[1]()
        child.kill.assert_called_once()
        self.root.destroy.assert_not_called()
        child.poll.return_value = 1
        self.root.after.call_args.args[1]()
        self.root.destroy.assert_called_once()

    def test_close_is_idempotent_and_prevents_new_children(self):
        with patch('child_processes.subprocess.Popen') as popen:
            self.children.close()
            self.children.close()
            self.assertIsNone(self.children.spawn(['not-run']))
        popen.assert_not_called()
        self.root.destroy.assert_called_once()

    def test_spawn_registers_pipe_and_reaps_closed_children(self):
        old = self.child()
        old.poll.return_value = 0
        with patch('child_processes.subprocess.Popen') as popen:
            new = self.children.spawn(['python', 'child.py'])
            args = popen.call_args.kwargs
        self.assertEqual(args['env'][PARENT_PIPE_ENV], '1')
        self.assertEqual(args['stdin'], subprocess.PIPE)
        self.assertEqual(self.children.processes, [new])
        self.assertTrue(old.stdin.closed)

    def test_parent_eof_closes_child_on_tk_thread(self):
        lifetime = ParentLifetime(io.BytesIO())
        self.assertTrue(lifetime.closed.wait(1))
        close = Mock()
        lifetime.bind(self.root, close)
        close.assert_not_called()
        self.root.after.call_args.args[1]()
        close.assert_called_once()

    def test_standalone_window_is_not_closed(self):
        close = Mock()
        ParentLifetime().bind(self.root, close)
        self.root.after.call_args.args[1]()
        close.assert_not_called()


class ProcessIntegrationTests(unittest.TestCase):
    def run_helper(self, code, *args):
        env = dict(os.environ, **{PARENT_PIPE_ENV: '1'})
        return subprocess.Popen([sys.executable, '-c', code, *args], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)

    def test_real_parent_eof_propagates_to_grandchild(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'grandchild-stopped'
            leaf = "from child_processes import parent_lifetime; from pathlib import Path; import sys; parent_lifetime.closed.wait(5); Path(sys.argv[1]).write_text('closed')"
            code = """
from child_processes import ChildProcesses, parent_lifetime
from unittest.mock import Mock
import sys, time
root = Mock()
children = ChildProcesses(root)
child = children.spawn([sys.executable, '-c', sys.argv[1], sys.argv[2]])
print('ready', flush=True)
assert parent_lifetime.closed.wait(5)
children.close()
child.wait(timeout=5)
assert child.returncode == 0
"""
            process = self.run_helper(code, leaf, str(marker))
            try:
                self.assertEqual(process.stdout.readline().strip(), b'ready')
                process.stdin.close()
                process.wait(timeout=7)
                self.assertEqual(process.returncode, 0, process.stderr.read().decode())
                self.assertEqual(marker.read_text(), 'closed')
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=2)
                process.stdout.close()
                process.stderr.close()


    def test_real_ptz_window_stops_camera_before_exiting_on_parent_eof(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'stops.json'
            code = """
import json, sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import ptz_keyboard_control as ptz
camera = Mock()
service = Mock()
service.create_type.side_effect = lambda name: SimpleNamespace()
camera.create_ptz_service.return_value = service
camera.create_imaging_service.return_value = service
camera.create_media_service().GetProfiles.return_value = [SimpleNamespace(
    token='profile', VideoSourceConfiguration=SimpleNamespace(SourceToken='source'))]
real_tk = ptz.tk.Tk
def window():
    root = real_tk()
    root.withdraw()
    def ready():
        # Keep a synthetic held movement active until parent shutdown.
        ptz.keyboard.press_key('d')
        print('ready', flush=True)
    root.after(50, ready)
    root.after(5000, ptz.close_controller)
    return root
with patch.dict(sys.modules, {'onvif': SimpleNamespace(ONVIFCamera=Mock(return_value=camera))}):
    with patch.object(ptz.tk, 'Tk', window):
        assert ptz.main(['id', 'ip', 'user', 'password']) == 0
assert not ptz.command_worker.is_alive()
Path(sys.argv[1]).write_text(json.dumps([call.args[0] for call in service.Stop.call_args_list]))
"""
            process = self.run_helper(code, str(marker))
            try:
                # The controller also writes its normal startup message.
                for _ in range(2):
                    if process.stdout.readline().strip() == b'ready':
                        break
                else:
                    self.fail('PTZ helper did not become ready')
                process.stdin.close()
                process.wait(timeout=8)
                self.assertEqual(process.returncode, 0, process.stderr.read().decode())
                stops = json.loads(marker.read_text())
                self.assertIn({'ProfileToken': 'profile', 'PanTilt': True, 'Zoom': True}, stops)
                self.assertIn({'VideoSourceToken': 'source'}, stops)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=2)
                process.stdout.close()
                process.stderr.close()


class WindowOwnershipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Exercise the real windows without reading any camera database or keys.
        crypto = Mock()
        crypto.Fernet.return_value.decrypt.return_value = b'test-password'
        with patch.dict(sys.modules, {'cryptography': Mock(), 'cryptography.fernet': crypto}):
            with patch('camera_key.load_encryption_key', return_value=None):
                cls.viewer = importlib.import_module('camera_viewer')
                cls.manager = importlib.import_module('camera_manager')

    def check_window(self, module, cls, with_manager):
        root = module.tk.Tk()
        root.withdraw()
        try:
            with patch.object(module, 'get_cameras', return_value=[]):
                app = cls(root)
            processes = []

            def popen(*args, **kwargs):
                # Do not replace interpreter selection in this test: a Viewer
                # started on 3.14 must not launch 3.9 with inherited 3.14 packages.
                self.assertEqual(args[0][0], sys.executable)
                child = Mock()
                child.poll.return_value = None
                child.stdin = io.BytesIO()
                processes.append(child)
                return child

            camera = (1, 'test-ip', 'test-user', b'encrypted-test-password', 1)
            with patch('child_processes.subprocess.Popen', side_effect=popen):
                app.play_camera_thread(camera)
                app.play_ptz_thread(camera)
                if with_manager:
                    app.open_camera_manager()
            self.assertEqual(len(app.children.processes), 3 if with_manager else 2)
            app.on_closing()
            for process in processes:
                self.assertTrue(process.stdin.closed)
                process.kill.assert_not_called()
        finally:
            root.destroy()

    def test_viewer_closes_video_ptz_and_manager(self):
        self.check_window(self.viewer, self.viewer.CameraViewer, True)

    def test_manager_closes_its_video_and_ptz_children(self):
        self.check_window(self.manager, self.manager.CameraApp, False)



if __name__ == '__main__':
    unittest.main()
