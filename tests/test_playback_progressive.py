"""User-run regressions. No Tk window, camera, FFmpeg or libVLC is started."""
from dataclasses import asdict, replace
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from playback.config import Settings
from playback.controller import Controller, Status
from playback.engine import Engine, NativeSnapshot
from playback.media import PrefixResult, Segment, media_info, mp4_prefix, probe_prefix
from playback.model import Controls, PlaybackError, Viewport
from playback.native_worker import FrameGate, run as native_run
from playback.progressive import ProgressivePreparation
from playback.server import Playlist
from playback.ui import PlaybackWindow
from playback.presentation import ACTIONS, MONTHS, WEEKDAYS, TEXT, error_text
from tools.summarize_playback_events import summarize


def metadata(container='mpeg'):
    return {'format': {'format_name': container, 'start_time': '0'},
            'streams': [{'codec_type': 'video', 'codec_name': 'h264', 'width': 640, 'height': 360}]}


def box(kind, payload=b''):
    return (len(payload)+8).to_bytes(4, 'big')+kind+payload


def controller():
    c = Controller.__new__(Controller)
    c.request = c.active_request = (1, 3, 100.)
    c.serial, c.seek_serial = 1, 0
    c.seek_target = c.sent_seek = None
    c.open_seek_token = None
    c.session_ranges = ((100., 300.),)
    c.preview_position = 105.
    c.cancel, c.stop_event, c.playlist_changing = threading.Event(), threading.Event(), threading.Event()
    c.playback_lock = threading.RLock()
    c.controls = Controls(rate=2., paused=True, muted=False, volume=37)
    c.status = Status('PLAYING', 3, 105., key='archive')
    c.server, c.log = Mock(), Mock()
    c.server.publish.return_value = 'http://127.0.0.1/example'
    c.active_playlist = Playlist(c.server, 100.)
    c.active_playlist.update('archive', segments(2))
    c.engine = Engine.__new__(Engine)
    c.engine.serial = 40
    c.engine.request = (40, c.active_playlist.url, 0.)
    c.engine.seek_request = None
    c.engine.snapshot = NativeSnapshot('PAUSED', 40, 5., 2., decoded=100, displayed=95)
    c.engine.controls = c.controls
    c.engine.event = Mock()
    c.engine.idle = threading.Event()
    c.engine.idle.set()
    c.engine.open = Mock(wraps=c.engine.open)
    c.engine.stop = Mock(wraps=c.engine.stop)
    return c


def segments(count, duration=20.):
    return tuple(Segment(Path(f'segment-{i:05d}.ts'), 100.+i*duration, duration, 'archive') for i in range(count))


