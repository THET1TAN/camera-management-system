"""Session persistence, independent instances and restoration without camera I/O."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
import gc
import importlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from camera_session import SessionEntry, SessionStore, SessionWorkspace, SessionWindow, SESSION_ENV
from window_positions import PositionStore, Screen, screens_signature, WindowPlacement


def eventually(check, root=None, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if root is not None:
            root.update()
        if check():
            return
        time.sleep(.01)
    raise AssertionError('Session operation timed out')


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        gc.collect()
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'layout.db'
        self.store = SessionStore(self.path)
        self.owner = 'a' * 32

    def add(self, camera=1, kind='video', slot=0, muted=False, claim=False):
        entry = SessionEntry.create(camera, kind, slot, muted)
        return self.store.add(self.owner, entry, restore=True, claim=claim)

    def test_session_survives_new_process_with_distinct_instances_and_audio(self):
        first = self.add(muted=True, claim=True)
        self.add(slot=1)
        self.add(kind='ptz')
        code = ('import json,sys; from dataclasses import asdict; '
                'from camera_session import SessionStore; '
                'print(json.dumps([asdict(e) for e in SessionStore(sys.argv[1]).load()]))')
        process = subprocess.run([sys.executable, '-c', code, str(self.path)],
                                 capture_output=True, text=True, timeout=5, check=True)
        rows = json.loads(process.stdout)
        self.assertEqual(len(rows), 3)
        self.assertEqual(next(r for r in rows if r['window_id'] == first.window_id)['muted'], True)

    def test_manual_close_removes_only_that_instance_but_remembers_audio(self):
        first = self.add(muted=True, claim=True)
        second = self.add(slot=1)
        self.store.report(self.owner, first.window_id, closed=True)
        self.assertEqual(self.store.load(), [second])
        reopened = self.store.add(self.owner, SessionEntry.create(1, 'video'))
        self.assertTrue(reopened.muted)

    def test_parent_close_and_crash_preserve_membership_and_latest_audio(self):
        entry = self.add(claim=True)
        self.store.report(self.owner, entry.window_id, muted=True, closed=True, preserve=True)
        self.assertTrue(self.store.load()[0].muted)
        self.store.freeze(self.owner)
        self.store.report(self.owner, entry.window_id, closed=True)
        self.assertEqual(len(self.store.load()), 1)

    def test_new_viewer_does_not_erase_session_until_it_opens_a_window(self):
        entry = self.add(claim=True)
        workspace = SessionWorkspace(self.path)
        try:
            self.assertEqual(workspace.loaded.result(3), [entry])
            workspace.freeze().result(3)
            self.assertEqual(self.store.load(), [entry])
        finally:
            workspace.shutdown()

    def test_old_child_and_old_viewer_cannot_modify_new_owners_session(self):
        old = self.add(claim=True)
        current = SessionEntry.create(2, 'video')
        self.store.add('b' * 32, current, claim=True)
        self.store.report(self.owner, old.window_id, muted=True, closed=True)
        self.store.freeze(self.owner)
        self.assertEqual(self.store.load(), [current])
        with self.assertRaises(RuntimeError):
            self.store.add(self.owner, SessionEntry.create(3, 'video'))

    def test_retry_replaces_failed_slot_and_ignores_late_reports(self):
        old = self.add(claim=True)
        current = self.add(muted=True)
        self.store.report(self.owner, old.window_id, muted=False, closed=True)
        self.assertEqual(self.store.load(), [current])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM session_windows').fetchone()[0], 1)

    def test_position_reset_preserves_session_audio_and_legacy_coordinates(self):
        signature = screens_signature((Screen('one', (0, 0, 800, 600), (0, 0, 800, 560), True),))
        positions = PositionStore(self.path)
        positions.save('1', 0, signature, 20, 30, 500, 300)
        entry = self.add(muted=True, claim=True)
        self.assertEqual(positions.read('1')[1][1:], (20, 30, 500, 300))
        positions.reset()
        self.assertEqual(self.store.load(), [entry])
        self.assertIsNone(positions.read('1')[1])
        reopened = self.store.add(self.owner, SessionEntry.create(1, 'video'))
        self.assertTrue(reopened.muted)

    def test_concurrent_children_keep_all_mutes_and_memberships(self):
        entries = [self.add(camera=i+1, claim=i == 0) for i in range(12)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda e: self.store.report(self.owner, e.window_id, muted=True), entries))
        self.assertEqual(len(self.store.load()), 12)
        self.assertTrue(all(e.muted for e in self.store.load()))

    def test_storage_contains_only_identifiers_kinds_instances_and_mutes(self):
        self.add(muted=True, claim=True)
        with closing(sqlite3.connect(self.path)) as db:
            columns = {row[1] for row in db.execute('PRAGMA table_info(session_windows)')}
        self.assertEqual(columns, {'window_id', 'camera_id', 'kind', 'slot', 'muted'})

    def test_corrupt_database_is_not_replaced_and_reports_failure(self):
        original = b'not a SQLite database'
        self.path.write_bytes(original)
        workspace = SessionWorkspace(self.path)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                workspace.loaded.result(3)
            with self.assertRaises(sqlite3.DatabaseError):
                workspace.open(SessionEntry.create(1, 'video')).result(3)
            self.assertEqual(self.path.read_bytes(), original)
        finally:
            workspace.shutdown()

    def test_locked_database_failure_does_not_replace_previous_session(self):
        entry = self.add(claim=True)
        workspace = SessionWorkspace(self.path)
        workspace.loaded.result(3)
        try:
            with closing(sqlite3.connect(self.path)) as db:
                db.execute('BEGIN IMMEDIATE')
                with self.assertRaises(sqlite3.OperationalError):
                    workspace.open(SessionEntry.create(2, 'video')).result(3)
                db.rollback()
            self.assertEqual(self.store.load(), [entry])
        finally:
            workspace.shutdown()

    def test_invalid_or_duplicate_rows_are_skipped(self):
        entry = self.add(claim=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('INSERT INTO session_windows VALUES (?,?,?,?,?)',
                       ('b'*32, '1', 'video', 0, 0))
            db.execute('INSERT INTO session_windows VALUES (?,?,?,?,?)',
                       ('c'*32, 'rtsp://private', 'video', 1, 0))
            db.execute('INSERT INTO session_windows VALUES (?,?,?,?,?)',
                       ('d'*32, '2', 'unknown', 0, 0))
        self.assertEqual(len(self.store.load()), 1)

    def test_child_environment_is_consumed_and_close_flushes_latest_mute(self):
        entry = self.add(claim=True)
        with patch.dict(os.environ, dict(entry.environment(self.owner),
                                        CAMERA_WINDOW_POSITIONS_FILE=str(self.path))):
            child = SessionWindow.from_environment(1, 'video')
            self.assertIsNotNone(child)
            self.assertFalse(any(name in os.environ for name in SESSION_ENV))
            child.audio(True)
            child.close(preserve=True)
            child.pending.result(3)
            self.assertTrue(child.finished())
        self.assertTrue(self.store.load()[0].muted)

    def test_child_retries_transient_lock_and_final_close_keeps_latest_mute(self):
        entry = self.add(claim=True)
        child = SessionWindow(self.owner, entry, self.path)
        with patch.object(child.store, 'report', side_effect=[
                sqlite3.OperationalError('locked'), sqlite3.OperationalError('locked'), None]) as report:
            child.audio(True)
            self.assertTrue(child.pending.result(3))
            self.assertEqual(report.call_count, 3)
        child.close(preserve=True)
        self.assertTrue(child.pending.result(3))
        self.assertTrue(self.store.load()[0].muted)


class ViewerSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        crypto = Mock()
        crypto.Fernet.return_value.decrypt.return_value = b'fake-password'
        with patch.dict(sys.modules, {'cryptography': Mock(), 'cryptography.fernet': crypto}), \
                patch('camera_key.load_encryption_key', return_value=None):
            cls.viewer = importlib.import_module('camera_viewer')

    def setUp(self):
        gc.collect()
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / 'layout.db'
        self.env = patch.dict(os.environ, {'CAMERA_WINDOW_POSITIONS_FILE': str(self.path)})
        self.env.start()
        self.cameras = [(i, 'fake-camera', 'fake-user', b'fake-ciphertext', 1) for i in (1, 2)]
        self.processes = []
        self.owner = 'a'*32
        self.store = SessionStore(self.path)
        self.saved = [SessionEntry.create(1, 'video', 0, True),
                      SessionEntry.create(1, 'video', 1, False),
                      SessionEntry.create(1, 'ptz'), SessionEntry.create(2, 'video')]
        for i, entry in enumerate(self.saved):
            self.store.add(self.owner, entry, restore=True, claim=i == 0)
        self.root = tk.Tk()
        self.root.withdraw()
        self.patches = [patch.object(self.viewer, 'get_cameras', side_effect=lambda: self.cameras),
                        patch.object(self.viewer, 'HealthMonitor', side_effect=self.monitor),
                        patch.object(self.viewer, 'load_overrides', return_value={}),
                        patch('child_processes.subprocess.Popen', side_effect=self.popen)]
        for item in self.patches:
            item.start()
        self.app = self.viewer.CameraViewer(self.root)
        eventually(lambda: self.app.saved_session is not None, self.root)

    def monitor(self, *args):
        monitor = Mock()
        monitor.poll.return_value = None
        monitor.updates.return_value = []
        monitor.stdin = io.BytesIO()
        return monitor

    def popen(self, *args, **kwargs):
        process = Mock()
        process.poll.return_value = None
        process.returncode = None
        process.stdin = io.BytesIO()
        process.stdout = io.BytesIO()
        process.command = args[0]
        process.environment = kwargs['env']
        self.processes.append(process)
        return process

    def wait_launches(self, count):
        eventually(lambda: len(self.processes) == count, self.root)

    def tearDown(self):
        for process in self.app.children.processes:
            process.poll.return_value = 0
            process.returncode = 0
        self.app.on_closing()
        self.root.mainloop()
        for item in reversed(self.patches):
            item.stop()
        self.env.stop()
        self.folder.cleanup()
        gc.collect()

    def test_restore_multiplicity_muted_environment_and_repeated_clicks(self):
        self.app.restore_button.invoke()
        self.wait_launches(4)
        videos = [p for p in self.processes if p.environment['CAMERA_SESSION_MUTED'] == '1']
        self.assertEqual(len(videos), 1)
        self.assertIn('player_vilkin_hikvision.py', videos[0].command[1])
        self.assertEqual(len({p.environment['CAMERA_SESSION_WINDOW'] for p in self.processes}), 4)
        self.assertEqual(self.app.session_detail.cget('text'), '')
        self.assertEqual(self.app.session_detail.winfo_manager(), '')
        self.app.restore_session()
        self.app.restore_session()
        eventually(lambda: self.app.restore_task is None, self.root)
        self.assertEqual(len(self.processes), 4)

    def test_existing_window_is_reused_and_keeps_its_current_mute(self):
        self.app.play_camera_thread(self.cameras[0])
        self.wait_launches(1)
        environment = self.processes[0].environment
        self.store.report(self.app.session.owner, environment['CAMERA_SESSION_WINDOW'], muted=False)
        self.app.restore_session()
        self.wait_launches(4)
        existing = next(e for e in self.store.load() if e.window_id == environment['CAMERA_SESSION_WINDOW'])
        self.assertFalse(existing.muted)

    def test_removed_camera_and_disabled_ptz_do_not_block_other_windows(self):
        self.cameras = [self.cameras[0][:-1] + (0,)]
        self.app.restore_session()
        self.wait_launches(2)
        self.assertIn('2 camera(s)', self.app.session_detail.cget('text'))

    def test_failed_spawn_does_not_block_remaining_windows_or_disclose_credentials(self):
        real = self.popen
        first = True
        def failing(*args, **kwargs):
            nonlocal first
            if first:
                first = False
                raise OSError('fake-password and private host')
            return real(*args, **kwargs)
        with patch('child_processes.subprocess.Popen', side_effect=failing):
            self.app.restore_session()
            self.wait_launches(3)
        self.assertNotIn('fake-password', self.app.session_detail.cget('text'))
        self.assertEqual(len(self.store.load()), 4)

    def test_closing_viewer_freezes_instead_of_emptying_saved_session(self):
        self.app.restore_session()
        self.wait_launches(4)
        self.app.on_closing()
        self.app.session_final.result(3)
        for entry in self.store.load():
            self.store.report(self.app.session.owner, entry.window_id, closed=True)
        self.assertEqual(len(self.store.load()), 4)

    def test_child_exit_reports_failure_and_restore_retries_only_failed_window(self):
        self.app.restore_session()
        self.wait_launches(4)
        self.processes[0].poll.return_value = 1
        self.processes[0].returncode = 1
        self.app.restore_session()
        self.wait_launches(5)
        self.assertEqual(len(self.app.session_windows), 4)

    def test_no_session_message_and_idle_shutdown_preserve_startup_snapshot(self):
        self.app._restore_entries([])
        self.assertEqual(self.app.session_detail.cget('text'), 'No session to restore.')
        self.assertEqual(len(self.store.load()), 4)


class SessionPlacementTests(unittest.TestCase):
    def test_two_instances_restore_independent_geometry_and_fallback_to_legacy_layout(self):
        gc.collect()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'layout.db'
            screen = Screen('main', (0, 0, 1920, 1080), (0, 0, 1920, 1040), True)
            signature = screens_signature((screen,))
            store = PositionStore(path)
            store.save('1', 0, signature, 200, 120, 600, 400)
            store.save('video:1:1', 0, signature, 900, 220, 700, 500)
            roots, placements = [], []
            try:
                for slot in (0, 1):
                    root = tk.Tk()
                    root.geometry('800x600')
                    root.update_idletasks()
                    roots.append(root)
                    placements.append(WindowPlacement(root, 1, path=path,
                        screen_provider=lambda: (screen,), storage_id=f'video:1:{slot}', fallback_id='1'))
                for root, placement in zip(roots, placements):
                    eventually(lambda: placement.token is not None, root)
                    root.update_idletasks()
                self.assertEqual(placements[0]._geometry(), (600, 400, 200, 120))
                self.assertEqual(placements[1]._geometry(), (700, 500, 900, 220))
            finally:
                for placement in placements:
                    placement.close()
                    eventually(placement.finished)
                for root in roots:
                    root.destroy()
                gc.collect()


class SessionAudioTests(unittest.TestCase):
    def test_first_supervised_process_receives_saved_audio_before_playback(self):
        from player_supervisor import PlayerSupervisor, PlayerSettings
        gc.collect()
        with tempfile.TemporaryDirectory() as folder:
            probe = Path(folder) / 'audio.jsonl'
            owner = PlayerSupervisor(1, {'muted': True, 'volume': 65, 'audio_probe': str(probe)},
                1, PlayerSettings(stop_timeout=.1),
                worker_command=[sys.executable, '-u', str(Path(__file__).parent / 'helpers/player_process.py')],
                log_directory=folder)
            try:
                eventually(lambda: probe.exists() and probe.stat().st_size > 0)
                first = json.loads(probe.read_text().splitlines()[0])
                self.assertEqual(first, {'generation': 1, 'muted': True, 'volume': 65})
            finally:
                owner.close()
                self.assertTrue(owner.closed.wait(5))
                for handler in list(owner.logger.handlers):
                    handler.close()
                    owner.logger.removeHandler(handler)

    def test_native_player_requests_silence_before_play(self):
        from player_worker import Commands, run
        commands = Commands({'muted': True, 'volume': 65})
        vlc = Mock()
        vlc.__version__ = 'test'
        vlc.libvlc_get_version.return_value = b'test'
        vlc.dll._name = 'test'
        vlc.CallbackDecorators.LogCb = lambda fn: fn
        player = vlc.Instance.return_value.media_player_new.return_value
        def play():
            commands.stop.set()
            return 0
        player.play.side_effect = play
        run({'generation': 1, 'hwnd': 1, 'uri': 'rtsp://127.0.0.1/test'}, commands, vlc, Mock())
        calls = [c[0] for c in player.method_calls]
        self.assertLess(calls.index('audio_set_mute'), calls.index('play'))
        self.assertLess(calls.index('audio_set_volume'), calls.index('play'))
        player.audio_set_mute.assert_called_once_with(True)
        player.audio_set_volume.assert_called_once_with(0)

    def test_muted_restore_is_in_supervisor_config_before_start_and_button_is_red(self):
        from player_vilkin_hikvision import VideoPlayer
        gc.collect()
        session = Mock()
        session.entry = SessionEntry.create(1, 'video', muted=True)
        session.finished.return_value = True
        owner = Mock()
        owner.closed.is_set.return_value = True
        factory, placement = Mock(return_value=owner), Mock()
        placement.return_value.finished.return_value = True
        app = VideoPlayer(1, supervisor_factory=factory, placement_factory=placement, session=session)
        try:
            config = factory.call_args.args[1]
            self.assertTrue(config['muted'])
            self.assertEqual(app.mute_button.cget('background'), '#C98282')
            self.assertEqual(app.root.title(), 'Camera 1 (Muted)')
            app.toggle_mute()
            owner.set_audio.assert_called_once_with(False, 100)
            session.audio.assert_called_once_with(False)
            self.assertEqual(app.mute_button.cget('background'), '#C0C0C0')
        finally:
            app.on_closing()
            del app
            gc.collect()


class PTZSessionTests(unittest.TestCase):
    def test_restored_ptz_never_moves_or_recalls_presets_and_flushes_placement(self):
        """Run the real PTZ entry point with offline ONVIF services in a child."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'layout.db'
            entry = SessionEntry.create(1, 'ptz')
            owner = 'a'*32
            SessionStore(path).add(owner, entry, claim=True)
            code = '''
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch
import ptz_keyboard_control as ptz
camera, service = Mock(), Mock()
service.create_type.side_effect = lambda name: SimpleNamespace()
camera.create_ptz_service.return_value = service
camera.create_imaging_service.return_value = service
camera.create_media_service().GetProfiles.return_value = [SimpleNamespace(
    token='profile', VideoSourceConfiguration=SimpleNamespace(SourceToken='source'))]
real_tk = ptz.tk.Tk
def window():
    root = real_tk()
    root.after(450, lambda: ptz.parent_lifetime.closed.set())
    root.after(600, ptz.close_controller)
    root.after(5000, ptz.close_controller)
    return root
with patch.dict(sys.modules, {'onvif': SimpleNamespace(ONVIFCamera=Mock(return_value=camera))}):
    with patch.object(ptz.tk, 'Tk', window):
        assert ptz.main(['1', 'offline', 'fake-user', 'fake-password']) == 0
service.ContinuousMove.assert_not_called()
service.AbsoluteMove.assert_not_called()
service.RelativeMove.assert_not_called()
service.GotoPreset.assert_not_called()
service.GotoHomePosition.assert_not_called()
service.Move.assert_not_called()
assert ptz.placement.finished()
print('PTZ restoration stayed neutral and closed cleanly')
'''
            environment = dict(os.environ, **entry.environment(owner), CAMERA_WINDOW_POSITIONS_FILE=str(path))
            process = subprocess.run([sys.executable, '-c', code], env=environment,
                                     capture_output=True, text=True, timeout=12)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            self.assertIn('stayed neutral', process.stdout)
            self.assertEqual(len(SessionStore(path).load()), 1)


if __name__ == '__main__':
    unittest.main()
