"""Synthetic, headless archive checks. Run explicitly; never access saved cameras."""
from dataclasses import replace
from datetime import date, datetime, timezone
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from cryptography.fernet import Fernet
from playback.backends import IsapiBackend, VideoLinkBackend, cgi_items
from playback.config import Settings, base_url
from playback.controller import Controller, Status
from playback.media import Segment, hls_command, read_segments, packet_bounds
from playback.model import (Camera, Recording, SearchResult, PlaybackError, Viewport,
                            covered_days, day_bounds, local_candidates, parse_time, layout_mode)
from playback.server import Playlist, SessionServer
from playback.store import Store
from playback.transport import xml_body

CAMERA = Camera(42, 'camera.invalid', 'synthetic-user', 'synthetic-password', zone='America/Toronto')
STAMP = datetime(2026, 9, 18, 12, tzinfo=timezone.utc).timestamp()


def recording(**kwargs):
    values = dict(camera_id=42, device='device-a', backend='isapi', track='101', native_id='synthetic-a',
        start=STAMP, end=STAMP+120, size=400, locator='rtsp://camera.invalid/archive?name=x&size=400',
        raw_start='2026-09-18T12:00:00Z', raw_end='2026-09-18T12:02:00Z', observed=1)
    values.update(kwargs)
    return Recording(**values)


class TemporalTests(unittest.TestCase):
    def test_dst_day_duration(self):
        for day,hours in ((date(2026,3,8),23),(date(2026,11,1),25),(date(2026,9,18),24)):
            a,b=day_bounds(day,'America/Toronto')
            self.assertEqual(b-a,hours*3600)

    def test_fold_is_not_silently_assigned(self):
        options=local_candidates(datetime(2026,11,1,1,30),'America/Toronto')
        self.assertEqual(len(options),2)
        self.assertEqual((options[1]-options[0]).total_seconds(),3600)
        with self.assertRaisesRegex(PlaybackError,'ambiguous-time'):
            parse_time('2026-11-01 01:30:00','America/Toronto')

    def test_nonexistent_time_is_not_normalized_into_another_hour(self):
        self.assertEqual(local_candidates(datetime(2026,3,8,2,30),'America/Toronto'),())

    def test_native_offsets_and_explicit_local_correction(self):
        self.assertEqual(parse_time('2026-09-18T07:00:00-05:00'), STAMP)
        self.assertEqual(parse_time('2026-09-18T07:00:00-05:00',shift=3600), STAMP+3600)
        with self.assertRaisesRegex(PlaybackError,'timezone-required'):
            parse_time('2026-09-18 08:00:00')

    def test_midnight_and_year_boundaries_are_half_open(self):
        a=parse_time('2026-12-31T23:59:00-05:00')
        b=parse_time('2027-01-01T00:01:00-05:00')
        self.assertEqual(covered_days(a,b,'America/Toronto'),(date(2026,12,31),date(2027,1,1)))
        self.assertEqual(covered_days(a,b-60,'America/Toronto'),(date(2026,12,31),))

    def test_zoom_preserves_anchor_and_real_time_mapping(self):
        view=Viewport(100,1800)
        new=view.zoom(.5,550)
        self.assertEqual((550-view.start)/view.span,(550-new.start)/new.span)
        self.assertEqual(view.at(500,1000),1000)

    def test_layout_uses_both_dimensions_and_hysteresis(self):
        self.assertEqual(layout_mode(1200,800),'wide')
        self.assertEqual(layout_mode(600,900),'compact')
        self.assertEqual(layout_mode(1200,450),'short')
        self.assertEqual(layout_mode(790,800,previous='compact'),'compact')
        self.assertEqual(layout_mode(790,800,previous='medium'),'medium')
        self.assertEqual(layout_mode(1200,900,scale=2),'short')

    def test_device_and_observed_revision_separate_cache_keys(self):
        first=recording()
        self.assertNotEqual(first.key,replace(first,size=800).key)
        self.assertEqual(first.identity,replace(first,size=800).identity)
        self.assertNotEqual(first.identity,replace(first,device='replacement').identity)
        self.assertNotIn('synthetic-password',repr(CAMERA))
        self.assertNotIn('rtsp://',repr(first))


