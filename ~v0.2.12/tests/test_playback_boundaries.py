"""User-run offline regressions; no native media process or camera."""
from dataclasses import replace
import io
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from playback.controller import Controller, Status
from playback.config import Settings
from playback.engine import Engine, NativeSnapshot
from playback.media import Segment, read_segments
from playback.model import Controls, PlaybackError
from playback.native_worker import FrameGate
from playback.server import Playlist
from playback.timestamps import CLOCK, WRAP, TransportResource, pts, put_pts, translate
from tools.summarize_playback_events import summarize


def packet(timestamp, stream=0xe0, dts=None):
    data = bytearray(b'\xff'*188)
    data[:4] = b'\x47\x41\x00\x10'
    data[4:13] = b'\x00\x00\x01'+bytes((stream, 0, 0, 0x80, 0xc0 if dts is not None else 0x80, 10 if dts is not None else 5))
    data[13] = 0x31 if dts is not None else 0x21
    put_pts(data, 13, round(timestamp*CLOCK))
    if dts is not None:
        data[18] = 0x11
        put_pts(data, 18, round(dts*CLOCK))
    return bytes(data)


class TransportTimingTests(unittest.TestCase):
    def test_translation_preserves_audio_video_spacing_and_dts(self):
        source = packet(1.528, dts=1.448)+packet(1.4, 0xc0)
        shifted = translate(source, 600*CLOCK)
        self.assertAlmostEqual(pts(shifted[13:18])/CLOCK, 601.528)
        self.assertAlmostEqual(pts(shifted[18:23])/CLOCK, 601.448)
        self.assertAlmostEqual(pts(shifted[201:206])/CLOCK, 601.4)
        self.assertEqual(shifted[23:188], source[23:188])

    def test_timestamp_wrap_is_modular(self):
        data = packet((WRAP-45000)/CLOCK)
        self.assertEqual(pts(translate(data, CLOCK)[13:18]), 45000)

    def test_pcr_and_extension_are_shifted_without_touching_payload(self):
        data = bytearray(b'\xff'*188)
        data[:6] = bytes((0x47, 0, 0x100 & 255, 0x20, 183, 0x10))
        data[6:12] = bytes((0, 0, 0, 0, 0x7e, 17))
        shifted = translate(bytes(data), CLOCK)
        pcr = shifted[6] << 25 | shifted[7] << 17 | shifted[8] << 9 | shifted[9] << 1 | shifted[10] >> 7
        self.assertEqual(pcr, CLOCK)
        self.assertEqual(shifted[10] & 0x7f, 0x7e)
        self.assertEqual(shifted[11:], data[11:])

    def test_unaligned_ranges_match_full_response_and_original_is_untouched(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'segment.ts'
            raw = packet(1.4)*400
            path.write_bytes(raw)
            resource = TransportResource(path, CLOCK*20)
            with path.open('rb') as handle:
                full = b''.join(resource.chunks(handle, 0, len(raw)-1))
            for start, end in ((1, 200), (190, 900), (187, 188), (65520, len(raw)-1)):
                with path.open('rb') as handle:
                    self.assertEqual(b''.join(resource.chunks(handle, start, end)), full[start:end+1])
            self.assertEqual(path.read_bytes(), raw)

    def test_actual_packet_gap_is_not_replaced_by_extinf_sum(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory/'segment-00000.ts').write_bytes(packet(1.4))
            (directory/'segment-00001.ts').write_bytes(packet(7.4))
            (directory/'source.m3u8').write_text('#EXTM3U\n#EXTINF:4,\nsegment-00000.ts\n#EXTINF:4,\nsegment-00001.ts\n#EXT-X-ENDLIST\n')
            segments, complete = read_segments(directory, SimpleNamespace(start=100., key='a'), timing=True)
            self.assertTrue(complete)
            self.assertAlmostEqual(segments[1].start, 106.)
            playlist = Playlist(Mock(), 100.)
            playlist.update('a', segments)
            with self.assertRaisesRegex(PlaybackError, 'no-video'):
                playlist.media_offset(105.)

    def test_two_native_files_share_clock_without_rewriting_either(self):
        server = Mock()
        server.publish.return_value = 'loopback'
        playlist = Playlist(server, 102.)
        playlist.update('a', (Segment(Path('segment-00000.ts'), 100., 4., 'a', first_pts=1.528),))
        identifier = playlist.identifier
        playlist.update('b', (Segment(Path('segment-00000.ts'), 104., 4., 'b', first_pts=1.528),))
        resources = [call.args[1] for call in server.publish.call_args_list if isinstance(call.args[1], TransportResource)]
        self.assertAlmostEqual(pts(translate(packet(1.528), resources[-1].ticks)[13:18])/CLOCK, 14.)
        self.assertEqual(playlist.identifier, identifier)
        self.assertFalse(playlist.closed)


class FrameEvidenceTests(unittest.TestCase):
    def test_late_counters_after_observed_landing_confirm_advancing_target(self):
        gate = FrameGate(100., (10, 10), started=0., rate=2.)
        self.assertFalse(gate.accepts(100.1, (10, 10), now=.1))
        self.assertTrue(gate.accepts(103., (60, 35), now=1.6))

    def test_missing_landing_requires_plausible_forward_samples(self):
        gate = FrameGate(100., (10, 10), started=0., rate=2.)
        self.assertFalse(gate.accepts(103., (60, 35), now=1.6))
        self.assertTrue(gate.accepts(104., (80, 55), now=2.1))

    def test_wrong_position_never_confirmed_by_fresh_counters_alone(self):
        gate = FrameGate(100., (10, 10), started=0., rate=1.)
        self.assertFalse(gate.accepts(500., (60, 35), now=1.))
        self.assertFalse(gate.accepts(501., (80, 55), now=2.))

    def test_unavailable_and_reset_statistics_need_a_new_baseline(self):
        gate = FrameGate(100., (500, 400), started=0., baseline_valid=False)
        self.assertFalse(gate.accepts(100., (0, 0), now=.1, valid=False))
        self.assertFalse(gate.accepts(100.1, (50, 40), now=.2))
        self.assertFalse(gate.accepts(100.2, (1, 1), now=.3))
        self.assertTrue(gate.accepts(100.3, (2, 2), now=.4))

    def test_old_seek_is_retired_on_new_native_generation(self):
        engine = Engine.__new__(Engine)
        engine.serial, engine.request = 1, (1, 'old', 0.)
        engine.seek_request, engine.event, engine.idle = None, Mock(), threading.Event()
        engine.seek(15.)
        old = engine.seek_request
        generation = engine.open('new', 20., origin='ended-recovery')
        self.assertIsNone(engine.seek_request)
        self.assertNotEqual(generation, old['generation'])


class ChronologyTests(unittest.TestCase):
    def test_legacy_error_without_session_is_retained_and_marked(self):
        rows = [dict(event='request', monotonic=1., camera_id=2, generation=4),
                dict(event='error', monotonic=22., camera_id=2, reason='seek-unavailable')]
        result = summarize(rows, 2)[0]
        self.assertEqual(result['terminal_errors'][0]['reason'], 'seek-unavailable')
        self.assertTrue(result['terminal_errors'][0]['session_inferred'])

    def test_prefetch_download_does_not_count_as_first_archive_download(self):
        rows = [dict(event='request', monotonic=1., camera_id=1, generation=4),
                dict(event='native-new-frame', monotonic=3., camera_id=1, session_id=4, archive_id='aaaaaaaaaaaa'),
                dict(event='download-complete', monotonic=5., camera_id=1, session_id=4, archive_id='bbbbbbbbbbbb')]
        result = summarize(rows, 1)[0]
        self.assertIsNone(result['archives']['aaaaaaaaaaaa']['native_frame_before_download_end'])
        self.assertIsNone(result['archives']['bbbbbbbbbbbb']['native_frame_before_download_end'])

    def test_overlapping_session_events_stay_with_their_owner(self):
        rows = [dict(event='request', monotonic=1., camera_id=1, generation=1),
                dict(event='request', monotonic=2., camera_id=1, generation=2),
                dict(event='error', monotonic=3., camera_id=1, session_id=1, reason='cancelled')]
        result = summarize(rows, 1)
        self.assertEqual(len(result[0]['terminal_errors']), 1)
        self.assertEqual(result[1]['terminal_errors'], [])


class PrefetchTests(unittest.TestCase):
    def test_next_native_archive_starts_before_first_download_finishes(self):
        c = Controller.__new__(Controller)
        c.request = (1, 3, 130.)
        c.cameras = (SimpleNamespace(camera_id=3),)
        c.controls, c.settings = Controls(), Settings()
        c.status = Status('LOADING', 3, 130.)
        c.cancel, c.stop_event, c.playlist_changing = threading.Event(), threading.Event(), threading.Event()
        c.playback_lock = threading.RLock()
        c.seek_target = c.sent_seek = c.export_request = None
        c.log, c.store, c.server, c.engine = Mock(), Mock(), Mock(), Mock()
        c.server.drained = threading.Event(); c.server.drained.set()
        c.engine.snapshot = NativeSnapshot()
        c.engine.request = c.engine.seek_request = None
        c.engine.idle = threading.Event(); c.engine.idle.set()
        first = SimpleNamespace(recording=SimpleNamespace(key='a', start=100., device='device', track='1'), end=140.)
        second = SimpleNamespace(recording=SimpleNamespace(key='b', start=140., device='device', track='1'), end=180.)
        c.store.entries.return_value = (first, second)
        c.store.entry.side_effect = lambda key: first if key == 'a' else second
        c._find = Mock(return_value=first)
        second_started, first_finished = threading.Event(), threading.Event()
        order = []
        def prepare(entry, *_args):
            if entry is first:
                if second_started.wait(3):
                    order.append('first-finished')
                first_finished.set()
            else:
                order.append('second-started')
                second_started.set()
                first_finished.wait(3)
                c.request = None
        c._prepare = prepare
        thread = threading.Thread(target=c._session, args=(c.request,), daemon=True)
        thread.start()
        try:
            self.assertTrue(second_started.wait(3))
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(order, ['second-started', 'first-finished'])
        finally:
            c.stop_event.set(); c.cancel.set(); second_started.set(); first_finished.set()
            thread.join(3)


if __name__ == '__main__':
    unittest.main()