class PrefixTests(unittest.TestCase):
    def test_prefix_accepts_mpeg_without_global_duration_but_full_probe_requires_it(self):
        info = media_info(metadata(), complete=False)
        self.assertIsNone(info['duration'])
        self.assertEqual(info['video'], 'h264')
        with self.assertRaisesRegex(PlaybackError, 'media-invalid'):
            media_info(metadata(), complete=True)

    def test_prefix_requires_video_dimensions_and_finite_timestamps(self):
        data = metadata()
        data['streams'][0]['width'] = 0
        with self.assertRaisesRegex(PlaybackError, 'prefix-insufficient'):
            media_info(data, complete=False)
        data = metadata()
        data['format']['start_time'] = 'nan'
        with self.assertRaises(PlaybackError):
            media_info(data, complete=False)

    def test_mp4_metadata_layout_is_structural_not_a_byte_search(self):
        self.assertEqual(mp4_prefix(box(b'ftyp')+box(b'mdat', b'moov')), 'mp4-metadata-after-media')
        self.assertEqual(mp4_prefix(box(b'ftyp')+box(b'moov')+box(b'mdat')), '')
        self.assertEqual(mp4_prefix(box(b'ftyp')+box(b'moov', b'x')[:-1]), 'prefix-insufficient')
        self.assertEqual(mp4_prefix(box(b'ftyp')+box(b'moov')+box(b'moof')), '')

    def test_rejects_html_xml_json_before_any_media_process(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'prefix'
            for data in (b'<html>error</html>', b'\xef\xbb\xbf  <ResponseStatus/>', b'{"error":1}'):
                path.write_bytes(data)
                with patch('playback.media.run') as run:
                    with self.assertRaisesRegex(PlaybackError, 'invalid-media-response'):
                        probe_prefix(path, Settings(), threading.Event(), 262144)
                    run.assert_not_called()

    def test_mpeg_prefix_uses_a_finite_snapshot_without_duration(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'prefix'
            path.write_bytes(b'\x00\x00\x01\xba'+b'\x00'*300000)
            with patch('playback.media.run', return_value=(json.dumps(metadata()).encode(), 0)) as run:
                result = probe_prefix(path, Settings(), threading.Event(), 262144)
            self.assertIsNotNone(result.media)
            self.assertEqual(len(run.call_args.kwargs['input_chunks'][0]), 262144)
            self.assertEqual(run.call_args.kwargs['timeout'], 8)
            self.assertTrue(run.call_args.kwargs['allow_early_input_close'])

    def test_insufficient_probe_retries_only_after_more_received_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'prefix'
            path.write_bytes(b'x'*8)
            attempts, events = [], []
            def inspect(*args):
                attempts.append(path.stat().st_size)
                if len(attempts) == 1:
                    with path.open('ab') as out:
                        out.write(b'x'*8)
                    return PrefixResult(reason='prefix-insufficient', retry=True)
                return PrefixResult(media=media_info(metadata(), complete=False))
            produce = Mock()
            job = ProgressivePreparation(path, Settings(), threading.Event(), threading.Event(),
                threading.Event(), produce, lambda name, **values: events.append((name, values)))
            with patch('playback.progressive.PREFIX_STEPS', (8, 16)), patch('playback.progressive.probe_prefix', side_effect=inspect):
                job._run()
            self.assertEqual(attempts, [8, 16])
            produce.assert_called_once()
            self.assertTrue(job.complete)
            self.assertIn('prefix-insufficient', [name for name, _ in events])

    def test_mp4_fallback_has_visible_reason_and_does_not_start_producer(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'prefix'
            path.write_bytes(b'x'*8)
            events, produce = [], Mock()
            job = ProgressivePreparation(path, Settings(), threading.Event(), threading.Event(), threading.Event(),
                produce, lambda event, **values: events.append((event, values)))
            with patch('playback.progressive.PREFIX_STEPS', (8,)), patch('playback.progressive.probe_prefix',
                    return_value=PrefixResult(reason='mp4-metadata-after-media')):
                job._run()
            produce.assert_not_called()
            self.assertEqual(events[-1], ('mode-selected', {'mode':'complete','reason':'mp4-metadata-after-media'}))
            self.assertIn('waiting for the complete file', error_text(job.fallback))

    def test_failure_after_publishing_does_not_overwrite_playing_segments(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'prefix'
            path.write_bytes(b'x'*8)
            events = []
            job = ProgressivePreparation(path, Settings(), threading.Event(), threading.Event(), threading.Event(),
                Mock(side_effect=PlaybackError('preparation-incomplete')), lambda event, **kw: events.append((event,kw)), lambda:True)
            with patch('playback.progressive.PREFIX_STEPS', (8,)), patch('playback.progressive.probe_prefix',
                    return_value=PrefixResult(media=metadata())):
                job._run()
            self.assertEqual(events[-1][0], 'producer-failed')
            self.assertFalse(job.complete)

    def test_cancelled_prefix_never_starts_a_producer(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'prefix'
            path.write_bytes(b'')
            cancel, produce = threading.Event(), Mock()
            cancel.set()
            job = ProgressivePreparation(path, Settings(), cancel, threading.Event(), threading.Event(), produce, Mock())
            job._run()
            self.assertEqual(job.error, 'cancelled')
            produce.assert_not_called()


class ProgressivePlaylistTests(unittest.TestCase):
    def test_long_first_gop_is_published_before_finalization(self):
        c = controller()
        c.engine.request = None
        c.active_playlist = Playlist(c.server, 100.)
        c._publish_segments(c.active_playlist, 'archive', segments(1, 22.1), 100., c.request, False)
        self.assertEqual(c.active_playlist.target_duration, 23)
        self.assertTrue(c.active_playlist.url)
        c.engine.open.assert_called_once()
        self.assertFalse(c.active_playlist.closed)

    def test_later_long_gop_uses_new_url_without_rewriting_old_target(self):
        c = controller()
        old_id = c.active_playlist.identifier
        before = c.active_playlist.segments
        later = (*before, Segment(Path('segment-00002.ts'), 140., 25.2, 'archive'))
        c._publish_segments(c.active_playlist, 'archive', later, 100., c.request, False)
        self.assertNotEqual(c.active_playlist.identifier, old_id)
        self.assertEqual(c.active_playlist.target_duration, 26)
        c.engine.stop.assert_called_once()
        self.assertEqual(c.active_playlist.segments, later)
        self.assertTrue(all(s.duration <= c.active_playlist.target_duration for s in later))

    def test_active_playlist_rejects_a_larger_segment_without_rebase(self):
        playlist = controller().active_playlist
        with self.assertRaisesRegex(PlaybackError, 'long-gop-progressive'):
            playlist.update('archive', segments(3, 30.))
        self.assertEqual(playlist.target_duration, 20)

    def test_initial_reserve_is_separate_from_comfort_prefetch(self):
        c = controller()
        c.engine.request = None
        c.controls = replace(c.controls, rate=4.)
        c.active_playlist = Playlist(c.server, 100.)
        c.active_playlist.update('archive', segments(1, 8.1))
        c._maybe_open(c.active_playlist, 100., c.request)
        c.engine.open.assert_called_once()
        self.assertEqual(Settings().reserve_seconds, 120)


class SeekTests(unittest.TestCase):
    def test_latest_local_seek_keeps_session_generation_download_and_controls(self):
        c = controller()
        request, controls = c.request, c.controls
        for stamp in (110., 130., 115., 120.):
            c.seek(3, stamp, preview=True)
        c._service_seek()
        self.assertEqual(c.engine.seek_request['offset'], 20.)
        self.assertEqual(c.request, request)
        self.assertEqual(c.engine.request[0], 40)
        self.assertFalse(c.cancel.is_set())
        self.assertEqual(c.controls, controls)
        c.engine.open.assert_not_called()
        c.engine.stop.assert_not_called()
        c.server.clear.assert_not_called()

    def test_same_archive_unreceived_target_waits_without_cancelling_download(self):
        c = controller()
        c.seek(3, 170., preview=True)
        c._service_seek()
        self.assertIsNone(c.engine.seek_request['offset'])
        self.assertEqual(c.view.state, 'PREVIEW_LOADING')
        c.active_playlist.update('archive', segments(4))
        c._service_seek()
        self.assertEqual(c.engine.seek_request['offset'], 70.)
        self.assertFalse(c.cancel.is_set())
        self.assertEqual(c.request[0], 1)

    def test_preview_across_camera_does_not_open_many_sessions(self):
        c = controller()
        c.seek(7, 110., preview=True)
        c._service_seek()
        self.assertEqual(c.request[1], 3)
        self.assertEqual(c.view.camera_id, 7)
        self.assertEqual(c.view.state, 'PREVIEW_LOADING')
        c.seek(7, 120., preview=False)
        c._service_seek()
        self.assertEqual(c.request[1:], (7, 120.))
        self.assertTrue(c.cancel.is_set())

    def test_release_requires_fresh_frame_for_final_target(self):
        c = controller()
        c.seek(3, 115., preview=True)
        c._service_seek()
        previous = c.engine.seek_request['id']
        c.engine.snapshot = replace(c.engine.snapshot, position=15., confirmed_seek=previous)
        self.assertEqual(c.view.state, 'PREVIEW')
        c.seek(3, 120., preview=False)
        c._service_seek()
        final = c.engine.seek_request['id']
        self.assertNotEqual(final, previous)
        self.assertEqual(c.view.state, 'SEEKING')
        c.engine.snapshot = replace(c.engine.snapshot, position=20., confirmed_seek=final)
        self.assertEqual(c.view.state, 'PAUSED')
        self.assertIsNone(c.seek_target)
        self.assertTrue(c.controls.paused)
        self.assertFalse(c.controls.muted)
        self.assertEqual((c.controls.rate, c.controls.volume), (2., 37))

    def test_cumulative_counters_do_not_confirm_a_seek(self):
        gate = FrameGate(20., (500, 450))
        self.assertFalse(gate.accepts(20., (500, 450)))
        self.assertFalse(gate.accepts(20., (501, 450)))
        self.assertFalse(gate.accepts(15., (501, 451)))
        self.assertTrue(gate.accepts(20.1, (501, 451)))
        self.assertFalse(gate.accepts(20., (1, 1)))
        self.assertTrue(gate.accepts(20.1, (2, 2)))

    def test_stopping_clears_pending_seek(self):
        c = controller()
        c.seek(3, 120., preview=True)
        c.stop()
        c._service_seek()
        self.assertIsNone(c.seek_target)
        self.assertIsNone(c.request)
        self.assertTrue(c.cancel.is_set())

    def test_failed_target_is_not_automatically_reopened_by_mailbox(self):
        c = controller()
        c.seek(7, 120.)
        c._service_seek()
        opened = c.request
        c.active_playlist = c.active_request = None
        c.status = Status('ERROR', 7, 120., reason='no-video')
        for _ in range(5):
            c._service_seek()
        self.assertEqual(c.request, opened)
        c.seek(7, 120.)  # Only an explicit new request retries.
        c._service_seek()
        self.assertGreater(c.request[0], opened[0])

    def test_stale_native_generation_is_not_evidence_for_new_request(self):
        c = controller()
        self.assertFalse(c._native_event('native-new-frame', generation=39))
        c.log.event.assert_not_called()
        self.assertTrue(c._native_event('native-new-frame', generation=40))
        self.assertEqual(c.log.event.call_args.kwargs['session_id'], 1)

    def test_continuous_drag_updates_before_release_at_bounded_cadence(self):
        ui = PlaybackWindow.__new__(PlaybackWindow)
        ui.view = Viewport(100., 100.)
        ui.timeline = Mock()
        ui.timeline.winfo_width.return_value = 158
        ui.window = Mock()
        ui.window.after.return_value = 'pending'
        ui.controller = Mock()
        ui.camera_id, ui.initialized, ui.dragging, ui.closing = 3, True, True, False
        ui.seek_timer = None
        ui._draw_timeline = Mock()
        for second in range(3):
            for x in range(10, 90):
                ui._drag(SimpleNamespace(x=x+58))
            self.assertEqual(ui.window.after.call_count, second+1)
            ui._preview_seek()  # The scheduled callback runs while still holding the mouse.
            self.assertTrue(ui.dragging)
        self.assertEqual(ui.controller.seek.call_count, 3)
        self.assertEqual(ui.controller.seek.call_args.args, (3, 189.))
        self.assertEqual(ui.controller.seek.call_args.kwargs, {'preview':True})
        ui._drag_end(SimpleNamespace(x=78))
        self.assertFalse(ui.dragging)
        self.assertEqual(ui.controller.seek.call_args.args, (3, 120.))
        self.assertEqual(ui.controller.seek.call_args.kwargs, {})


class PresentationAndEvidenceTests(unittest.TestCase):
    def test_english_surface_and_explicit_calendar(self):
        self.assertEqual(ACTIONS['archives'][1], 'Recordings')
        self.assertEqual(MONTHS[8], 'September')
        self.assertEqual(WEEKDAYS[0], 'Mo')
        self.assertIn('HH:mm:ss', TEXT['time_format'])
        self.assertIn('Code:', error_text('future-code'))
        for filename in ('ui.py', 'presentation.py', 'widgets.py'):
            text = (Path(__file__).resolve().parent.parent/'playback'/filename).read_text(encoding='utf-8-sig')
            for old in ('Enregistrements', 'Réglages', 'Téléchargement', 'Saisissez', '0,5×'):
                self.assertNotIn(old, text)

    def test_native_and_screen_evidence_are_distinct_and_timed(self):
        rows = [dict(event=name, monotonic=10.+offset, camera_id=3, generation=1)
                for name,offset in [('request',0),('native-new-frame',2),('screen-frame-observed',3),('download-complete',20)]]
        result = summarize(rows, 3)[0]
        self.assertTrue(result['native_frame_before_download_end'])
        self.assertTrue(result['user_observation_before_download_end'])
        self.assertIsNone(summarize([r for r in rows if r['event'] != 'screen-frame-observed'], 3)[0]['user_observation_before_download_end'])


class NativeCommandTests(unittest.TestCase):
    def test_preview_mutes_freezes_and_restores_pause_rate_and_audio_after_final_frame(self):
        controls = asdict(Controls(rate=2., paused=True, muted=False, volume=37))
        position, frames, tick = [0.], [0], [-1]
        muted, paused, rates, volumes = [], [], [], []
        state = ['Playing']
        commands = SimpleNamespace(controls=controls, seek=None, stop=Mock())
        def wait(_seconds):
            tick[0] += 1
            if tick[0] in (2, 4, 6):
                commands.seek = {'id': tick[0], 'offset': {2:20.,4:30.,6:25.}[tick[0]], 'preview':tick[0] != 6}
            return tick[0] >= 8
        commands.stop.wait.side_effect = wait
        def set_time(value):
            position[0] = value/1000
            frames[0] += 1
        def set_pause(value):
            paused.append(value)
            state[0] = 'Paused' if value else 'Playing'
        def stats(value):
            value.decoded_video = value.displayed_pictures = frames[0]
            value.played_abuffers = 0
            return True
        media = SimpleNamespace(add_option=Mock(), get_stats=stats, release=Mock())
        player = SimpleNamespace(set_media=Mock(), set_hwnd=Mock(),
            audio_set_mute=lambda value:muted.append((tick[0],value)),
            audio_set_volume=lambda value:volumes.append(value),
            set_rate=lambda value:rates.append(value) or 0, get_rate=lambda:2.,
            play=lambda:0, set_pause=set_pause, get_state=lambda:state[0],
            set_time=set_time, get_time=lambda:position[0]*1000, stop=Mock(), release=Mock())
        instance = SimpleNamespace(media_player_new=lambda:player, media_new=lambda _url:media, release=Mock())
        vlc = SimpleNamespace(__version__='synthetic', libvlc_get_version=lambda:b'synthetic',
            Instance=lambda *_args:instance, MediaStats=SimpleNamespace,
            State=SimpleNamespace(Playing='Playing',Paused='Paused',Ended='Ended',Error='Error'))
        output = io.StringIO()
        with patch.dict('sys.modules', {'vlc':vlc}), patch('sys.stdout', output):
            native_run({'generation':1,'url':'http://127.0.0.1/demo.m3u8','offset':0.,'hwnd':0,
                        'controls':controls}, commands)
        samples = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertFalse(any(row.get('kind') == 'failure' for row in samples))
        self.assertTrue(all(value for step,value in muted if 2 <= step <= 6))
        self.assertEqual(muted[-1], (7,False))
        self.assertEqual(paused[-1], 1)
        self.assertEqual(set(rates), {2.})
        self.assertEqual(set(volumes), {37})
        self.assertTrue(any(row.get('confirmed_seek') == 6 and row.get('frame_confirmed') for row in samples))
        self.assertEqual(position[0], 25.)


if __name__ == '__main__':
    unittest.main()