class ProtocolTests(unittest.TestCase):
    def test_reject_html_success_and_external_entities(self):
        for data in (b'<html><img src="f404.jpg"/></html>',b'<!DOCTYPE x [<!ENTITY x SYSTEM "file:///secret">]><DeviceInfo/>'):
            with self.assertRaises(PlaybackError):
                xml_body(data,('DeviceInfo',))

    def test_videolink_observed_plural_item_attributes(self):
        data=b'<RecordQueryInfo><items filepath="/mnt/mmc0/schedule/20260918/080000-av-1.mp4" filesize="500" record_mode="1" media_type="3" stream_index="1" start_time="2026-09-18 08:00:00"/></RecordQueryInfo>'
        items,more=cgi_items(data)
        self.assertEqual(items[0]['filesize'],'500')
        self.assertFalse(more)
        http=Mock()
        http.read.return_value=data
        backend=VideoLinkBackend(CAMERA,http)
        backend.device='device-a'
        backend.uid='synthetic-session'
        result=backend.list_recordings(STAMP,STAMP+3600,threading.Event())
        self.assertEqual(len(result.records),1)
        self.assertIsNone(result.records[0].end)
        self.assertFalse(result.complete)
        self.assertEqual(result.records[0].start,STAMP)

    def test_empty_cgi_result_remains_unconfirmed(self):
        self.assertEqual(cgi_items(b'<RecordQueryInfo/>'),([],False))
        with self.assertRaises(PlaybackError):
            cgi_items(b'<UnknownSuccess/>')

    def test_cgi_path_traversal_is_refused(self):
        http=Mock()
        http.read.return_value=b'<RecordQueryInfo><items filepath="/mnt/%2e%2e/private" start_time="2026-09-18 08:00:00"/></RecordQueryInfo>'
        backend=VideoLinkBackend(CAMERA,http)
        backend.device='a'
        backend.uid='session'
        with self.assertRaisesRegex(PlaybackError,'invalid-recording-uri'):
            backend.list_recordings(STAMP,STAMP+60,threading.Event())

    def search_page(self,status='MORE',count=1):
        return (f'<CMSearchResult><responseStatus>true</responseStatus><responseStatusStrg>{status}</responseStatusStrg>'
            f'<numOfMatches>{count}</numOfMatches><matchList><searchMatchItem><trackID>101</trackID><timeSpan>'
            '<startTime>2026-09-18T12:00:00Z</startTime><endTime>2026-09-18T12:02:00Z</endTime></timeSpan>'
            '<mediaSegmentDescriptor><playbackURI>rtsp://camera.invalid/replay?name=file&amp;size=400</playbackURI>'
            '</mediaSegmentDescriptor></searchMatchItem></matchList></CMSearchResult>').encode()

    def test_repeated_more_page_is_partial_and_cursor_advances_by_received_count(self):
        http=Mock()
        http.read.return_value=self.search_page()
        backend=IsapiBackend(CAMERA,http)
        backend.device='a'
        backend.tracks=('101',)
        result=backend.list_recordings(STAMP,STAMP+300,threading.Event())
        self.assertEqual(len(result.records),1)
        self.assertFalse(result.complete)
        self.assertEqual(result.reason,'pagination-repeated')
        requests=[xml_body(call.kwargs['data']) for call in http.read.call_args_list]
        self.assertEqual([r.findtext('searchResultPostion') for r in requests],['0','1'])
        self.assertEqual(requests[0].findtext('searchID'),requests[1].findtext('searchID'))

    def test_inconsistent_page_count_is_never_complete(self):
        http=Mock()
        http.read.return_value=self.search_page('OK',3)
        backend=IsapiBackend(CAMERA,http)
        backend.device='a'
        backend.tracks=('101',)
        self.assertFalse(backend.list_recordings(STAMP,STAMP+300,threading.Event()).complete)

    def test_download_xml_keeps_original_uri_with_single_entity_escaping(self):
        http=Mock()
        backend=IsapiBackend(CAMERA,http)
        backend.device='device-a'
        r=recording()
        backend.download(r,Path('unused'),threading.Event(),1000,Mock())
        xml=http.download.call_args.kwargs['data']
        self.assertIn(b'&amp;size',xml)
        self.assertNotIn(b'&amp;amp;',xml)
        self.assertEqual(xml_body(xml).findtext('playbackURI'),r.locator)

    def test_endpoint_preserves_tls_port_without_userinfo_or_extra_path(self):
        self.assertEqual(base_url(replace(CAMERA,endpoint='https://camera.invalid:8443')),'https://camera.invalid:8443')
        for endpoint in ('http://u:p@camera.invalid','http://camera.invalid/path','ftp://camera.invalid'):
            with self.assertRaises(PlaybackError):
                base_url(replace(CAMERA,endpoint=endpoint))


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.settings=Settings(cache_directory=self.temp.name,free_gib=.25)
        self.store=Store(self.settings,Fernet(Fernet.generate_key()))
        self.record=recording()
        self.store.record_search(42,STAMP,STAMP+3600,'device-a',SearchResult((self.record,),True,'',time.time()))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_error_keeps_known_archives_and_freshness_reason(self):
        self.store.search_error(42,STAMP,STAMP+3600,'temporarily-unreachable')
        self.assertEqual(len(self.store.entries((42,),STAMP,STAMP+3600)),1)
        state=self.store.search_status(42,STAMP,STAMP+3600)
        self.assertFalse(state[1])
        self.assertEqual(state[2],'temporarily-unreachable')

    def test_cached_original_survives_remote_expiration(self):
        self.store.state(self.record.key,'prepared',{'duration':120})
        self.store.record_search(42,STAMP,STAMP+3600,'device-a',SearchResult((),True,'',time.time()))
        entry=self.store.entries((42,),STAMP,STAMP+3600)[0]
        self.assertFalse(entry.remote)
        self.assertEqual(entry.state,'prepared')

    def test_private_locator_is_encrypted_in_index(self):
        sealed=self.store.db.execute('SELECT sealed FROM recordings').fetchone()[0]
        self.assertNotIn(b'camera.invalid',sealed)

    def test_changed_device_is_not_current_remote_coverage(self):
        self.store.state(self.record.key,'prepared',{'duration':120})
        newer=recording(device='device-b')
        self.store.record_search(42,STAMP,STAMP+3600,'device-b',SearchResult((newer,),False,'partial',time.time()))
        self.assertFalse(self.store.entry(self.record.key).remote)
        self.assertTrue(self.store.entry(newer.key).remote)

    def test_path_does_not_accept_a_parent_or_absolute_path(self):
        for key in ('..','../outside','C:/private','a'*63):
            with self.assertRaises(PlaybackError):
                self.store.path(key)

    def test_pinned_media_is_not_evicted_even_when_quota_cannot_be_met(self):
        directory=self.store.path(self.record.key)
        directory.mkdir()
        (directory/'original.bin').write_bytes(b'x'*100)
        self.store.pin(self.record.key)
        with self.assertRaisesRegex(PlaybackError,'cache-full'):
            self.store.ensure_space(100*1024**3)
        self.assertTrue((directory/'original.bin').exists())


