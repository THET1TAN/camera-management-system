# Camera Management System

## Development: v0.2.11-dev — microSD archive playback

Root sources now contain the **unqualified development implementation for issue #15**:
an integrated archive window, camera-badge calendar, timeline, ISAPI/VideoLink
queries, local cache, progressive MPEG/HLS preparation, playback controls and
original/range export. The stable **v0.2.10** sources remain in `~v0.2.10`.

**The current progressive/scrubbing and native-boundary changes await user-run qualification.**
The [native-boundary investigation and two-file bench](docs/archive-playback-boundaries.md)
records existing C1/C2 evidence, candidate clock/frame-confirmation fixes and
the A/B/C comparison commands. The 84 prepared Playback tests have not been run
on this revision. Root sources remain synchronized with draft PR #16.
The user passed all 44 earlier Playback tests under Python 3.9.13. C3/101 returns
64 archives; C3/103 returns track 101 and is rejected as `track-mismatch`. Auto
preserves the 64 usable archives as partial coverage. These are metadata results,
not validation of progressive start, audio or scrubbing. Additional regressions
and a slowed synthetic bench are prepared; the assistant has not launched tests,
media, applications or camera requests. Cross-archive range export remains
outstanding; this is not a release or a claim that issue #15 is complete.

Use **Recordings** in Camera Viewer, or `Lancer-Enregistrements.cmd` for the
standalone archive browser. Both Windows launchers default to the user's
`%LOCALAPPDATA%\Programs\Python\Python39\python.exe`; set `CAMERA_PYTHON` to
override that explicit path. They do not inject temporary test dependencies.
Opening its calendar does not start a video. Add the
new `tzdata` dependency using the same Python interpreter as the application.
See [behavior, design and limits](docs/archive-playback.md) and
[commands and test sequence](docs/archive-playback-testing.md).
See the [progressive start, scrubbing and English UI trial](docs/archive-playback-progressive.md)
and [environment and one-camera diagnostics](docs/archive-playback-diagnostics.md).

## Current release: v0.2.10 — remembered window layout

