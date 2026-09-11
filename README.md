# Camera Management System — r9 B candidate

> **Branche de test, PR #4 en brouillon.** La r8 est retirée après régressions
> terrain et conservée sans correctif dans la [PR #8](https://github.com/THET1TAN/camera-management-system/pull/8).
> `main` revient à la base publique précédente. Voir [le bilan r8](docs/ptz-r8-withdrawal.md).

La r9 B essaie une vitesse entièrement nulle lors du relâchement d’un axe, puis
réapplique tous les axes encore maintenus. Les variations d’amplitude de même
signe restent directes et les commandes inchangées suivent le délai natif ONVIF.

**Premier essai utilisateur, 11 septembre 2026 :** les relâchements fonctionnent
et le déplacement avec zoom simultané est confirmé. Une pause au relâchement
reste perceptible. C’est une candidate prometteuse, encore à éprouver en usage
prolongé ; aucune validation universelle ni passage sur main n’est annoncé.

Voir [les recherches, le fonctionnement et le protocole de test](docs/ptz-release-investigation.md).
Le dossier `~v0.2.6` conserve la r8 historique retirée ; les fichiers actifs de
cette branche contiennent l’expérience r9 B.

## Installation

Requirements:

- Windows and Python 3.9 or newer, including Tkinter.
- VLC installed with the same architecture as Python for video playback.
- Network access to the camera and ONVIF enabled. The current controller connects
  to the camera's ONVIF service on port 80.

```powershell
git clone https://github.com/THET1TAN/camera-management-system.git
cd camera-management-system
git switch investigate/ptz-release-pause
python -m pip install -r requirements.txt
```

For a **new installation**, initialize the database and add a camera first:

```powershell
python camera_manager.py
```

Enter the camera address and credentials, enable PTZ if supported, and save.
Then launch the viewer:

```powershell
python camera_viewer_direct_test.py
```

The viewer offers video playback, PTZ control and access to Camera Manager.
Close existing application windows before upgrading so new windows use the same
source version. No rebuilt executable is included in this release; use the Python
sources. Child windows use the same Python interpreter as their parent Viewer.
Install the dependencies for the interpreter used to launch this checkout.

## Keyboard PTZ controls

Click the PTZ window to give it keyboard focus. Its title identifies
`v0.2.6 r9 - Neutral transition test B`. Controls apply only while that window is active.

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

The test launcher enables a whole neutral ContinuousMove before reapplying the
latest held axes on partial release or reversal. The initial user test confirms
correct releases and simultaneous zoom, with a remaining pause. The previous
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

The r9 B suite contains **144 passing tests**, including latest input during slow
responses, neutral transitions, native renewals, keyboard release recovery and
child shutdown. Real ONVIF/Zeep/Requests serialization and hidden Tk startup were
also checked. These tests do not establish long-term physical reliability.

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
