# Camera window positions and sizes — issue #6

Unreleased feature, based on v0.2.9 and `main` `c1b7b77` (including the corrected
v0.2.9 release documentation). This change does not create a new release snapshot.

## Use

- Move or resize a video window and close it. Opening the same camera restores its position and size,
  including after exiting and restarting Camera Viewer.
- Positions belong to the camera's database ID, not its address or credentials.
  Different cameras keep separate positions and sizes. If several windows show the same
  camera, the last recorded move or resize wins.
- The layout includes each monitor's identity, bounds, usable work area and
  primary-screen status. Enumeration order does not matter. Negative coordinates
  on left/upper screens are supported.
- If that layout changes, open video windows move to visible default positions
  on the current primary screen. Reopening with a different layout also uses
  defaults. A saved point outside a work area is clamped to an available monitor;
  gaps between monitors and taskbars are not treated as usable screen space.
- At restoration, reset or a display-layout change, a window larger than the
  available work area is reduced to keep its title bar and controls accessible.
- Windows Snap and manual resizing remain in control while arranging windows.
  Placement polling only records changes; it does not rewrite their geometry.
  If a window is moved, resized, snapped or maximized before the first video arrives,
  its chosen size takes priority over the automatic video aspect-ratio size.
  This also applies if the initial storage/display lookup has not finished yet.
  A restored size also takes priority, even if video arrives before the initial
  storage lookup finishes. Without a recorded size, ordinary initial video sizing
  still applies and is fitted to the work area. Snap groups and maximized/minimized
  state are not persisted; reopening uses the last recorded normal-window geometry.

Choose **Réinitialiser la position des fenêtres** below **Manage Cameras**.
All recorded video positions and sizes are cleared and open players return to default positions,
normally within 350 ms (250 ms background poll plus 100 ms Tk poll). Minimized
or maximized players return to normal windows for reset or a topology change.
Open players keep their current size, reduced if necessary to fit the work area.
New openings use defaults until the user moves or resizes a window again. Default placement
and a subsequent close do not recreate the deleted records. The action applies
to video players from this installation, including standalone players or players
opened through Camera Manager. It does not reposition PTZ or Manager windows.

## Local storage and failure handling

`camera_window_positions.db` is created beside the active scripts. It contains
only camera IDs, monitor-layout signatures, coordinates and dimensions, plus a reset
generation. It is ignored by Git, along with SQLite sidecars. It is independent
of `camera_credentials.db`, `.camera_encryption.key` and `camera_health.json`.
The reset button never edits those files or sends a command to a camera.

Tests or isolated installations can set `CAMERA_WINDOW_POSITIONS_FILE` to a
different local database path. Its parent directory must already exist.
This is optional; ordinary launchers require no changes.

Existing position-only databases gain a separate size table automatically, without
discarding coordinates. Already running older players can continue writing positions.
Older reset actions also invalidate saved sizes through the shared generation.
Old records without dimensions retain the initial video aspect-ratio sizing until
the user moves or resizes the window with the updated player. Invalid dimensions
are ignored without discarding otherwise valid coordinates.

SQLite transactions preserve records from simultaneous players. Every write
includes the generation it observed; reset atomically advances the generation
and removes all positions and sizes. A pending write from before reset, including a closing
player's final sample, cannot undo the reset. Windows check for display changes
while open; positions are not just validated at launch.

Database access and Windows monitor enumeration run off Tk. Tk samples normal
window geometry every 100 ms, coalesces writes and submits a final sample before
withdrawing on close. The worker polls every 250 ms. Shutdown waits asynchronously
for the final write, for at most 750 ms, alongside existing media-process cleanup.
There are no new native media calls or calls from libVLC callbacks.

Unavailable, locked or corrupt placement storage leaves video controls usable
and uses visible defaults when no saved position can be read. A failed reset
displays a retry message; it does not claim success or overwrite a corrupt file.
Saving is retried while the window is open. A crash or persistent storage failure
can lose the latest move or resize. The usual parent/child shutdown contract still applies.

