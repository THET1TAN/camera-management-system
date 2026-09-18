"""Opt-in native Windows/Tk/libVLC test with loopback synthetic video and audio.

Run from the checkout: python tools/test_rtsp_recovery.py --cycles 20 --output results.json
No camera credentials, firewall changes, downloaded binaries or private media.
"""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
import threading
import time
import ctypes

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from player_vilkin_hikvision import VideoPlayer
from rtsp_fixture import RTSPFixture, synthetic_frames


def process_metrics(pid):
    """Windows counters for this test's owned processes, without extra packages."""
    from ctypes import wintypes
    class Memory(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('faults', wintypes.DWORD)] + [
            (key, ctypes.c_size_t) for key in ('peak_rss', 'rss', 'peak_pool', 'pool',
                                             'peak_nonpaged', 'nonpaged', 'pagefile', 'peak_pagefile')]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Memory), wintypes.DWORD]
    handle = kernel.OpenProcess(0x410, False, pid)
    if not handle:
        return None
    try:
        memory = Memory()
        memory.cb = ctypes.sizeof(memory)
        count = wintypes.DWORD()
        psapi.GetProcessMemoryInfo(handle, ctypes.byref(memory), memory.cb)
        kernel.GetProcessHandleCount(handle, ctypes.byref(count))
        return {'rss_bytes': memory.rss, 'handles': count.value}
    finally:
        kernel.CloseHandle(handle)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cycles', type=int, default=20)
    parser.add_argument('--durations', default='5,30,120')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--ffmpeg', default='ffmpeg')
    parser.add_argument('--initial-offline', type=float, default=5)
    parser.add_argument('--capture', action='store_true', help='Keep test window on top and compare its video area (requires Pillow)')
    args = parser.parse_args()
    durations = [float(value) for value in args.durations.split(',')]
    frames, sps, pps = synthetic_frames(args.ffmpeg)
    source = RTSPFixture(frames, sps, pps)
    witness = RTSPFixture(frames, sps, pps)
    source.set_mode('offline')
    app = VideoPlayer('lab-A', uri=source.url)
    other = VideoPlayer('lab-B', uri=witness.url)
    app.root.geometry('640x420+0+0')
    other.root.geometry('480x320+660+0')
    if args.capture:
        app.root.attributes('-topmost', True)
    # Mute synthetic tone on the host. played_abuffers still measures output.
    app.toggle_mute()
    other.toggle_mute()
    results = {'scenario': 'Windows libVLC / loopback RTSP TCP / H264 + PCMU',
               'cycles': [], 'errors': [], 'initial_offline_seconds': args.initial_offline}
    started = time.monotonic()
    phase, due = 'initial', started + args.initial_offline
    cycle = 0
    heartbeat = [started]
    max_gap = 0.
    baseline = None
    captured = None
    finishing = False
    close_started = None

    def finish():
        nonlocal finishing, close_started
        if finishing:
            return
        finishing = True
        close_started = time.monotonic()
        results['max_tk_heartbeat_gap_seconds'] = max_gap
        results['witness_generation'] = other.supervisor.snapshot.generation
        results['final_snapshot'] = asdict(app.supervisor.snapshot)
        results['supervisor_threads'] = sum(t.name == 'Player supervisor' for t in threading.enumerate())
        results['status_pipe_threads'] = sum(t.name == 'VLC status pipe' for t in threading.enumerate())
        app.on_closing()
        other.on_closing()

    def tick():
        nonlocal phase, due, cycle, max_gap, baseline, captured
        try:
            now = time.monotonic()
            max_gap = max(max_gap, now-heartbeat[-1])
            heartbeat[:] = [now]
            snap = app.supervisor.snapshot
            witness_snap = other.supervisor.snapshot
            if now-started > 45 and witness_snap.state != 'PLAYING':
                raise AssertionError('Uninterrupted witness lost video')
            if witness_snap.generation > 1:
                raise AssertionError('Uninterrupted witness restarted')
            if phase == 'initial' and now >= due:
                source.set_mode('live')
                phase, due = 'initial-recovery', now + 35
                baseline = now
            elif phase == 'initial-recovery':
                if snap.state == 'PLAYING' and snap.audio > 0:
                    results['initial_recovery_seconds'] = now-baseline
                    phase, due = 'stable', now+2
                elif now >= due:
                    raise AssertionError('No initial video/audio after source returned')
            elif phase == 'stable' and now >= due:
                if args.capture and captured is not None:
                    from PIL import ImageGrab, ImageChops
                    surface = app.video_surface
                    x, y = surface.winfo_rootx(), surface.winfo_rooty()
                    current = ImageGrab.grab(bbox=(x, y, x+surface.winfo_width(), y+surface.winfo_height()), all_screens=True)
                    pixels = list(current.resize((64, 36)).getdata())
                    color_fraction = sum(max(p)-min(p) > 40 for p in pixels)/len(pixels)
                    changed = (current.size == captured.size and color_fraction > .3 and
                               ImageChops.difference(current, captured).getbbox() is not None)
                    results.setdefault('visual_checks', []).append({'cycle': cycle, 'image_changed': changed,
                        'color_fraction': color_fraction, 'surface_mapped': surface.winfo_ismapped(),
                        'status_mapped': app.status_label.winfo_ismapped()})
                    if not changed:
                        captured.save(args.output.with_name('rtsp-capture-before.png'))
                        current.save(args.output.with_name('rtsp-capture-after.png'))
                    if not changed:
                        raise AssertionError('Owned HWND capture did not show a changing image')
                    if cycle in (1, args.cycles):
                        current.save(args.output.with_name(f'rtsp-cycle-{cycle}.png'))
                    captured = None
                results.setdefault('resources', []).append({
                    'cycle': cycle, 'tk': process_metrics(__import__('os').getpid()),
                    'worker': process_metrics(app.supervisor.worker_pid) if app.supervisor.worker_pid else None,
                    'python_threads': threading.active_count(), 'rtsp_clients': len(source.clients)})
                if cycle >= args.cycles:
                    finish()
                    return
                duration = durations[cycle % len(durations)]
                mode = 'offline' if cycle % 2 == 0 else 'silence'
                results['cycles'].append({'cycle': cycle+1, 'mode': mode, 'duration': duration,
                                          'generation_before': snap.generation})
                source.set_mode(mode)
                phase, due = 'outage', now+duration
            elif phase == 'outage' and now >= due:
                source.set_mode('live')
                baseline = (now, snap.generation, snap.displayed, snap.audio)
                phase, due = 'recovery', now+35
            elif phase == 'recovery':
                fresh = snap.generation != baseline[1] or snap.displayed > baseline[2]+5
                audio = snap.audio > (0 if snap.generation != baseline[1] else baseline[3])
                if snap.state == 'PLAYING' and fresh and audio:
                    results['cycles'][-1].update(recovery_seconds=now-baseline[0],
                        generation_after=snap.generation, displayed=snap.displayed, audio=snap.audio)
                    cycle += 1
                    phase, due = 'stable', now+2
                    if args.capture:
                        phase, due = 'visual', now+.5
                elif now >= due:
                    raise AssertionError('Video/audio did not recover within 35 seconds')
            elif phase == 'visual' and now >= due:
                from PIL import ImageGrab
                surface = app.video_surface
                x, y = surface.winfo_rootx(), surface.winfo_rooty()
                captured = ImageGrab.grab(bbox=(x, y, x+surface.winfo_width(), y+surface.winfo_height()), all_screens=True)
                phase, due = 'stable', now+2
            # Exercise Tk controls while the stream is unavailable.
            if phase == 'outage':
                app.set_volume(35 if int(now) % 2 else 70)
            if now-started > 1800:
                raise AssertionError('Overall test deadline')
            app.root.after(50, tick)
        except Exception as error:
            results['errors'].append(type(error).__name__ + ': ' + str(error))
            finish()

    app.root.after(50, tick)
    try:
        app.run()
    finally:
        for player in (app, other):
            player.supervisor.close()
            player.supervisor.closed.wait(5)
        source.close()
        witness.close()
        results['elapsed_seconds'] = time.monotonic()-started
        results['close_seconds'] = time.monotonic()-close_started if close_started else None
        results['workers_reaped'] = all(player.supervisor.worker_pid is None for player in (app, other))
        if max_gap > 1:
            results['errors'].append('Tk heartbeat gap exceeded one second')
        if not results['workers_reaped']:
            results['errors'].append('Worker was not reaped')
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(results, indent=2), encoding='utf-8')
    print(json.dumps(results, indent=2))
    return 1 if results['errors'] or cycle != args.cycles else 0


if __name__ == '__main__':
    raise SystemExit(main())