Released September 18, 2026, with the changes from
[PR #13](https://github.com/THET1TAN/camera-management-system/pull/13), resolving
[issue #6](https://github.com/THET1TAN/camera-management-system/issues/6): video
windows remember their position and resized dimensions for each camera, including
after restarting Camera Viewer.

- **Arrange with Windows Snap:** snapping and manual resizing stay active.
  Reopening restores the chosen size without letting the first video overwrite it.
- **Keep edge alignment:** windows placed flush with an edge retain that alignment
  on restoration, including portrait screens, without added side margins.
- **Recover a visible layout:** if the monitor arrangement changes, windows return
  to visible positions on the current primary screen.
- **Reset the layout:** choose **Réinitialiser la position des fenêtres** below
  **Manage Cameras** to clear saved positions and sizes and reposition open players.
  Open players keep their current size where it fits; future openings use defaults.

Positions and sizes are restored when the monitor layout matches. Snap groups
and maximized/minimized states are not saved. Layout data stays locally in
`camera_window_positions.db`, separate from camera credentials and excluded from
Git. See the [window layout guide](docs/window-positions.md) for behavior and limits.

[`~v0.2.10`](https://github.com/THET1TAN/camera-management-system/tree/main/~v0.2.10)
preserves this stable version. See the [v0.2.10 release notes](note_de_version-v0.2.10.txt).
Previous snapshots remain unchanged. No rebuilt executable is included.

## Previous release: v0.2.9 — nonblocking RTSP recovery

Released September 18, 2026, through
[PR #12](https://github.com/THET1TAN/camera-management-system/pull/12), resolving
[issue #9](https://github.com/THET1TAN/camera-management-system/issues/9).

- **Automatic RTSP recovery:** an interrupted stream reconnects when the source
  returns. Supervised VLC sessions keep the window responsive and closable even
  when a native playback operation hangs.
- **Visible connection status:** a status message covers a stale image during
  an outage; recovery is confirmed by new decoded and displayed frames.
- **Bounded diagnostics:** rotating logs and terminal output record session
  changes, retry attempts and native operations to help diagnose failures.

[`~v0.2.9`](https://github.com/THET1TAN/camera-management-system/tree/main/~v0.2.9)
preserves this release; root sources also include the window-layout improvements
described above. Older source snapshots remain unchanged. The version
folders exclude private databases, keys, local configuration and logs.
For an existing installation, launch `python camera_viewer.py` from the active
application directory with its existing database and dependencies.

See the [v0.2.9 release notes](note_de_version-v0.2.9.txt),
[architecture, diagnostics and reproducible tests](docs/player-recovery.md),
and [validation results and remaining limits](docs/player-recovery-results.md).

## Camera availability

Introduced September 16, 2026 through
[PR #11](https://github.com/THET1TAN/camera-management-system/pull/11), implementing
[issue #10](https://github.com/THET1TAN/camera-management-system/issues/10).
The previous release is preserved in
[`~v0.2.8`](https://github.com/THET1TAN/camera-management-system/tree/main/~v0.2.8).

Camera Viewer now displays a colored dot and text beside each camera. A small
RTSP `OPTIONS` request checks whether the service responds, without opening video.
Only if that fails, the monitor checks the configured HTTP/ONVIF port; only if
that also fails, it pings the camera. Ping-only reachability is **Degraded**.
Two consecutive unsuccessful rounds are needed for **Unreachable**.

The footer shows one short summary for all cameras, such as `2 online · 1 unreachable`.
It stays on one line and does not change when a camera is hovered or focused.
Saved changes are reloaded after Camera Manager closes. Play, PTZ and child-window
shutdown remain available. The availability monitor does not trigger player
recovery (#9).

The indicator reports service reachability, not successful image decoding or
valid RTSP credentials. For a custom ONVIF port or RTSP address, use the optional
`camera_health.json` configuration described in the availability guide.

See [availability behavior and configuration](docs/camera-availability.md) and
[v0.2.8 release notes](note_de_version-v0.2.8.txt). Launch `python camera_viewer.py`
from the active application directory with its existing database.
The version folders are source snapshots; private databases and keys are excluded.

## PTZ controller — r9 B

Released in v0.2.7 on September 12, 2026. The r9 B controller handles partial key
releases and simultaneous camera movement with optical zoom.
Normal `camera_viewer.py` startup selects this behavior without a test launcher.

**Known limitation:** a short pause remains when releasing one axis of a diagonal.
This release preserves the tested behavior; it does not claim to eliminate that
pause or establish compatibility with every ONVIF camera.

The previous release is preserved in
[`~v0.2.7`](https://github.com/THET1TAN/camera-management-system/tree/main/~v0.2.7).
See the [v0.2.7 release notes](note_de_version-v0.2.7.txt).

r8 remains withdrawn in [draft PR #8](https://github.com/THET1TAN/camera-management-system/pull/8)
and the historical `~v0.2.6` snapshot. The C early-resume experiment is also
withdrawn; its old launcher redirects to B. See the [r8 report](docs/ptz-r8-withdrawal.md)
and [C regression report](docs/ptz-early-resume-test.md).

## Installation

Requirements:

- Windows and Python 3.9 or newer, including Tkinter.
- VLC installed with the same architecture as Python for video playback.
- Network access to the camera and ONVIF enabled. The current controller connects
  to the camera's ONVIF service on port 80.

```powershell
git clone https://github.com/THET1TAN/camera-management-system.git
cd camera-management-system
python -m pip install -r requirements.txt
```

For a **new installation**, initialize the database and add a camera first:

```powershell
python camera_manager.py
```

Enter the camera address and credentials, enable PTZ if supported, and save.
Then launch the viewer:

```powershell
python camera_viewer.py
```

The viewer offers video playback, PTZ control and access to Camera Manager.
Close existing application windows before upgrading so new windows use the same
source version. No rebuilt executable is included in this release; use the Python
sources. Child windows use the same Python interpreter as their parent Viewer.
Install the dependencies for the interpreter used to launch this checkout.

## Keyboard PTZ controls

Click the PTZ window to give it keyboard focus. Its title identifies
`v0.2.7 r9 - Neutral transition B`. Controls apply only while that window is active.

| Key | Action |
| --- | --- |
| W / S or Up / Down | Tilt up / down |
| A / D or Left / Right | Pan left / right |
| Shift / Ctrl | Zoom in / out |
| Q / E | Focus in / out |
| M / N | Increase / decrease speed |
| 1–9 | Preset shortcuts, when supported by the camera's preset tokens |
| Esc | Stop and close the PTZ window |

Hold multiple keys for combined movement. For opposite directions on the same
axis, the most recent real press takes priority; releasing it resumes the other
key if still held. Auto-repeat does not change that priority. Left/right Shift
and Ctrl are tracked independently.

A single worker sends the latest complete input state to the camera. PTZ network
calls do not block Tk's keyboard processing or build a queue of old directions.
Pan/tilt and zoom are sent together in `ContinuousMove` requests.

### Movement duration and stopping

The controller prefers the camera's declared default movement duration when it
is valid within its supported range. The tested camera declares 60 seconds.
Unchanged commands are renewed at one third of that duration, measured from
transmission time. Key changes are processed immediately, independently of that
renewal schedule.

Releasing the controls, losing focus or closing the window requests an explicit
Stop without waiting for this timeout. An absence of keyboard updates for
0.5 seconds also requests Stop. If a network failure prevents Stop from reaching
the camera, the last accepted movement may continue until the camera timeout.

The default controller sends a whole neutral ContinuousMove before reapplying the
latest held axes on partial release or reversal. Camera testing verified correct
releases and simultaneous zoom, with a remaining pause. The previous
direct and Stop/resume modes are retained only for comparison; see the test guide.

### Window ownership

Closing Camera Viewer closes the video players, PTZ controllers and Camera
Manager windows it launched, including their children. A PTZ controller requests
its movement and focus stops before exiting. Tk remains responsive during
shutdown; an unresponsive owned child is terminated after 10 seconds. Windows
launched independently remain independent.

## Existing installations and encryption key

Keep `camera_credentials.db` and its matching `.camera_encryption.key` together
when backing up or moving an installation. These local files are excluded from
GitHub and the source snapshot.

The application reads the key beside its scripts, or from `CAMERA_ENCRYPTION_KEY`
if that environment variable is set. A source snapshot such as `~v0.2.10` can also
read the key in its parent application folder.

When upgrading an older installation that embedded its key in the source, save
that **same existing key** in `.camera_encryption.key` before replacing the old
files. Changing the key does not decrypt the old database. An existing database
without a configured key stops startup with recovery instructions instead of
creating a replacement. A new installation without a database generates its own
key.

Fernet protects stored credentials; it does not establish encrypted ONVIF or RTSP
transport. Use a network appropriate for your camera's connection settings.

## Diagnostics and tests

The PTZ controller writes a local `ptz_control_<process-id>.log`, bounded to
256 KiB plus one archive. It records requested axes, serialized velocity fields,
requested movement duration and request results. It does not record camera
addresses, credentials, profile tokens or SOAP payloads. Diagnostics do not add
text to the PTZ interface, and a log failure cannot prevent Stop.

```powershell
$env:CAMERA_WINDOW_POSITIONS_FILE = Join-Path $env:TEMP ('camera-layout-tests-' + [guid]::NewGuid() + '.db')
python -m unittest discover -s tests
```

The v0.2.10 suite has **305 passing tests** on Windows, including
remembered positions and sizes, Windows frame alignment, reset behavior, storage
upgrades and concurrent players. The temporary layout database above keeps test
windows separate from the installation's saved layout.

The preserved v0.2.9 suite contains **246 passing tests**, including recovery supervision,
native-call isolation, mute persistence, smoothed bitrate and counter rollover,
ONVIF URI handling, availability checks and inherited PTZ/child-window behavior.
The playback-option regression reads the frozen v0.2.8 source; when running tests
inside a version snapshot such as `~v0.2.10`, keep the sibling `~v0.2.8` folder available.

The latest Windows/libVLC synthetic RTSP bench recovered changing video and a
numeric bitrate after both a 5-second disconnect and 30-second silence. The
witness camera stayed uninterrupted. Earlier 20-cycle results and their different
playback settings are recorded separately in the validation report.
Joël also reported recovery during a physical-camera test; the exact cause of
that camera's prior missing bitrate still requires a targeted repeat.

Player diagnostics are retained in bounded files under `player-logs/` and relayed
to the launch terminal without authenticated URIs or private SOAP payloads.
See the recovery guide for the local synthetic test command and log options.
H.265/H.265+, UDP, audible sound recovery, actual GPU removal and packaged-EXE
operation have not been validated for this change. These results do not establish
universal camera compatibility. Archived C simulations do not validate C physically.

## Source layout

| File | Purpose |
| --- | --- |
| `camera_viewer.py` | Main camera list and launch controls |
| `camera_health.py` | RTSP / HTTP / ping checks and cached ONVIF discovery |
| `camera_health_monitor.py` | Bounded background supervision and status messages |
| `camera_manager.py` | Add, edit and remove camera configurations |
| `player_vilkin_hikvision.py` | VLC video player and ONVIF stream discovery |
| `player_supervisor.py` | Nonblocking player state and bounded session-process replacement |
| `player_worker.py` | Generic ONVIF discovery and one serialized libVLC session |
| `player_metrics.py` | Five-reading bitrate average and unsigned byte-counter handling |
| `player_diagnostics.py` | Rotating logs, bounded terminal relay and optional stack capture |
| `window_positions.py` | Per-camera position/size persistence, visible screen fitting and layout reset |
| `ptz_keyboard_control.py` | PTZ window and physical key tracking |
| `ptz_command_worker.py` | Latest-state ONVIF commands and stops |
| `ptz_velocity.py` | Advertised velocity spaces and speed scaling |
| `child_processes.py` | Cascading lifetime of child processes |
| `camera_key.py` | Local encryption key loading |
| `ptz_diagnostics.py` | Bounded command and serialized-velocity logs |
| `tests/` | Automated regression tests |
| `~v0.2.10/` | Source snapshot matching the current release with remembered window layout |
| `~v0.2.9/` | Preserved previous release with nonblocking RTSP recovery |
| `~v0.2.8/` | Preserved previous release with camera availability |
| `~v0.2.7/` | Preserved previous release with PTZ r9 B |
| `~v0.2.6/` | Withdrawn historical r8 source snapshot |

## Contributing and support

Propose changes through a pull request and include relevant validation. Changes
to PTZ movement need both automated regression checks and testing on a camera
before they replace the current behavior.

Report problems in [GitHub Issues](https://github.com/THET1TAN/camera-management-system/issues)
with the application revision, reproduction steps and relevant PTZ log entries.
Do not attach your database, encryption key or camera credentials.

Author: [Joël Smith-Gravel / THET1TAN](https://github.com/THET1TAN).

## License

[Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International](./LICENSE).
Technical reference: [ONVIF PTZ service specification](https://www.onvif.org/specs/srv/ptz/ONVIF-PTZ-Service-Spec-v250a.pdf).
