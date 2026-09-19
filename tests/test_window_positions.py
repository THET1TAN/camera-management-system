"""Persistence, simulated display layouts and real local Tk windows; no cameras."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from window_positions import (Screen, PositionStore, PlacementWorker, WindowPlacement,
                              reset_positions, screens_signature, visible_geometry,
                              windows_frame_insets, windows_screens)


PRIMARY = Screen('main', (0, 0, 1920, 1080), (0, 0, 1920, 1040), True)
LEFT = Screen('left', (-1920, -200, 0, 880), (-1920, -200, 0, 840))
ABOVE = Screen('above', (0, -1200, 1600, 0), (0, -1200, 1600, -40))
SCREENS = (PRIMARY, LEFT)


def eventually(predicate, timeout=3, pump=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pump:
            pump()
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('Timed out waiting for window placement')


class GeometryTests(unittest.TestCase):
    def test_portrait_edge_placements_keep_exact_position_and_size(self):
        portrait = Screen('portrait', (0, 0, 1080, 1920), (0, 0, 1080, 1880), True)
        # 7 px are invisible on each side; the visible frame is 2 px wider than the client.
        for position, size in [((-7, 0), (538, 1848)), ((533, 0), (538, 1848)),
                               ((-7, 0), (1078, 1848))]:
            with self.subTest(position=position, size=size):
                self.assertEqual(visible_geometry('1', position, size, (portrait,),
                                                  invisible_frame=(7, 0, 7, 7)), (*size, *position))

    def test_invisible_border_on_neighbor_monitor_does_not_change_target_screen(self):
        for screens, position, size in [
            ((PRIMARY, LEFT), (-7, 0), (958, 1008)),
            ((LEFT, PRIMARY), (-7, 0), (958, 1008)),
            ((PRIMARY, LEFT), (-1927, -200), (1918, 1008)),
        ]:
            with self.subTest(position=position):
                self.assertEqual(visible_geometry('1', position, size, screens,
                                                  invisible_frame=(7, 0, 7, 7)), (*size, *position))

    def test_frame_fit_still_keeps_visible_content_out_of_taskbar_and_offscreen(self):
        small = Screen('small', (0, 0, 640, 480), (40, 0, 640, 480), True)
        self.assertEqual(visible_geometry('1', (-9999, 9000), (800, 1000), (small,),
                                          invisible_frame=(7, 0, 7, 7)), (598, 448, 33, 0))

    def test_mostly_visible_window_uses_its_monitor_even_when_origin_overlaps_neighbor(self):
        right = Screen('right', (1920, 0, 3000, 1920), (1920, 0, 3000, 1880))
        self.assertEqual(visible_geometry('1', (1900, 100), (800, 600), (PRIMARY, right),
                                          invisible_frame=(7, 0, 7, 7)), (800, 600, 1913, 100))

    def test_single_screen_restores_exact_position(self):
        self.assertEqual(visible_geometry('1', (300, 150), (800, 600), (PRIMARY,)),
                         (800, 600, 300, 150))

    def test_negative_horizontal_and_vertical_coordinates_are_absolute(self):
        self.assertEqual(visible_geometry('1', (-1500, -100), (800, 600), SCREENS),
                         (800, 600, -1500, -100))
        self.assertEqual(visible_geometry('1', (200, -1100), (800, 600), (PRIMARY, ABOVE)),
                         (800, 600, 200, -1100))

    def test_unplugged_monitor_moves_window_onto_remaining_work_area(self):
        self.assertEqual(visible_geometry('1', (-1500, -100), (800, 600), (PRIMARY,))[2:], (0, 0))

    def test_gaps_between_displays_are_not_usable_desktop(self):
        right = Screen('right', (3000, 0, 4920, 1080), (3000, 0, 4920, 1040))
        self.assertEqual(visible_geometry('1', (2500, 100), (800, 600), (PRIMARY, right))[2:], (3000, 100))

    def test_taskbar_and_decorations_are_excluded(self):
        self.assertEqual(visible_geometry('1', (1900, 1030), (800, 600), (PRIMARY,)),
                         (800, 600, 1104, 401))

    def test_small_monitor_shrinks_window_to_keep_controls_accessible(self):
        small = Screen('small', (0, 0, 640, 480), (0, 0, 640, 440), True)
        self.assertEqual(visible_geometry('1', None, (800, 1000), (small,)), (624, 401, 0, 0))

    def test_defaults_are_stable_and_visible_for_multiple_cameras(self):
        defaults = [visible_geometry(str(i), None, (800, 600), SCREENS) for i in range(10)]
        self.assertGreater(len(set(defaults)), 1)
        for i, value in enumerate(defaults):
            self.assertEqual(value, visible_geometry(str(i), None, (800, 600), SCREENS))
            self.assertGreaterEqual(value[2], 0)
            self.assertLessEqual(value[3]+600+39, 1040)

    def test_monitor_order_is_irrelevant_but_layout_work_area_and_primary_are_not(self):
        signature = screens_signature(SCREENS)
        self.assertEqual(signature, screens_signature(tuple(reversed(SCREENS))))
        variants = [(PRIMARY,), (PRIMARY, ABOVE),
                    (Screen('main', PRIMARY.bounds, (0, 40, 1920, 1080), True), LEFT),
                    (Screen('main', PRIMARY.bounds, PRIMARY.work, False),
                     Screen('left', LEFT.bounds, LEFT.work, True))]
        for screens in variants:
            self.assertNotEqual(signature, screens_signature(screens))


class FrameInsetsTests(unittest.TestCase):
    def setUp(self):
        self.user32, self.dwmapi = Mock(), Mock()
        def outer(_hwnd, target):
            target._obj.left, target._obj.top = -1000, 200
            target._obj.right, target._obj.bottom = -344, 649
            return True
        def visible(_hwnd, _attribute, target, _size):
            target._obj.left, target._obj.top = -1986, 400
            target._obj.right, target._obj.bottom = -702, 1284
            return 0
        def logical(_hwnd, target):
            target._obj.x //= 2
            target._obj.y //= 2
            return True
        self.user32.GetWindowRect.side_effect = outer
        self.dwmapi.DwmGetWindowAttribute.side_effect = visible
        self.user32.PhysicalToLogicalPointForPerMonitorDPI.side_effect = logical
        self.enterContext(patch('window_positions.os.name', 'nt'))
        self.enterContext(patch('window_positions.ctypes.WinDLL', create=True,
                                side_effect=lambda name, **kw: self.user32 if name == 'user32' else self.dwmapi))

    def test_dwm_physical_coordinates_are_converted_before_measuring_margins(self):
        self.assertEqual(windows_frame_insets(123), (7, 0, 7, 7))
        self.assertEqual(self.user32.PhysicalToLogicalPointForPerMonitorDPI.call_count, 2)

    def test_failed_native_queries_or_unavailable_dwm_use_conservative_fit(self):
        for function, result in [(self.user32.GetWindowRect, False),
                                 (self.dwmapi.DwmGetWindowAttribute, -1),
                                 (self.user32.PhysicalToLogicalPointForPerMonitorDPI, False)]:
            original = function.side_effect
            function.side_effect = None
            function.return_value = result
            self.assertEqual(windows_frame_insets(123), (0, 0, 0, 0))
            function.side_effect = original
        self.dwmapi.DwmGetWindowAttribute.side_effect = OSError('DWM unavailable')
        self.assertEqual(windows_frame_insets(123), (0, 0, 0, 0))

    def test_inconsistent_dwm_rectangle_does_not_allow_clipping_content(self):
        self.user32.PhysicalToLogicalPointForPerMonitorDPI.side_effect = None
        self.user32.PhysicalToLogicalPointForPerMonitorDPI.return_value = True
        self.assertEqual(windows_frame_insets(123), (0, 0, 0, 0))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'positions.db'
        self.store = PositionStore(self.path)
        self.signature = screens_signature(SCREENS)

    def test_independent_camera_ids_survive_store_reopen(self):
        self.store.save(1, 0, self.signature, -1500, -100)
        self.store.save(2, 0, self.signature, 300, 200)
        self.assertEqual(PositionStore(self.path).read(1), (0, (self.signature, -1500, -100, None, None)))
        self.assertEqual(PositionStore(self.path).read('2')[1][1:3], (300, 200))

    def test_positions_survive_a_new_python_process(self):
        self.store.save('one', 0, self.signature, -1500, -100, 640, 410)
        code = 'import json,sys; from window_positions import PositionStore; print(json.dumps(PositionStore(sys.argv[1]).read("one")))'
        result = subprocess.run([sys.executable, '-c', code, str(self.path)],
                                capture_output=True, text=True, timeout=5, check=True)
        self.assertEqual(json.loads(result.stdout), [0, [self.signature, -1500, -100, 640, 410]])

    def test_simultaneous_writers_do_not_overwrite_other_cameras(self):
        self.store.read('init')
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: self.store.save(i, 0, self.signature, i*10, i*20, 500+i, 300+i), range(12)))
        for i in range(12):
            self.assertEqual(self.store.read(i)[1][1:], (i*10, i*20, 500+i, 300+i))

    def test_reset_clears_every_camera_and_rejects_pre_reset_writes(self):
        for i in range(3):
            self.store.save(i, 0, self.signature, 10, 20, 640, 410)
        self.store.reset()
        self.store.save(1, 0, self.signature, 500, 600, 900, 700)
        for i in range(3):
            self.assertEqual(self.store.read(i), (1, None))
        self.store.save(1, 1, self.signature, 70, 80)
        self.assertEqual(self.store.read(1)[1][1:], (70, 80, None, None))

    def test_invalid_size_keeps_valid_position_without_restoring_dimensions(self):
        for size in [(0, 400), (400, -1), ('bad', 400), (400, 10**12), (None, 400)]:
            with self.subTest(size=size):
                self.store.save(1, 0, self.signature, 300, 200, *size)
                self.assertEqual(self.store.read(1)[1], (self.signature, 300, 200, None, None))

    def legacy_store(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE layout_state (id INTEGER PRIMARY KEY, generation INTEGER NOT NULL)')
            db.execute('INSERT INTO layout_state VALUES (1, 3)')
            db.execute('CREATE TABLE window_positions (camera_id TEXT PRIMARY KEY, screens TEXT NOT NULL, '
                       'x INTEGER NOT NULL, y INTEGER NOT NULL)')
            db.execute('INSERT INTO window_positions VALUES (?,?,?,?)', ('1', self.signature, 300, 200))

    def test_upgrade_preserves_positions_and_remains_compatible_with_running_old_players(self):
        self.legacy_store()
        self.assertEqual(self.store.read(1), (3, (self.signature, 300, 200, None, None)))
        self.store.save(1, 3, self.signature, 300, 200, 640, 410)
        with closing(sqlite3.connect(self.path)) as db, db:
            # Old players still use a four-column INSERT; an old viewer only resets positions.
            db.execute('INSERT OR REPLACE INTO window_positions SELECT ?,?,?,? '
                       'WHERE (SELECT generation FROM layout_state WHERE id=1)=?',
                       ('1', self.signature, 350, 220, 3))
        self.assertEqual(self.store.read(1)[1][1:], (350, 220, 640, 410))
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('UPDATE layout_state SET generation=generation+1 WHERE id=1')
            db.execute('DELETE FROM window_positions')
            db.execute('INSERT INTO window_positions VALUES (?,?,?,?)', ('1', self.signature, 300, 200))
        self.assertEqual(self.store.read(1), (4, (self.signature, 300, 200, None, None)))

    def test_simultaneous_upgrade_keeps_legacy_records_and_new_camera_sizes(self):
        self.legacy_store()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: PositionStore(self.path).save(i, 3, self.signature, 300, 200, 600+i, 400+i),
                          range(2, 6)))
        self.assertEqual(self.store.read(1), (3, (self.signature, 300, 200, None, None)))
        for i in range(2, 6):
            self.assertEqual(self.store.read(i)[1][3:], (600+i, 400+i))

    def test_multiple_resets_advance_generation_even_without_positions(self):
        self.store.reset()
        self.store.reset()
        self.assertEqual(self.store.read(1), (2, None))

    def test_invalid_coordinates_are_ignored(self):
        self.store.save(1, 0, self.signature, 'invalid', 100)
        self.assertIsNone(self.store.read(1)[1])
        self.store.save(1, 0, self.signature, 10**12, 0)
        self.assertIsNone(self.store.read(1)[1])

    def test_reset_does_not_touch_credentials_or_other_settings(self):
        sentinel = Path(self.directory.name) / 'camera_credentials.db'
        sentinel.write_bytes(b'private sentinel')
        self.assertTrue(reset_positions(self.path).result(timeout=2))
        self.assertEqual(sentinel.read_bytes(), b'private sentinel')

    def test_locked_database_returns_failure_without_waiting_on_caller(self):
        self.store.read(1)
        with closing(sqlite3.connect(self.path)) as lock, lock:
            lock.execute('BEGIN EXCLUSIVE')
            started = time.monotonic()
            future = reset_positions(self.path)
            self.assertLess(time.monotonic()-started, .1)
            self.assertFalse(future.result(timeout=2))
        self.assertTrue(reset_positions(self.path).result(timeout=2))

    def test_corrupt_database_is_preserved_and_reset_reports_failure(self):
        self.path.write_bytes(b'not sqlite')
        self.assertFalse(reset_positions(self.path).result(timeout=2))
        self.assertEqual(self.path.read_bytes(), b'not sqlite')


class PlacementWorkerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'positions.db'
        self.screens = SCREENS
        self.workers = []

    def tearDown(self):
        for worker in self.workers:
            worker.close()
            self.assertTrue(worker.closed.wait(2))
        self.directory.cleanup()

    def worker(self, camera='1'):
        worker = PlacementWorker(camera, (PRIMARY,), path=self.path, screen_provider=lambda: self.screens)
        self.workers.append(worker)
        return worker

    def update(self, worker):
        eventually(lambda: not worker.updates.empty())
        return worker.updates.get_nowait()

    def test_worker_restores_only_with_identical_screens(self):
        store = PositionStore(self.path)
        store.save(1, 0, screens_signature(SCREENS), -1500, -100)
        self.assertEqual(self.update(self.worker())[2], (-1500, -100, None, None))
        self.screens = (PRIMARY,)
        self.assertIsNone(self.update(self.worker())[2])

    def test_screen_change_and_reset_notify_all_running_players(self):
        first, second = self.worker('1'), self.worker('2')
        self.update(first); self.update(second)
        self.screens = (PRIMARY,)
        for worker in (first, second):
            self.assertEqual(self.update(worker)[1], (PRIMARY,))
        PositionStore(self.path).reset()
        for worker in (first, second):
            update = self.update(worker)
            self.assertEqual(update[0][0], 1)
            self.assertIsNone(update[2])

    def test_close_flushes_last_move_and_finishes_worker(self):
        worker = self.worker()
        token = self.update(worker)[0]
        worker.save(token, (-1400, -50, 640, 410))
        worker.close()
        self.assertTrue(worker.closed.wait(2))
        self.assertEqual(PositionStore(self.path).read(1)[1][1:], (-1400, -50, 640, 410))

    def test_reset_racing_with_close_does_not_restore_stale_position(self):
        worker = self.worker()
        token = self.update(worker)[0]
        PositionStore(self.path).reset()
        worker.save(token, (-1400, -50, 640, 410))
        worker.close()
        self.assertTrue(worker.closed.wait(2))
        self.assertEqual(PositionStore(self.path).read(1), (1, None))

    def test_slow_monitor_call_does_not_block_owner_or_close(self):
        entered, release = threading.Event(), threading.Event()
        owner = threading.get_ident()
        def slow():
            self.assertNotEqual(threading.get_ident(), owner)
            entered.set()
            release.wait(2)
            return SCREENS
        worker = PlacementWorker('1', (PRIMARY,), path=self.path, screen_provider=slow)
        self.workers.append(worker)
        try:
            self.assertTrue(entered.wait(1))
            start = time.monotonic()
            worker.close()
            self.assertLess(time.monotonic()-start, .1)
        finally:
            release.set()

    def test_corrupt_storage_still_supplies_visible_defaults_and_tracks_screens(self):
        self.path.write_bytes(b'bad database')
        worker = self.worker()
        self.assertIsNone(self.update(worker)[2])
        self.screens = (PRIMARY,)
        self.assertEqual(self.update(worker)[1], (PRIMARY,))


class TkPlacementTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'positions.db'
        self.root = tk.Tk()
        self.root.geometry('400x240+300+200')
        self.root.update()
        self.screens = SCREENS
        self.placements = []
        self.windows = [self.root]

    def tearDown(self):
        for placement in self.placements:
            placement.close()
            self.assertTrue(placement.worker.closed.wait(2))
        for window in reversed(self.windows):
            if window.winfo_exists():
                window.destroy()
        self.directory.cleanup()

    def placement(self, root=None, camera='1'):
        placement = WindowPlacement(root or self.root, camera, path=self.path,
                                    screen_provider=lambda: self.screens)
        self.placements.append(placement)
        self.pump_until(lambda: placement.token is not None and not placement.settling)
        return placement

    def pump_until(self, predicate):
        eventually(predicate, pump=self.root.update)

    def test_real_tk_restores_negative_coordinates(self):
        PositionStore(self.path).save(1, 0, screens_signature(SCREENS), -1500, -100)
        self.placement()
        self.assertEqual((self.root.winfo_x(), self.root.winfo_y()), (-1500, -100))

    @unittest.skipUnless(os.name == 'nt', 'Windows visible frame measurement')
    def test_portrait_edges_survive_repeated_close_restore_without_margin_or_size_drift(self):
        self.screens = (Screen('portrait', (0, 0, 1080, 1920), (0, 0, 1080, 1880), True),
                        Screen('neighbor', (-1920, 0, 0, 1080), (-1920, 0, 0, 1040)))
        insets = windows_frame_insets(int(self.root.wm_frame(), 0))
        self.assertGreater(insets[0], 0)  # Exercise real DWM borders, not the conservative fallback.
        border = self.root.winfo_rootx() - self.root.winfo_x()
        width = 540 - border*2 + insets[0] + insets[2]
        for x in (-insets[0], 540-insets[0]):
            camera = str(x)
            initial = self.placement(camera=camera)
            self.root.geometry(f'{width}x600+{x}+100'); self.root.update()
            expected = initial._geometry()
            initial.close()
            self.assertTrue(initial.worker.closed.wait(2))
            for _ in range(3):
                window = tk.Toplevel(self.root)
                self.windows.append(window)
                window.geometry('400x240+300+200'); window.update()
                restored = self.placement(window, camera)
                restored.resize_for_video(800, 490)
                window.update()
                self.assertEqual(restored._geometry(), expected)
                actual_insets = windows_frame_insets(int(window.wm_frame(), 0))
                visible_left = window.winfo_x()+actual_insets[0]
                visible_right = window.winfo_x()+window.winfo_width()+border*2-actual_insets[2]
                self.assertEqual((visible_left, visible_right), (0, 540) if x < 0 else (540, 1080))
                restored.close()
                self.assertTrue(restored.worker.closed.wait(2))
                window.destroy()

    def test_move_close_and_new_window_restore_each_camera(self):
        first = self.placement()
        self.root.geometry('+350+220')
        self.root.update()
        first.close()
        self.assertTrue(first.worker.closed.wait(2))
        window = tk.Toplevel(self.root)
        self.windows.append(window)
        window.geometry('400x240')
        window.update()
        self.placement(window)
        self.assertEqual((window.winfo_x(), window.winfo_y()), (350, 220))
        other = tk.Toplevel(self.root)
        self.windows.append(other)
        other.geometry('400x240')
        other.update()
        self.placement(other, '2')
        self.assertNotEqual((other.winfo_x(), other.winfo_y()), (350, 220))

    def test_size_only_change_survives_close_reopen_and_first_video(self):
        first = self.placement()
        position = first._geometry()[2:]
        self.root.geometry('640x410')
        self.root.update()
        self.assertEqual(first._geometry()[2:], position)
        first.close()  # Capture a resize even before the next scheduled sample.
        self.assertTrue(first.worker.closed.wait(2))
        self.assertEqual(PositionStore(self.path).read(1)[1][1:], (*position, 640, 410))
        window = tk.Toplevel(self.root)
        self.windows.append(window)
        window.geometry('800x600'); window.update()
        restored = self.placement(window)
        with patch.object(window, 'geometry', wraps=window.geometry) as geometry:
            restored.resize_for_video(800, 490)
            geometry.assert_not_called()
        self.assertEqual(restored._geometry(), (640, 410, *position))

    def test_saved_size_wins_when_first_video_arrives_before_storage_lookup(self):
        PositionStore(self.path).save(1, 0, screens_signature(SCREENS), 300, 200, 640, 410)
        entered, release = threading.Event(), threading.Event()
        def slow_screens():
            entered.set()
            release.wait(2)
            return SCREENS
        placement = WindowPlacement(self.root, '1', path=self.path, screen_provider=slow_screens)
        self.placements.append(placement)
        try:
            self.assertTrue(entered.wait(1))
            placement.resize_for_video(800, 490)
            self.root.update()
            self.assertEqual(placement._geometry()[:2], (800, 490))
            release.set()
            self.pump_until(lambda: placement.token is not None and not placement.settling)
            self.assertEqual(placement._geometry(), (640, 410, 300, 200))
        finally:
            release.set()

    def test_legacy_position_without_size_still_uses_initial_video_aspect_ratio(self):
        PositionStore(self.path).save(1, 0, screens_signature(SCREENS), 300, 200)
        placement = self.placement()
        placement.resize_for_video(800, 490)
        self.pump_until(lambda: not placement.settling)
        self.assertEqual(placement._geometry(), (800, 490, 300, 200))
        self.assertFalse(placement.user_placed)

    def test_restored_oversized_window_is_fitted_and_not_resized_by_video(self):
        PositionStore(self.path).save(1, 0, screens_signature(SCREENS), 300, 200, 4000, 3000)
        placement = self.placement()
        restored = placement._geometry()
        self.assertLessEqual(self.root.winfo_rootx()+self.root.winfo_width(), 1920)
        self.assertLessEqual(self.root.winfo_rooty()+self.root.winfo_height(), 1040)
        placement.resize_for_video(800, 490)
        self.root.update()
        self.assertEqual(placement._geometry(), restored)

    def test_maximizing_does_not_replace_saved_normal_size(self):
        placement = self.placement()
        self.root.geometry('640x410+300+200'); self.root.update()
        self.pump_until(lambda: PositionStore(self.path).read(1)[1] is not None)
        self.root.state('zoomed'); self.root.update()
        placement.close()
        self.assertTrue(placement.worker.closed.wait(2))
        self.assertEqual(PositionStore(self.path).read(1)[1][1:], (300, 200, 640, 410))

    def test_reset_repositions_open_windows_and_next_open_uses_defaults(self):
        for camera, pos in [('1', (-1500, -100)), ('2', (500, 300))]:
            PositionStore(self.path).save(camera, 0, screens_signature(SCREENS), *pos)
        first = self.placement()
        other = tk.Toplevel(self.root)
        self.windows.append(other)
        other.geometry('400x240'); other.update()
        second = self.placement(other, '2')
        self.assertTrue(reset_positions(self.path).result(timeout=2))
        self.pump_until(lambda: all(p.token[0] == 1 and not p.settling for p in (first, second)))
        for window, camera in [(self.root, '1'), (other, '2')]:
            expected = visible_geometry(camera, None, (400, 240), SCREENS)[2:]
            self.assertEqual((window.winfo_x(), window.winfo_y()), expected)
            self.assertIsNone(PositionStore(self.path).read(camera)[1])
        first.close(); second.close()
        self.assertTrue(first.worker.closed.wait(2))
        self.assertTrue(second.worker.closed.wait(2))
        self.assertIsNone(PositionStore(self.path).read('1')[1])
        self.placement(self.root)
        self.assertEqual((self.root.winfo_x(), self.root.winfo_y()),
                         visible_geometry('1', None, (400, 240), SCREENS)[2:])

    def test_disconnect_or_rearrange_displays_recovers_already_open_window(self):
        PositionStore(self.path).save(1, 0, screens_signature(SCREENS), -1500, -100)
        placement = self.placement()
        for screens in [(PRIMARY,), (PRIMARY, ABOVE)]:
            self.screens = screens
            self.pump_until(lambda: placement.screens == screens and not placement.settling)
            self.assertGreaterEqual(self.root.winfo_x(), 0)
            self.assertGreaterEqual(self.root.winfo_y(), 0)

    def test_minimize_does_not_overwrite_normal_position(self):
        placement = self.placement()
        self.root.geometry('+330+210'); self.root.update()
        self.pump_until(lambda: PositionStore(self.path).read(1)[1] is not None)
        self.root.iconify(); self.root.update()
        placement.close()
        self.assertTrue(placement.worker.closed.wait(2))
        self.assertEqual(PositionStore(self.path).read(1)[1][1:], (330, 210, 400, 240))

    def test_video_resize_is_fitted_without_changing_native_surface(self):
        placement = self.placement()
        hwnd = self.root.winfo_id()
        placement.resize_for_video(800, 1400)
        self.pump_until(lambda: self.root.winfo_height() < 1040 and not placement.settling)
        self.assertEqual(self.root.winfo_id(), hwnd)
        self.assertLessEqual(self.root.winfo_rooty()+self.root.winfo_height(), 1040)

    def test_snap_resize_is_observed_without_rewriting_windows_geometry(self):
        placement = self.placement()
        # Snap can put its invisible resize border beyond the work-area edge.
        self.root.geometry('958x1008+-7+0')
        self.root.update()
        snapped = placement._geometry()
        with patch.object(self.root, 'geometry', wraps=self.root.geometry) as geometry:
            self.pump_until(lambda: placement.last == snapped)
            for _ in range(5):
                placement._sample()
            geometry.assert_not_called()
        self.assertTrue(placement.user_placed)
        self.pump_until(lambda: PositionStore(self.path).read(1)[1] is not None)
        self.assertEqual(PositionStore(self.path).read(1)[1][1:], (-7, 0, *snapped[:2]))

    def test_early_snap_is_preserved_when_initial_display_query_finishes(self):
        PositionStore(self.path).save(1, 0, screens_signature(SCREENS), 300, 200, 640, 410)
        entered, release = threading.Event(), threading.Event()
        def slow_screens():
            entered.set()
            release.wait(2)
            return SCREENS
        placement = WindowPlacement(self.root, '1', path=self.path, screen_provider=slow_screens)
        self.placements.append(placement)
        try:
            self.assertTrue(entered.wait(1))
            self.root.geometry('958x1008+-7+0')
            self.root.update()
            snapped = placement._geometry()
            with patch.object(self.root, 'geometry', wraps=self.root.geometry) as geometry:
                release.set()
                self.pump_until(lambda: placement.token is not None)
                geometry.assert_not_called()
            self.assertEqual(placement._geometry(), snapped)
            self.pump_until(lambda: PositionStore(self.path).read(1)[1][3:] == snapped[:2])
        finally:
            release.set()

    def test_snap_just_after_restore_is_not_lost_during_settling(self):
        placement = self.placement()
        placement._place(None)
        self.root.update()
        self.root.geometry('958x1008+-7+0')
        self.root.update()
        with patch.object(self.root, 'geometry', wraps=self.root.geometry) as geometry:
            placement.resize_for_video(800, 490)
            geometry.assert_not_called()
        self.assertTrue(placement.user_placed)

    def test_reset_still_recovers_a_snapped_window(self):
        placement = self.placement()
        self.root.geometry('958x1008+-7+0')
        self.root.update()
        self.pump_until(lambda: placement.user_placed)
        PositionStore(self.path).reset()
        self.pump_until(lambda: placement.token[0] == 1 and not placement.settling)
        self.assertGreaterEqual(self.root.winfo_x(), 0)
        self.assertLessEqual(self.root.winfo_rooty()+self.root.winfo_height(), 1040)
        self.assertIsNone(PositionStore(self.path).read(1)[1])

    def test_ordinary_resize_and_close_never_correct_user_geometry(self):
        placement = self.placement()
        self.root.geometry('680x430+340+220')
        self.root.update()
        with patch.object(self.root, 'geometry', wraps=self.root.geometry) as geometry:
            placement.close()
            geometry.assert_not_called()
        self.assertTrue(placement.worker.closed.wait(2))
        self.assertEqual(PositionStore(self.path).read(1)[1][1:], (340, 220, 680, 430))

    def test_destroy_stops_worker_and_cancels_poll(self):
        placement = self.placement()
        window = tk.Toplevel(self.root)
        self.windows.append(window)
        window.geometry('400x240'); window.update()
        child = self.placement(window, '2')
        window.destroy()
        self.assertTrue(child.worker.closed.wait(2))
        self.assertIsNone(child.timer)
        self.assertFalse(placement.stopping)

    @unittest.skipUnless(os.name == 'nt', 'Windows monitor enumeration')
    def test_real_windows_monitor_enumeration_has_usable_work_areas(self):
        screens = windows_screens()
        self.assertTrue(screens)
        self.assertTrue(any(s.primary for s in screens))
        for screen in screens:
            self.assertLess(screen.work[0], screen.work[2])
            self.assertLess(screen.work[1], screen.work[3])


class PlayerProcessPlacementTests(unittest.TestCase):
    def test_real_player_processes_restart_reset_and_close_on_parent_eof(self):
        # Real Tk + real parent lifetime pipe, simulated media and monitor layout.
        code = r'''
import json, sys
from pathlib import Path
from unittest.mock import Mock
from child_processes import parent_lifetime
from player_supervisor import PlayerSnapshot
from player_vilkin_hikvision import VideoPlayer
from window_positions import Screen, WindowPlacement, PositionStore
camera, mode, marker, database = sys.argv[1:]
owner = Mock()
owner.snapshot = PlayerSnapshot()
owner.closed.is_set.return_value = True
screen = Screen('simulated', (0,0,1920,1080), (0,0,1920,1040), True)
app = VideoPlayer(camera, supervisor_factory=lambda *a: owner,
    placement_factory=lambda root, ident: WindowPlacement(root, ident, path=database,
        screen_provider=lambda: (screen,)))
parent_lifetime.bind(app.root, app.on_closing)
moved = False
reported = None
def poll():
    global moved, reported
    placement = app.placement
    if placement.token is not None and not placement.settling:
        if mode == 'move' and not moved:
            app.root.geometry('640x410+350+220')
            moved = True
        elif (mode != 'move' or reported is not None or PositionStore(database).read(camera)[1] is not None):
            generation = placement.token[0]
            if generation != reported:
                if mode == 'restore' and reported is None:
                    owner.snapshot = PlayerSnapshot(state='PLAYING', width=1920, height=1080)
                    app.root.after_cancel(app._timer)
                    app.check_stream_status()
                    app.root.update_idletasks()
                    assert app._sized
                Path(marker).write_text(json.dumps([generation, app.root.winfo_x(), app.root.winfo_y(),
                                                   app.root.winfo_width(), app.root.winfo_height()]))
                reported = generation
    if not app.closing:
        app.root.after(50, poll)
app.root.after(100, poll)
app.root.after(12000, app.on_closing)
app.run()
assert app.placement.worker.closed.wait(2)
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'positions.db'
            processes = []
            env = dict(os.environ, CAMERA_PARENT_PIPE='1')

            def start(camera, mode, name):
                marker = Path(directory) / name
                process = subprocess.Popen([sys.executable, '-c', code, camera, mode, str(marker), str(path)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                processes.append(process)
                eventually(marker.exists, timeout=5)
                return process, marker

            def stop(process):
                process.stdin.close()
                process.wait(timeout=5)
                self.assertEqual(process.returncode, 0, process.stderr.read().decode())

            try:
                initial, marker = start('1', 'move', 'initial.json')
                self.assertEqual(json.loads(marker.read_text()), [0, 350, 220, 640, 410])
                stop(initial)
                restored, marker1 = start('1', 'restore', 'restored.json')
                self.assertEqual(json.loads(marker1.read_text()), [0, 350, 220, 640, 410])
                other, marker2 = start('2', 'move', 'other.json')
                PositionStore(path).reset()
                def reset_seen(marker):
                    try:
                        return json.loads(marker.read_text())[0] == 1
                    except (ValueError, OSError):
                        return False
                eventually(lambda: reset_seen(marker1) and reset_seen(marker2), timeout=4)
                self.assertNotEqual(json.loads(marker1.read_text())[1:3], [350, 220])
                self.assertNotEqual(json.loads(marker2.read_text())[1:3], [350, 220])
                stop(restored)
                stop(other)
                self.assertEqual(PositionStore(path).read(1), (1, None))
                self.assertEqual(PositionStore(path).read(2), (1, None))
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=3)
                    for pipe in (process.stdin, process.stdout, process.stderr):
                        pipe.close()


if __name__ == '__main__':
    unittest.main()
