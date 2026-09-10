# Camera Management System

Windows desktop application for managing IP cameras, viewing video streams and
controlling pan, tilt, optical zoom and focus with the keyboard.

## Current version: v0.2.6 — PTZ r8

The application sources at the repository root are the current version on `main`.
An identical source snapshot is kept in [`~v0.2.6/`](./~v0.2.6/).
See the [release notes](./note_de_version-v0.2.6.txt) for changes and validation.

This version provides:

- Independent tracking of held keys, including diagonals combined with zoom.
- Recovery of missed key releases on Windows and replacement of obsolete requests
  during rapid direction changes.
- ONVIF velocity spaces, speed ranges and movement timeouts selected from the
  camera's advertised capabilities, without a manufacturer-specific PTZ branch.
- Explicit stops on release, loss of focus and shutdown.
- Cascading closure of video players, PTZ windows and Camera Manager when their
  parent Camera Viewer closes.
- Local camera configuration in SQLite, with credentials encrypted using Fernet.

The r8 camera test confirmed sustained zoom with a diagonal and correct direction
releases. **A short pause remains when releasing one direction of a diagonal.**
Pan and tilt share an ONVIF stop group; omitting that stop reintroduced stuck
movement on the tested camera. Further work on this pause is kept separate from
this validated version. Compatibility with other devices still needs testing.

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
sources. If Python 3.9 is installed, child scripts may select it automatically;
install the dependencies for that interpreter as well.

## Keyboard PTZ controls

Click the PTZ window to give it keyboard focus. Its title identifies
`v0.2.6 r8`. Controls apply only while that window is active.

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

Targeted Stop followed by resumption is the default for cameras that ignore zero
velocity. For a device **verified** to handle zero axes correctly, direct
transitions can be enabled with `CAMERA_PTZ_CONSERVATIVE_STOPS=0` before launch.
This optional mode failed on the reported camera; the normal setting preserves
reliable releases at the cost of the short transition pause.

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
if that environment variable is set. A source snapshot named `~v0.2.6` can also
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
python -m unittest discover -s tests
```

The v0.2.6 r8 suite contains **114 passing tests**, covering keyboard combinations,
rapid input changes, delayed responses, native timeouts, explicit stops, local key
handling and parent/child shutdown. Tests use simulated camera services and do
not move a physical camera. Real ONVIF/Zeep serialization and Tk startup/shutdown
were also checked. Hardware validation applies to the camera tested, not to all
ONVIF devices.

## Source layout

| File | Purpose |
| --- | --- |
| `camera_viewer.py` | Main camera list and launch controls |
| `camera_manager.py` | Add, edit and remove camera configurations |
| `player_vilkin_hikvision.py` | VLC video player and ONVIF stream discovery |
| `ptz_keyboard_control.py` | PTZ window and physical key tracking |
| `ptz_command_worker.py` | Latest-state ONVIF commands and stops |
| `ptz_velocity.py` | Advertised velocity spaces and speed scaling |
| `child_processes.py` | Cascading lifetime of child processes |
| `camera_key.py` | Local encryption key loading |
| `ptz_diagnostics.py` | Bounded command and serialized-velocity logs |
| `tests/` | Automated regression tests |
| `~v0.2.6/` | Source snapshot of the current release |

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
