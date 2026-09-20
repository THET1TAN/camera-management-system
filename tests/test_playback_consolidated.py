"""User-run regression suite. All cache deletion uses disposable synthetic files."""
from dataclasses import replace
from contextlib import contextmanager
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.request import urlopen

from cryptography.fernet import Fernet
from playback.config import Settings
from playback.exporting import ExportRequest, Exporter, plan_interval, destination_check
from playback.model import Recording, SearchResult, PlaybackError, Viewport
from playback.store import Entry, Store
from playback.server import SessionServer
from playback.ui import PlaybackWindow
from test_playback_progressive import controller


def entry(start,end,camera=1,track='1',observed=1):
    r=Recording(camera,'test-device','fixture',track,str(start),start,end,100,
                observed=observed,raw_start=str(start),raw_end=str(end))
    return Entry(r,'downloaded',dict(duration=end-start,video='h264',audio='aac',width=640,height=360))


class ExportPlanTests(unittest.TestCase):
    def request(self,a=100,b=700):
        return ExportRequest(1,'1',a,b,Path('result.mp4'))

    def test_more_than_four_archives_without_open_player(self):
        rows=[entry(n,n+100) for n in range(100,700,100)]
        plan=plan_interval(rows,self.request())
        self.assertEqual(len(plan),6)
        self.assertTrue(all(p.entry for p in plan))

    def test_duplicates_and_overlaps_have_one_deterministic_owner(self):
        a,b=entry(100,250),entry(200,300)
        plan=plan_interval((a,a,b),self.request(150,280))
        self.assertEqual([(p.start,p.end,p.entry) for p in plan],[(150,200,a),(200,280,b)])

    def test_tiny_true_gap_is_not_filled_by_join_tolerance(self):
        plan=plan_interval((entry(100,200),entry(200.1,300)),self.request(100,300))
        self.assertEqual([(p.start,p.end) for p in plan if p.entry is None],[(200,200.1)])

    def test_camera_and_track_never_mix(self):
        plan=plan_interval((entry(100,300,camera=2),entry(100,300,track='2')),self.request(100,300))
        self.assertEqual(len(plan),1)
        self.assertIsNone(plan[0].entry)

    def test_midnight_is_an_ordinary_utc_boundary(self):
        plan=plan_interval((entry(86380,86400),entry(86400,86430)),self.request(86390,86410))
        self.assertEqual(sum(p.end-p.start for p in plan),20)
        self.assertTrue(all(p.entry for p in plan))

    def test_output_cannot_be_inside_evictable_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(PlaybackError,'export-path'):
                destination_check(Path(temp)/'result.mp4',Path(temp),0,0)

    def test_precise_command_reencodes_sources_not_viewer(self):
        e=entry(100,200)
        exporter=Exporter(SimpleNamespace(settings=Settings()))
        args=exporter._command(plan_interval((e,),self.request(110,120))[0],Path('original.bin'),e.media,Path('part.mkv'),640,360)
        self.assertEqual(args[args.index('-ss')+1],'10')
        self.assertEqual(args[args.index('-c:v')+1],'libx264')
        self.assertNotIn('screen',str(args))
        self.assertNotIn('fps=',args[args.index('-vf')+1])
        self.assertEqual(args[args.index('-vsync')+1],'vfr')

    def test_missing_recording_is_explicit_neutral_image(self):
        exporter=Exporter(SimpleNamespace(settings=Settings()))
        part=plan_interval((),self.request(100,110))[0]
        args=exporter._command(part,None,{},Path('part.mkv'),640,360)
        self.assertIn('No recording',args[args.index('-vf')+1])

    def test_failed_discovery_does_not_become_confirmed_gap(self):
        backend=Mock()
        backend.list_recordings.side_effect=PlaybackError('temporarily-unreachable')
        c=SimpleNamespace(settings=Settings(),store=Mock(),export_cancel=threading.Event(),
                          stop_event=threading.Event(),request=None,connect_backend=Mock(return_value=backend))
        c.store.entries.return_value=()
        with self.assertRaisesRegex(PlaybackError,'export-coverage-unknown'):
            Exporter(c)._discover(self.request(),SimpleNamespace(camera_id=1),[backend])

    def test_cancel_before_discovery_makes_no_camera_request(self):
        cancel=threading.Event();cancel.set()
        c=SimpleNamespace(settings=Settings(),store=Mock(),export_cancel=cancel,
                          stop_event=threading.Event(),request=None,connect_backend=Mock())
        with self.assertRaisesRegex(PlaybackError,'cancelled'):
            Exporter(c)._discover(self.request(),SimpleNamespace(camera_id=1),[None])
        c.connect_backend.assert_not_called()


class LeaseAndBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='camera-cache-synthetic-')
        self.settings=Settings(cache_directory=self.temp.name,free_gib=0,cache_gib=1)
        self.store=Store(self.settings,Fernet(Fernet.generate_key()),maintenance=False)
        self.e=entry(100,200)
        self.store.record_search(1,0,1000,'test-device',SearchResult((self.e.recording,),True,'',time.time()))
        self.directory=self.store.path(self.e.recording.key)
        self.directory.mkdir()
        (self.directory/'original.bin').write_bytes(b'x'*100)
        self.store.state(self.e.recording.key,'downloaded',self.e.media)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_owner_release_cannot_release_another_owner(self):
        key=self.e.recording.key
        self.store.pin(key,'player')
        self.store.pin(key,'export')
        self.store.unpin(key,'export')
        self.assertEqual(self.store.plan_cleanup()['candidates'],[])
        self.assertIn('player',self.store.pins[key])

    def test_nested_same_owner_is_reference_counted(self):
        key=self.e.recording.key
        with self.store.lease(key,'reader'):
            with self.store.lease(key,'reader'):
                self.assertEqual(self.store.pins[key]['reader'],2)
            self.assertIn(key,self.store.pins)
        self.assertNotIn(key,self.store.pins)

    def test_manual_confirmation_rechecks_new_player_lease(self):
        plan=self.store.plan_cleanup()
        self.store.pin(self.e.recording.key,'player')
        result=self.store.execute_cleanup(plan)
        self.assertEqual(result['freed'],0)
        self.assertEqual(result['protected'],1)
        self.assertTrue((self.directory/'original.bin').exists())

    def test_actual_cleanup_retains_remote_availability(self):
        result=self.store.execute_cleanup(self.store.plan_cleanup())
        self.assertEqual(result['freed'],100)
        e=self.store.entry(self.e.recording.key)
        self.assertEqual(e.state,'expired')
        self.assertTrue(e.remote)
        self.assertEqual(len(self.store.entries((1,),100,200)),1)

    def test_unknown_files_are_preserved_and_reported(self):
        (self.directory/'my-export.mp4').write_bytes(b'keep')
        plan=self.store.plan_cleanup()
        self.assertEqual(plan['candidates'],[])
        self.assertEqual(self.store.cache_snapshot['uncertain'],1)
        self.assertTrue((self.directory/'original.bin').exists())

    def test_incident_hold_is_not_a_transient_lease(self):
        self.settings.protected_archives=[self.e.recording.key]
        self.assertEqual(self.store.plan_cleanup()['candidates'],[])

    def test_reservation_written_bytes_are_not_counted_twice(self):
        key=self.e.recording.key
        with self.store.lease(key,'producer'),self.store.reserve('producer',key,1000) as r:
            (self.directory/'original.part').write_bytes(b'y'*400)
            r.check()
            snapshot=self.store._inventory()[1]
            self.assertEqual(snapshot['media'],500)
            self.assertEqual(snapshot['reserved'],600)
            self.assertEqual(snapshot['media']+snapshot['reserved'],1100)
        self.assertEqual(self.store.reservations,{})

    def test_two_concurrent_reservations_cannot_spend_same_budget(self):
        key=self.e.recording.key
        self.settings.cache_gib=1500/1024**3
        with self.store.lease(key,'writer'),self.store.reserve('one',key,900):
            with self.assertRaisesRegex(PlaybackError,'cache-full'):
                with self.store.reserve('two',key,900):
                    self.fail('Overlapping capacity was granted')

    def test_windows_lock_failure_does_not_report_freed_bytes(self):
        plan=self.store.plan_cleanup()
        with patch.object(Path,'unlink',side_effect=PermissionError('synthetic sharing violation')):
            result=self.store.execute_cleanup(plan)
        self.assertEqual(result['freed'],0)
        self.assertEqual(result['errors'],1)
        self.assertEqual(self.store.entry(self.e.recording.key).state,'downloaded')

    def test_junction_detection_blocks_deletion(self):
        with patch('playback.cache.linked',side_effect=lambda p:p==self.directory):
            self.assertEqual(self.store.plan_cleanup()['candidates'],[])
        self.assertTrue((self.directory/'original.bin').exists())

    def test_metadata_refresh_does_not_extend_use(self):
        with self.store.lock,self.store.db:
            self.store.db.execute('UPDATE recordings SET used=1')
        self.store.record_search(1,0,1000,'test-device',SearchResult((self.e.recording,),True,'',time.time()))
        used=self.store.db.execute('SELECT used FROM recordings').fetchone()[0]
        self.assertEqual(used,1)
        self.store.touch(self.e.recording.key)
        self.store.flush_touches()
        self.assertGreater(self.store.db.execute('SELECT used FROM recordings').fetchone()[0],1)

    def test_repeated_use_remains_protected_past_retention(self):
        with self.store.lock,self.store.db:
            self.store.db.execute('UPDATE recordings SET used=1')
        self.store.touch(self.e.recording.key)
        self.assertEqual(self.store.plan_cleanup(automatic=True)['candidates'],[])

    def test_interrupted_workspace_is_reclaimed_on_next_lifecycle(self):
        with self.store.workspace('export') as (_,path):
            (path/'export-part.mkv').write_bytes(b'temporary')
            self.assertNotIn(path.name,[k for k,_ in self.store.plan_cleanup()['candidates']])
        plan=self.store.plan_cleanup(automatic=True)
        self.assertIn(path.name,[k for k,_ in plan['candidates']])
        self.store.execute_cleanup(plan)
        self.assertFalse(path.exists())

    def test_unknown_directory_is_counted_but_not_deleted(self):
        orphan=Path(self.temp.name)/('f'*64)
        orphan.mkdir();(orphan/'original.bin').write_bytes(b'unknown')
        plan=self.store.plan_cleanup()
        self.assertNotIn(orphan.name,[k for k,_ in plan['candidates']])
        self.store.execute_cleanup(plan)
        self.assertTrue((orphan/'original.bin').exists())
        self.assertEqual(self.store.cache_snapshot['media'],7)

    def test_quota_reduction_preserves_active_media(self):
        self.settings.cache_gib=50/1024**3
        with self.store.lease(self.e.recording.key,'player'):
            with self.assertRaisesRegex(PlaybackError,'cache-full'):
                self.store.ensure_space()
        self.assertTrue((self.directory/'original.bin').exists())

    def test_http_reader_keeps_lease_after_playlist_is_retired(self):
        entered,proceed=threading.Event(),threading.Event()
        key=self.e.recording.key
        @contextmanager
        def held_reader(key,owner):
            with self.store.lease(key,owner):
                entered.set()
                proceed.wait(3)
                yield
        server=SessionServer(store=SimpleNamespace(lease=held_reader))
        received=[]
        url=server.publish('fixture.ts',self.directory/'original.bin','video/mp2t')
        def fetch():
            with urlopen(url,timeout=4) as response:
                received.append(response.read())
        self.store.pin(key,'playlist')
        reader=threading.Thread(target=fetch,daemon=True)
        reader.start()
        try:
            self.assertTrue(entered.wait(2))
            self.store.unpin(key,'playlist')
            self.assertEqual(self.store.plan_cleanup()['candidates'],[])
            proceed.set();reader.join(4)
            self.assertEqual(received,[b'x'*100])
        finally:
            proceed.set()
            server.close()
            reader.join(4)


