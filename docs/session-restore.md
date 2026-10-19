# Session restoration — v0.2.11 / issue #18

Introduced in v0.2.11 and included in the current v0.2.12 application. The new
release also migrates credentials and saved layout into the local Windows
profile; see [credential protection](windows-credentials.md). Camera IDs and
session behavior are preserved. Earlier source snapshots remain unchanged.

## Start and qualify

Close earlier Viewer windows first. From the application folder, in PowerShell:

```powershell
.\Lancer-Camera.cmd
```

The launcher uses the existing Python 3.9 installation and dependencies. The
Viewer title displays `v0.2.12`. The former issue-18 launcher opens this same main
application. Alternatively:

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python39\python.exe" -u .\camera_viewer.py
```

1. Open two different cameras and two video windows for the same camera. Move
   and resize each window separately. Open some PTZ controllers and move them.
2. Mute selected video windows. Their speaker button must become a soft red;
   unmuting restores the usual gray button and updates the window title.
3. Close **Camera Viewer itself**, leaving the children open. It closes its
   children normally while preserving the session to restore.
4. Run the same command and click **Restore All**. Check each video's size,
   location and audio, plus the PTZ controllers' location and size. Restoration
   must not pan, tilt, zoom, focus, recall a preset or recenter a camera.
5. Click again several times. Existing windows keep their current audio/layout;
   no additional instances should appear. Also try opening one camera manually
   before the first restoration: it counts toward the saved number of windows.
6. Close one child window manually, then close and relaunch Viewer. That child
   must no longer belong to the saved session. Reopening its slot manually keeps
   its last mute preference and layout.
7. Test a removed camera, disabled PTZ, an unavailable stream, and an unplugged
   or repositioned monitor. Other windows remain usable. Saved windows are fitted
   to visible monitor work areas using the existing placement mechanism.
8. Reset window positions. Open windows return to visible defaults; the list of
   session windows and their mute flags remain. PTZ now participates in placement
   and position resets too.

## Persistence and lifecycle

In v0.2.12, the active database is
`%LOCALAPPDATA%\CameraManagementSystem\camera_window_positions.db`; the existing
layout/session database is copied during migration. `camera_window_positions.db`
retains its old schema and gains separate
`viewer_session`, `session_windows` and `window_audio` tables. Session records
contain only camera database IDs, opaque window IDs, video/PTZ kinds, instance
slots and mute flags. Geometry remains in the existing position/size tables.
No addresses, passwords, authenticated URLs or camera control state are saved.
The existing Git ignore rule covers the database and its SQLite sidecars.

Each instance has a stable layout key (`video:<camera-id>:<slot>` or
`ptz:<camera-id>:<slot>`). Video falls back to the old per-camera placement when
no instance placement exists and continues updating that legacy placement.
Standalone video and Camera Manager remain compatible with the old layout keys;
session membership covers the windows launched by this Camera Viewer.

Opening Viewer reads the saved session without clearing it. Until the first
restore, that startup snapshot remains available even if windows were opened
manually. The first opened window starts the new saved session; restoration adds
missing instances to those already open. Later restore clicks use the updated
session, including failed windows eligible for a retry. Manual child closure
removes membership; parent shutdown freezes membership and children independently
recognize parent EOF. Abnormal child exits keep their slot available for retry.
Late reports from a replaced window or older Viewer cannot remove its replacement.
When multiple Viewers are used, the last one to begin a new session owns storage;
an older Viewer remains usable and displays a storage warning if it tries to save.

Database reads/writes and monitor enumeration run outside Tk. A failed or corrupt
database is not replaced. Viewer reports unavailable storage and can still open
windows. Child reports retry transient database failures, with a bounded final
flush. A crash, persistent storage failure, or forced shutdown may lose the latest
change. Startup failures identify the camera by database ID without exposing
exception text or credentials. RTSP outages retain the existing player recovery UI.

Mute is passed to the supervisor **before its thread starts**, included in the
first worker configuration and retained during reconnects. The native worker
requests mute and zero volume before playback, then reapplies the saved audio
state after playback starts. libVLC documents that some outputs ignore pre-play
mute or do not support muting, particularly digital passthrough. Actual silent
startup and audible unmute still require the user's camera/audio test.

## Validation

The v0.2.11 publication checkout passed the complete **446-test** suite on
Windows / Python 3.14 in **116.756 seconds**. Before promotion, the final session
and English-interface revision passed **38 tests** on the installed Python
3.9.13. Public Python sources parse under Python 3.9, and the version snapshot
matches the active sources. Test databases, synthetic services and caches were
isolated from the installation's real data. The full run used local loopback
services outside the network-restricted test sandbox. Evidence stays locally in
`.release-work/v0.2.11-release-tests.txt` and `.release-work/english-ui-tests.txt`.

Automated coverage exercises persistence in another Python process, multiple
instances, ownership, manual/global closure, reset isolation, corrupt/locked
storage, retries, partial restoration, repeated clicks, independent geometry,
initial audio configuration and a real PTZ entry point with offline services.
The PTZ restoration test checks that no movement or preset request is sent.
Physical camera behavior, speaker output and actual monitor hotplug remain to
be qualified using the steps above; automated results do not establish universal
camera or audio-output compatibility.