Windows uses `EnumDisplayMonitors` and `GetMonitorInfoW` work areas in the same
process DPI coordinate space as Tk; the feature does not change DPI awareness.
Other platforms use Tk's primary-screen dimensions as a fallback and do not
claim the Windows multi-monitor support. See Microsoft's
[monitor enumeration](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-enumdisplaymonitors)
and [multiple-monitor coordinates](https://learn.microsoft.com/en-us/windows/win32/gdi/multiple-monitor-system-metrics).

## Validation

On Windows / Python 3.14 / Tk 8.6, the original **246 tests pass** before the
change. The updated suite has **297 passing tests**: 51 added cases cover storage
across a new Python process, simultaneous camera writers, reset generations and
late writes, negative coordinates, monitor gaps, work areas, monitor removal and
rearrangement, resized windows, minimized windows, storage failure, reset-button
feedback, final saves and child-process shutdown. Eight follow-up regressions
cover passive observation of Snap/resize, a slow initial placement lookup,
Snap immediately after restoration, reset of a snapped window, close after
resize, and first-video arrival during placement, after Snap or maximization.
Eight size-persistence cases cover resizing without moving, restoration before or
after the first video, legacy database upgrade (including concurrent startup and
older running writers/resetters), invalid dimensions, oversized restored windows,
legacy aspect-ratio sizing and preservation of normal dimensions during maximization.
The size-persistence follow-up passed all 297 tests in 28.630 seconds in the
working checkout, with no skips.

Real Tk tests move and reopen windows. A process integration test launches real
`VideoPlayer` processes with simulated media, resizes and restarts a player, checks
that the restored 640 × 410 dimensions survive its first 1920 × 1080 video, resets two open
players through the shared store, then closes their parent pipes. Both exit and
leave the cleared positions empty. Existing recovery tests still cover EOF
shutdown with a blocked media worker. The installed monitor API is also exercised.

The initial 281-test suite passed in 23.313 seconds in the working checkout and 23.451 seconds
from the synchronized active installation, with no skips. A local smoke check
read the installation's encrypted configuration without exposing it or starting
camera traffic, verified the player launch arguments, and exercised the actual
reset button with two simulated players. A captured synthetic Viewer preview
confirmed that both footer buttons fit the existing 470 × 290 window.

Before the Snap follow-up, a Windows/libVLC bench using two loopback H.264/PCMU
streams also passed **2/2 cycles**: a 5-second disconnect and a 30-second silence. Moving video and numeric
bitrate returned after both; the witness remained on generation 1. Recovery took
12.625 / 1.910 seconds, the maximum Tk heartbeat gap was 0.507 seconds and shutdown
took 0.331 seconds with all media workers reaped. These are synthetic RTSP tests,
not physical-camera results. [Recorded results](validation/issue-6-rtsp-regression.json).

The final Snap follow-up passed **289 tests in 27.665 seconds** from the active
installation, without skips.
The previous implementation was reproduced using a real Windows mouse drag to
the right screen edge: it rewrote the snapped geometry, shifting the window and
reducing its height. With the correction, the native right-half placement remained
unchanged through polling and first-video simulation, with **zero geometry writes
after initial placement**. This was a real Windows Snap interaction on a simulated
player, with no camera traffic. [Recorded results](validation/issue-6-native-snap.json).
The regular unit tests simulate other Snap geometries; they do not exercise every
Windows Snap layout or keyboard shortcut.

The multi-screen coordinates, removal, rearrangement and small-screen scenarios
are **simulations**. No physical screen was unplugged or rearranged, and no camera
was contacted or moved for this feature. Mixed-DPI monitor transitions, physical
hot-plug behavior and packaged executables remain unverified.

Run from the source root with its dependencies installed:

```powershell
$env:CAMERA_WINDOW_POSITIONS_FILE = Join-Path $env:TEMP ('camera-layout-tests-' + [guid]::NewGuid() + '.db')
python -m unittest discover -s tests
```

The isolated path keeps the existing playback tests' synthetic camera IDs out of
the installation's normal layout database. The new placement tests create their
own temporary databases. No private camera configuration is needed for the suite.