class ScrubAndSelectionTests(unittest.TestCase):
    def test_one_inflight_and_only_latest_pending(self):
        c=controller()
        c.seek(3,110,preview=True);c._service_seek()
        first=c.engine.seek_request['id']
        for target in (125,115,130,120):
            c.seek(3,target,preview=True);c._service_seek()
        self.assertEqual(c.engine.seek_request['id'],first)
        c.engine.snapshot=replace(c.engine.snapshot,confirmed_seek=first,position=10.)
        c._service_seek()
        self.assertEqual(c.engine.seek_request['offset'],20.)
        self.assertEqual(c.preview_position,110.)
        self.assertEqual(c.engine.request[0],40)

    def test_slow_preview_is_abandoned_for_latest_target(self):
        c=controller()
        c.seek(3,110,preview=True);c._service_seek()
        token,native,generation,_=c.seek_inflight
        c.seek_inflight=(token,native,generation,time.monotonic()-1)
        c.seek(3,130,preview=True);c._service_seek()
        self.assertEqual(c.engine.seek_request['offset'],30.)
        self.assertNotEqual(c.engine.seek_request['id'],native)

    def test_final_release_preempts_old_preview_and_preserves_controls(self):
        c=controller();controls=c.controls
        c.seek(3,130,preview=True);c._service_seek()
        old=c.engine.seek_request['id']
        c.seek(3,110,preview=False);c._service_seek()
        self.assertNotEqual(c.engine.seek_request['id'],old)
        self.assertFalse(c.engine.seek_request['preview'])
        c._native_event('native-new-frame',generation=40,seek_id=old,position=30.)
        self.assertIsNotNone(c.seek_target)
        self.assertEqual(c.controls,controls)

    def test_selection_drag_never_seeks_and_moves_as_a_whole(self):
        ui=PlaybackWindow.__new__(PlaybackWindow)
        ui.timeline=Mock();ui.timeline.winfo_width.return_value=158
        ui.view=Viewport(100,100);ui.controller=Mock()
        ui.selection_drag=('move',120,110,140)
        ui.mark_a,ui.mark_b=110,140
        ui._show_selection=Mock();ui._draw_timeline=Mock()
        ui._drag(SimpleNamespace(x=88))
        self.assertEqual((ui.mark_a,ui.mark_b),(120,150))
        ui.controller.seek.assert_not_called()


if __name__=='__main__':
    unittest.main()