class MediaAndServingTests(unittest.TestCase):
    def test_export_bounds_use_packet_pts_including_out_of_decode_order_frames(self):
        with patch('playback.media.run',return_value=(b'51.00,0.04\n50.92,0.04\n50.96,0.04\n',0)):
            first,last=packet_bounds(Path('unused'),Settings(),threading.Event())
        self.assertEqual(first,50.92)
        self.assertAlmostEqual(last,51.04)

    def test_codec_policy_is_not_a_camera_configuration_change(self):
        for audio,output in (('aac','copy'),('pcm_mulaw','aac'),('pcm_alaw','aac'),('','aac')):
            args=hls_command(Path('source.bin'),Path('session'),{'audio':audio},Settings())
            self.assertEqual(args[args.index('-c:v')+1],'copy')
            self.assertEqual(args[args.index('-c:a')+1],output)
            self.assertNotIn('-readrate',args)
            self.assertNotIn('independent_segments',args)

    def test_actual_extinf_not_nominal_segment_number(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp)
            for n in range(2):
                (directory/f'segment-{n:05d}.ts').write_bytes(b'synthetic')
            (directory/'source.m3u8').write_text('#EXTM3U\n#EXTINF:4.131,\nsegment-00000.ts\n#EXTINF:3.875,\nsegment-00001.ts\n#EXT-X-ENDLIST\n')
            segments,complete=read_segments(directory,recording())
            self.assertTrue(complete)
            self.assertAlmostEqual(segments[1].start,STAMP+4.131)

    def test_playlist_does_not_end_at_a_native_archive_boundary(self):
        server=Mock()
        server.publish.return_value='http://127.0.0.1/test'
        playlist=Playlist(server,100)
        playlist.update('a',(Segment(Path('segment-00000.ts'),100,4.125,'a'),))
        playlist.update('b',(Segment(Path('segment-00001.ts'),104.125,4.,'b'),))
        body=server.publish.call_args.args[1]
        self.assertNotIn(b'ENDLIST',body)
        self.assertIn(b'EXT-X-DISCONTINUITY',body)
        playlist.finish()
        self.assertIn(b'ENDLIST',server.publish.call_args.args[1])

    def test_true_gap_is_not_compressed_into_continuity(self):
        playlist=Playlist(Mock(),100)
        playlist.update('a',(Segment(Path('segment-00000.ts'),100,4.,'a'),))
        with self.assertRaisesRegex(PlaybackError,'archive-boundary-gap'):
            playlist.update('b',(Segment(Path('segment-00001.ts'),115,4.,'b'),))

    def test_offset_mapping_preserves_discontinuity_adjustment(self):
        playlist=Playlist(Mock(),100)
        playlist.update('a',(Segment(Path('segment-00000.ts'),100,4.,'a'),))
        playlist.update('b',(Segment(Path('segment-00001.ts'),104.25,4.,'b'),))
        self.assertAlmostEqual(playlist.absolute_at(5),105.25)
        self.assertAlmostEqual(playlist.media_offset(105.25),5)

    def test_loopback_allowlist_range_and_no_directory_access(self):
        server=SessionServer()
        try:
            url=server.publish('sample.ts',b'0123456789','video/mp2t')
            with urlopen(Request(url,headers={'Range':'bytes=2-4'}),timeout=2) as reply:
                self.assertEqual(reply.status,206)
                self.assertEqual(reply.read(),b'234')
                self.assertEqual(reply.headers['Content-Range'],'bytes 2-4/10')
            for path in (url+'/../private',url.replace('sample.ts','index.sqlite3'),url+'?path=private'):
                with self.assertRaises(HTTPError):
                    urlopen(path,timeout=2)
            self.assertEqual(server.http.server_address[0],'127.0.0.1')
        finally:
            server.close()

    def test_existing_export_temporary_file_is_never_removed(self):
        with tempfile.TemporaryDirectory() as temp:
            destination=Path(temp)/'saved.bin'
            partial=destination.with_name('saved.bin.partial')
            partial.write_bytes(b'user-owned')
            controller=Controller.__new__(Controller)
            controller.export_request=('key',destination,None,None)
            controller.export_cancel=threading.Event()
            controller._export(threading.Event())
            self.assertEqual(controller.export_status,'export-exists')
            self.assertEqual(partial.read_bytes(),b'user-owned')


if __name__ == '__main__':
    unittest.main()
