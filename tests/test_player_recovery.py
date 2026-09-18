import io
import gc
import json
from pathlib import Path
from queue import Empty
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
from xml.etree import ElementTree as ET

from player_supervisor import PlayerSettings, PlayerSupervisor, Progress
from player_worker import Notifications, Commands, authenticated_uri, discover_uri, run


class ProgressTests(unittest.TestCase):
    def test_low_cadence_timeout_is_configurable_and_invalid_settings_are_safe(self):
        with patch.dict('os.environ', {'CAMERA_PLAYER_STALL_SECONDS': '60'}):
            settings = PlayerSettings.from_environment()
            self.assertEqual(settings.stall_timeout, 60)
            self.assertEqual(settings.startup_timeout, 120)
        for value in ('nan', 'inf', '-1', 'bad'):
            with patch.dict('os.environ', {'CAMERA_PLAYER_STALL_SECONDS': value}):
                self.assertEqual(PlayerSettings.from_environment().stall_timeout, 15)

    def test_receive_decode_and_display_stalls_are_distinct(self):
        for expected, sample in (
            ('no-input-progress', {'received': 1, 'decoded': 1, 'displayed': 1}),
            ('no-decode-progress', {'received': 100, 'decoded': 1, 'displayed': 1}),
            ('no-display-progress', {'received': 100, 'decoded': 100, 'displayed': 1})):
            progress = Progress(0, PlayerSettings())
            progress.observe({'received': 0, 'decoded': 0, 'displayed': 0}, 0)
            progress.observe({'received': 1, 'decoded': 1, 'displayed': 1}, 1)
            progress.observe(sample, 20)
            self.assertEqual(progress.failure(20), expected)

    def test_no_image_from_play_or_single_sample(self):
        progress = Progress(0, PlayerSettings())
        progress.observe({'displayed': 1, 'decoded': 1}, 1)
        self.assertIsNone(progress.playing_since)
        self.assertEqual(progress.failure(25), 'startup-no-video')

    def test_network_and_decode_without_presentation_are_not_recovery(self):
        progress = Progress(0, PlayerSettings())
        for second in range(26):
            progress.observe({'displayed': 0, 'received': second*100, 'decoded': second}, second)
        self.assertEqual(progress.failure(26), 'startup-no-video')

    def test_low_fps_has_no_false_recovery_and_stable_reset_is_delayed(self):
        progress = Progress(0, PlayerSettings())
        for second in range(40):
            progress.observe({'displayed': second//5, 'decoded': second//5}, second)
            self.assertIsNone(progress.failure(second))
            self.assertEqual(progress.stable(second), second >= 35)
        self.assertEqual(progress.failure(50), 'no-input-progress')

    def test_counter_reset_is_not_progress(self):
        progress = Progress(0, PlayerSettings())
        progress.observe({'displayed': 100, 'decoded': 100}, 1)
        progress.observe({'displayed': 0, 'decoded': 0}, 2)
        self.assertIsNone(progress.playing_since)

    def test_repeated_presentation_of_the_same_decoded_picture_is_not_live(self):
        progress = Progress(0, PlayerSettings())
        progress.observe({'decoded': 1, 'displayed': 1}, 0)
        for second in range(1, 26):
            progress.observe({'decoded': 1, 'displayed': second+1}, second)
        self.assertEqual(progress.failure(26), 'startup-no-video')

    def test_decode_and_presentation_can_advance_in_different_samples(self):
        progress = Progress(0, PlayerSettings())
        progress.observe({'decoded': 0, 'displayed': 0}, 0)
        progress.observe({'decoded': 1, 'displayed': 0}, 1)
        progress.observe({'decoded': 1, 'displayed': 1}, 2)
        self.assertEqual(progress.playing_since, 2)


class CallbackAndDiscoveryTests(unittest.TestCase):
    def test_argument_error_does_not_echo_a_credential_like_option(self):
        result = subprocess.run([sys.executable, 'player_vilkin_hikvision.py', '1', 'host',
                                 'user', '--PRIVATE_TEST_PASSWORD'], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn(b'PRIVATE_TEST_PASSWORD', result.stdout + result.stderr)

    def test_native_owner_never_reenters_from_callback_and_releases_in_order(self):
        calls, callbacks, messages = [], {}, []
        inside_callback = False
        owner = threading.get_ident()
        commands = Commands({'muted': True, 'volume': 42})

        def native(name, result=None):
            def action(*args):
                self.assertFalse(inside_callback, name)
                self.assertEqual(threading.get_ident(), owner)
                calls.append(name)
                return result
            return action

        def attach(event, callback):
            callbacks[event] = callback

        def play():
            nonlocal inside_callback
            inside_callback = True
            if mode == 'graphics':
                callbacks['log'](None, 4, None, b'SwapChain Present failed. (hr=0x%lX)', None)
            else:
                callbacks['error'](Mock())
            callbacks['ended'](Mock())
            inside_callback = False
            return 0

        manager = SimpleNamespace(event_attach=attach, event_detach=native('detach'))
        player = SimpleNamespace(set_media=native('set_media'), set_hwnd=native('hwnd'),
            event_manager=native('event_manager', manager), play=play, stop=native('stop'),
            release=native('release-player'))
        media = SimpleNamespace(release=native('release-media'))
        instance = SimpleNamespace(media_player_new=native('player', player), media_new=native('media', media),
            log_set=lambda callback, _: callbacks.update(log=callback),
            log_unset=native('log_unset'), release=native('release-instance'))
        vlc = SimpleNamespace(Instance=native('instance', instance), __version__='test',
            libvlc_get_version=lambda: b'test', dll=SimpleNamespace(_name='test'),
            CallbackDecorators=SimpleNamespace(LogCb=lambda fn: fn), EventType=SimpleNamespace(
                MediaPlayerEncounteredError='error', MediaPlayerEndReached='ended', MediaPlayerESDeleted='es'))
        config = {'generation': 1, 'hwnd': 123, 'uri': 'rtsp://127.0.0.1/test'}
        for mode in ('error', 'graphics'):
            with self.subTest(mode=mode):
                calls.clear()
                messages.clear()
                run(config, commands, vlc, messages.append)
                failures = [m for m in messages if m.get('kind') == 'failure']
                self.assertEqual(len(failures), 1)
                self.assertEqual(failures[0]['reason'], 'graphics-error' if mode == 'graphics' else 'vlc-error')
                self.assertLess(calls.index('detach'), calls.index('stop'))
                self.assertLess(calls.index('stop'), calls.index('release-player'))
                self.assertLess(calls.index('release-player'), calls.index('release-media'))
                self.assertLess(calls.index('release-media'), calls.index('release-instance'))

    def test_discovery_exception_body_and_credentials_are_not_emitted(self):
        messages = []
        with patch('player_worker.discover_uri', side_effect=RuntimeError('rtsp://private:secret@host/private')):
            run({'generation': 1}, Commands({}), emit=messages.append)
        self.assertNotIn('secret', json.dumps(messages))
        self.assertNotIn('private', json.dumps(messages))
        self.assertIn({'kind': 'failure', 'reason': 'worker-error'}, messages)

    def test_callback_does_not_touch_native_event_or_wait_on_full_queue(self):
        notifications = Notifications(7)
        callback = notifications.callback('vlc-error')
        event = Mock()
        for _ in range(10000):
            callback(event)
        self.assertEqual(event.mock_calls, [])
        self.assertEqual(notifications.queue.qsize(), 32)
        self.assertEqual(notifications.queue.get_nowait(), (7, 'vlc-error'))

    def test_uri_preserves_vendor_path_profile_port_query_and_ipv6(self):
        self.assertEqual(authenticated_uri('rtsp://[::1]:8554/custom/low?profile=2', 'a@b', 'x:/?#'),
                         'rtsp://a%40b:x%3A%2F%3F%23@[::1]:8554/custom/low?profile=2')
        uri = 'rtsp://existing:secret@host:8554/vendor'
        self.assertEqual(authenticated_uri(uri, 'other', 'other'), uri)

    def test_discovery_retains_first_profile_and_returns_complete_uri(self):
        from camera_health import SCHEMA, MEDIA
        responses = iter([
            ET.fromstring(f'<r xmlns:t="{SCHEMA}"><t:Media><t:XAddr>http://camera/media</t:XAddr></t:Media></r>'),
            ET.fromstring(f'<r xmlns:m="{MEDIA}"><m:Profiles token="one&amp;two"/><m:Profiles token="other"/></r>'),
            ET.fromstring(f'<r xmlns:t="{SCHEMA}"><t:Uri>rtsp://camera:8554/vendor?profile=main</t:Uri></r>')])
        request = Mock(side_effect=lambda *args: next(responses))
        uri = discover_uri({'host': 'camera', 'username': 'user', 'password': 'pass'}, threading.Event(), request)
        self.assertEqual(uri, 'rtsp://user:pass@camera:8554/vendor?profile=main')
        self.assertIn('one&amp;two', request.call_args.args[3])
        self.assertEqual(request.call_args.args[4], 2.)


class ProcessRecoveryTests(unittest.TestCase):
    def setUp(self):
        # Earlier GUI tests can leave Tcl/widget cycles after destroy(). Collect
        # them on their creator thread before this test starts any new workers.
        gc.collect()
        self.directory = tempfile.TemporaryDirectory()
        self.owners = []

    def tearDown(self):
        for owner in self.owners:
            owner.close()
            self.assertTrue(owner.closed.wait(5))
            self.assertIsNone(owner.worker_pid)
        # Release test log handles before removing their temporary directory.
        for owner in self.owners:
            for handler in list(owner.logger.handlers):
                handler.close()
                owner.logger.removeHandler(handler)
        self.directory.cleanup()
        gc.collect()

    def owner(self, config=None, **settings):
        options = dict(operation_timeout=.5, discovery_timeout=.5, stop_timeout=.1,
                       startup_timeout=2, stall_timeout=.5, stable_seconds=.5, backoff=(.05, .1, .2))
        options.update(settings)
        owner = PlayerSupervisor(len(self.owners)+900, config or {}, 1, PlayerSettings(**options),
            worker_command=[sys.executable, '-u', str(Path(__file__).parent / 'helpers/player_process.py')],
            log_directory=self.directory.name)
        self.owners.append(owner)
        return owner

    def wait_for(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for owner in self.owners:
                owner.heartbeat()
            if predicate():
                return
            time.sleep(.01)
        self.fail('Timed out waiting for player state')

    def test_real_process_ignores_old_generation_and_close_is_nonblocking(self):
        owner = self.owner({'block_stop': True})
        self.wait_for(lambda: owner.snapshot.state == 'PLAYING')
        self.assertEqual(owner.snapshot.generation, 1)
        start = time.monotonic()
        owner.close()
        self.assertLess(time.monotonic()-start, .05)
        self.wait_for(owner.closed.is_set)
        self.assertLess(time.monotonic()-start, 2)
        self.assertEqual(owner.snapshot.state, 'CLOSED')

    def test_hung_discovery_is_killed_before_next_generation(self):
        owner = self.owner({'block_discover': True, 'block_stop': True})
        self.wait_for(lambda: owner.snapshot.generation >= 3)
        self.assertGreaterEqual(owner.snapshot.attempt, 2)
        self.assertLessEqual(len([t for t in threading.enumerate() if t.name == 'VLC status pipe']), 1)

    def test_error_burst_has_one_retry_and_cancelled_backoff_never_restarts(self):
        owner = self.owner({'burst': True}, backoff=(10,))
        self.wait_for(lambda: owner.snapshot.state == 'RECONNECT_WAIT')
        self.assertEqual(owner.snapshot.attempt, 1)
        owner.close()
        self.wait_for(owner.closed.is_set)
        self.assertEqual(owner.snapshot.generation, 1)

    def test_one_hung_camera_does_not_interrupt_other_session(self):
        broken = self.owner({'block_discover': True, 'block_stop': True})
        healthy = self.owner()
        self.wait_for(lambda: broken.snapshot.generation >= 3 and healthy.snapshot.displayed >= 10)
        self.assertEqual(healthy.snapshot.generation, 1)

    def test_mute_and_volume_are_applied_to_the_replacement_process(self):
        from player_vilkin_hikvision import VideoPlayer
        path = Path(self.directory.name) / 'audio.jsonl'
        owner = self.owner({'fail_first': True, 'audio_probe': str(path)})
        app = VideoPlayer('mute-test', supervisor_factory=lambda *args: owner)
        app.root.withdraw()
        try:
            self.wait_for(lambda: owner.snapshot.state == 'PLAYING')
            app.set_volume(42)
            app.mute_button.invoke()
            self.wait_for(lambda: owner.snapshot.generation == 2 and owner.snapshot.state == 'PLAYING')
            app.root.update()
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(records[-1], {'generation': 2, 'muted': True, 'volume': 42})
            self.assertEqual(app.mute_button.cget('image'), str(app.volume_mute_icon))
            self.assertEqual(app.mute_button.cget('relief'), 'sunken')
            self.assertEqual(app.root.title(), 'Camera mute-test (Muted)')
            app.mute_button.invoke()
            # Only consume complete lines while the worker appends observations.
            self.wait_for(lambda: json.loads(path.read_text().split('\n')[-2])['muted'] is False)
            self.assertEqual(app.mute_button.cget('image'), str(app.volume_up_icon))
            self.assertEqual(app.mute_button.cget('relief'), 'flat')
            self.assertEqual(app.root.title(), 'Camera mute-test')
            self.assertEqual(owner._audio, (False, 42))
        finally:
            app.on_closing()
            app.root.mainloop()

    def test_viewer_pipe_eof_closes_real_player_window_and_its_blocked_worker(self):
        code = """
import sys
from pathlib import Path
from child_processes import parent_lifetime
from player_vilkin_hikvision import VideoPlayer
from player_supervisor import PlayerSupervisor, PlayerSettings
def supervisor(camera_id, config, hwnd, settings):
    config['block_stop'] = True
    return PlayerSupervisor(camera_id, config, hwnd, PlayerSettings(stop_timeout=.1),
        worker_command=[sys.executable, '-u', str(Path('tests/helpers/player_process.py').resolve())],
        log_directory=sys.argv[1])
app = VideoPlayer('pipe-test', supervisor_factory=supervisor)
app.root.withdraw()
app.root.after(150, lambda: print('ready', flush=True))
app.root.after(5000, app.on_closing)
parent_lifetime.bind(app.root, app.on_closing)
app.run()
assert app.supervisor.closed.is_set() and app.supervisor.worker_pid is None
print('reaped', flush=True)
"""
        import os
        process = subprocess.Popen([sys.executable, '-u', '-c', code, self.directory.name],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=dict(os.environ, CAMERA_PARENT_PIPE='1'))
        try:
            while b'ready' not in process.stdout.readline():
                if process.poll() is not None:
                    self.fail(process.stderr.read().decode())
            start = time.monotonic()
            process.stdin.close()
            process.wait(3)
            self.assertLess(time.monotonic()-start, 2)
            self.assertEqual(process.returncode, 0, process.stderr.read().decode())
            self.assertIn(b'reaped', process.stdout.read())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(2)
            process.stdout.close()
            process.stderr.close()

    def test_real_tk_remains_responsive_during_hung_stop(self):
        from player_vilkin_hikvision import VideoPlayer
        owner = self.owner({'block_stop': True})
        app = VideoPlayer('test', supervisor_factory=lambda *args: owner)
        app.root.withdraw()
        beats = []
        def beat():
            beats.append(time.monotonic())
            app.root.after(10, beat)
        app.root.after(10, beat)
        app.root.after(300, app.mute_button.invoke)
        app.root.after(400, app.on_closing)
        app.root.after(5000, app.on_closing)
        app.run()
        self.assertTrue(owner.closed.is_set())
        self.assertGreater(len(beats), 25)
        self.assertLess(max(b-a for a, b in zip(beats, beats[1:])), 1)
        self.assertEqual(owner._audio, (True, 100))

    def test_worker_error_can_exit_while_command_pipe_remains_open(self):
        process = subprocess.Popen([sys.executable, '-u', 'player_worker.py'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            config = {'generation': 1, 'uri': 'invalid://redacted', 'hwnd': 0}
            process.stdin.write((json.dumps(config)+'\n').encode())
            process.stdin.flush()
            process.wait(3)  # Do not close stdin: exercise interpreter finalization.
            errors = process.stderr.read()
            self.assertEqual(process.returncode, 0, errors.decode())
            self.assertEqual(errors, b'')
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(2)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()

    def test_parent_eof_kills_a_native_owner_that_never_returns(self):
        code = """
import threading, time
from player_worker import Commands
commands = Commands({})
threading.Thread(target=commands.read, daemon=True).start()
print('ready', flush=True)
while True:
    time.sleep(60)
"""
        process = subprocess.Popen([sys.executable, '-u', '-c', code],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertEqual(process.stdout.readline().strip(), b'ready')
            process.stdin.close()
            process.wait(4)
            self.assertEqual(process.returncode, 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(2)
            process.stdout.close()
            process.stderr.close()


if __name__ == '__main__':
    unittest.main()
