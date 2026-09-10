# 📹 Camera Management System

A comprehensive Python-based camera management system with PTZ (Pan-Tilt-Zoom) control, ONVIF support, and encrypted credential storage.

## Test version: v0.2.6 r7

Keyboard PTZ control now tracks each held key independently. For example, hold
Down + Right, then release Down: the camera receives a horizontal-only command
while Right remains held. Windows also checks held keys every 30 ms to recover
missed release events. Releasing all PTZ keys sends an explicit ONVIF stop;
leaving the control window or closing it clears all held controls and requests
both PTZ and focus stops.

Releasing or reversing an axis now updates the complete velocity directly.
Released axes are explicitly zeroed in that request while held axes keep moving.
This removes the intermediate Pan/Tilt Stop that paused the remaining direction
for approximately 220–270 ms in the reported r6 session. A full Stop still handles
complete release, loss of focus, shutdown, preset cancellation and uncertain state
after an error. For devices that ignore zero velocity, the previous stop/resume
behavior remains available with `CAMERA_PTZ_CONSERVATIVE_STOPS=1` before launch.

Pan/tilt and zoom are transmitted together in one ONVIF ContinuousMove request,
including every renewal. The separate requests used in r4 made devices that
replace omitted groups alternate between movement and zoom. Active groups and
groups being explicitly zeroed are included together. This uses
standard ONVIF without manufacturer-specific branches or SDKs. Simulated tests
cover standard behavior and replacement of omitted groups. Simultaneous motion
on the reported camera is still unresolved after the r5 hardware test. Its local
trace shows all three requested velocities sent together and requests acknowledged,
without an intervening Stop while the keys remain held. Successful presets
demonstrate mechanical ability, but do not establish ContinuousMove behavior.
See [ONVIF PTZ section 5.3.3](https://www.onvif.org/specs/srv/ptz/ONVIF-PTZ-Service-Spec-v250a.pdf).

Revision 6 reads the advertised continuous velocity spaces and ranges, selects
the profile's supported defaults (or an advertised alternative), and includes
their URIs explicitly in each move. Signed speeds are scaled into those ranges.
Missing or invalid capability metadata preserves the previous normalized requests.
The local trace also inspects the serialized SOAP velocity fields without storing
the authentication header or camera addresses. The real ONVIF/Zeep serialization
has been checked offline. The r6 hardware test briefly combines movement and zoom,
but zoom stops before either end of its range while the requested axes remain held.
There is no confirmed physical fix yet. A read-only GetStatus probe could not
obtain usable position feedback or verify the camera's internal timer.
The user also confirms optical zoom plus lateral movement in TinyCam configured
for ONVIF Profile S: the zoom is visible in Camera Viewer as well. The exact
TinyCam commands are not captured. Both camera media profiles advertise a default
movement timeout of 60 seconds; this application explicitly requests one second.

Revision 7 schedules renewal from request transmission, instead of response
arrival. A simulated 0.8-second response previously left a gap in a one-second
camera timeout; renewal now occurs immediately when overdue and uses current input.
The existing short timeout remains unchanged. This fixes the measured scheduling
defect, but the faster replies in the r6 trace do not establish it as the cause of
the zoom interruption. Physical validation is still required.

Closing Camera Viewer now closes all its video players, PTZ controllers and
Camera Manager windows, including their children. Each child receives a graceful
shutdown request; PTZ sends its stop requests before exiting. Shutdown keeps Tk
responsive and forcibly ends an unresponsive owned child after 10 seconds.
Auxiliary windows launched independently remain independent.

A bounded local `ptz_control_<process-id>.log` records requested axes, outgoing
PTZ commands and whether requests succeeded. It contains no camera addresses,
credentials, profile tokens or SOAP payloads. No diagnostic text is added to the
control window. These logs stay outside the version snapshot and GitHub.

Revision 3 processes ONVIF requests in one background worker so the keyboard
remains responsive during network calls. Only the latest complete input state is
kept. After each camera response, the worker reads that state again before the
next command, including between Stop and resume. Rapid direction changes cannot
build a queue of old movements. If input updates stop for 0.5 seconds, the worker
requests an explicit stop.

Continuous movement uses a short renewable duration within the camera's advertised
timeout range. When capabilities cannot be read, the camera's declared default
duration is used if available; otherwise its implicit default remains in effect.
Renewals always use current input. Network operation timeouts and retry delays
also keep errors from blocking the keyboard.

Close existing Viewer and auxiliary windows, then restart `python camera_viewer.py`
from the root to load the parent/child changes. The PTZ title must contain
`v0.2.6 r7`. This version is available for review in
[PR #3](https://github.com/THET1TAN/camera-management-system/pull/3).

The current source files are at the repository root, with an identical source
snapshot in [`~v0.2.6/`](https://github.com/THET1TAN/camera-management-system/tree/fix/ptz-keyboard-v0.2.6/~v0.2.6). See the
[release notes and camera verification steps](./note_de_version-v0.2.6.txt).
Camera databases, logs and generated executables are not part of the snapshot.

## ✨ Features

- **🔐 Secure Credential Management**: Encrypted storage of camera credentials using Fernet encryption
- **📺 Camera Viewer**: Browse and play camera streams with an intuitive GUI
- **🎮 PTZ Control**: Real-time Pan-Tilt-Zoom control with keyboard shortcuts
- **🔧 Camera Manager**: Add, edit, and delete camera configurations
- **📡 ONVIF Support**: Standard camera services, subject to the capabilities supported by each device
- **💾 SQLite Database**: Lightweight local database for camera storage
- **🖱️ User-Friendly Interface**: Clean Tkinter-based GUI for all operations

## 🚀 Quick Start

### Prerequisites

- Python 3.9+ (recommended for optimal ONVIF compatibility)
- Windows OS (current implementation)

### Installation

1. **Clone the repository**
   ```bash
   git clone https://github.com/THET1TAN/camera-management-system.git
   cd camera-management-system
   ```

2. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

3. **Run the application**
   ```bash
   python camera_viewer.py
   ```

## 📋 Usage

### Camera Viewer
Launch the main application to view and manage your cameras:
```bash
python camera_viewer.py
```

- **View Cameras**: Browse all configured cameras
- **Play Stream**: Click "Play" to start camera stream
- **PTZ Control**: Click "PTZ" for cameras with pan-tilt-zoom capabilities
- **Manage**: Access camera management interface

### Camera Manager
Add and configure new cameras:
```bash
python camera_manager.py
```

- **Add Camera**: Configure IP, username, password, and PTZ capabilities
- **Edit Camera**: Modify existing camera settings
- **Delete Camera**: Remove cameras from the system

### PTZ Control
Control camera movement with keyboard shortcuts:

| Key | Action |
|-----|--------|
| `W/A/S/D` or `Arrow Keys` | Pan and Tilt |
| `Shift/Ctrl` | Zoom In/Out |
| `Q/E` | Focus In/Out |
| `M/N` | Increase/Decrease Speed |
| `1-9` | Camera Presets |
| `ESC` | Exit |

Multiple directions, zoom and focus can be held together. Opposite keys on the
same axis use the most recently pressed key; releasing it resumes the other key
if it is still held. Keyboard auto-repeat does not change that priority.

### Tests

Run the keyboard and simulated ONVIF regression tests without connecting a camera:

```bash
python -m unittest discover -s tests -v
```

Tests use Python's standard library and Tkinter. The physical camera still needs
the short verification procedure in the release notes to confirm its response.

## 🏗️ Architecture

### Core Components

- **`camera_viewer.py`**: Main application interface
- **`camera_manager.py`**: Camera configuration management
- **`ptz_keyboard_control.py`**: Real-time PTZ control interface
- **`ptz_command_worker.py`**: Serialized ONVIF requests using the latest keyboard state
- **`ptz_velocity.py`**: Advertised velocity spaces and signed speed scaling
- **`child_processes.py`**: Parent/child lifetime and graceful cascading shutdown
- **`ptz_diagnostics.py`**: Local PTZ command trace without connection details
- **`camera_key.py`**: Installation key loaded from local configuration
- **`player_vilkin_hikvision.py`**: Video stream player (Hikvision optimized)

### Security Features

- **Fernet Encryption**: All credentials are encrypted before database storage
- **Local Storage**: Data remains on your local machine
- **Secure Key Management**: Encryption keys are handled securely

### Database Schema

```sql
CREATE TABLE cameras (
    id INTEGER PRIMARY KEY,
    ip TEXT,           -- Encrypted IP address
    username TEXT,     -- Encrypted username
    password TEXT,     -- Encrypted password
    ptz INTEGER        -- PTZ capability flag (0/1)
);
```

## 🔧 Configuration

### Encryption Key
The application reads `.camera_encryption.key` beside its scripts, or the
`CAMERA_ENCRYPTION_KEY` environment variable. A source snapshot under `~v0.2.6`
can use the key in the parent application folder. The key file is excluded from
GitHub and the source snapshot. Keep it with your database backups.

For an existing installation, copy the existing key into that local file before
replacing old source files. Do not generate a different key for an existing
database: it would prevent decryption. This workspace's original key has been
preserved in the local file without modifying the database.

A fresh installation with no database generates its own key. If a database
already exists and no key is configured, startup stops with recovery instructions
instead of silently creating an incompatible replacement key.

### Python Version Management
The application automatically detects and uses Python 3.9 for ONVIF operations:

```python
def get_python39():
    python_exe = shutil.which("python3.9")
    if not python_exe:
        try:
            python_exe = subprocess.check_output(
                ["py", "-3.9", "-c", "import sys; print(sys.executable)"]
            ).decode().strip()
        except Exception:
            python_exe = sys.executable
    return python_exe
```

## 📦 Dependencies

- **cryptography**: Secure credential encryption
- **python-vlc**: Video stream playback
- **onvif-zeep**: ONVIF camera communication
- **zeep**: SOAP web services
- **lxml**: XML processing
- **requests**: HTTP communications
- **tkinter**: GUI framework (included with Python)

## 🎯 Supported Cameras

- **ONVIF-compliant cameras** (primary support)
- **Hikvision cameras** (optimized support)
- **Generic IP cameras** with RTSP streams

## 🔒 Security Considerations

- All camera credentials are encrypted using Fernet (AES 128)
- Database file is stored locally with encrypted content
- No network transmission of plain-text credentials
- Consider implementing per-user encryption keys for multi-user environments

## 🚧 Future Enhancements

- [ ] Multi-user support with individual encryption keys
- [ ] Web-based interface
- [ ] Camera group management
- [ ] Recording and playback features
- [ ] Motion detection alerts
- [ ] Mobile app companion

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## 📝 License

This project is licensed under the [Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International License](https://creativecommons.org/licenses/by-nc-sa/4.0/).

## 🆘 Support

If you encounter any issues or have questions:

1. Check the [Issues](https://github.com/THET1TAN/camera-management-system/issues) page
2. Create a new issue with detailed information
3. Include system information and error logs

## 📞 Contact

- **Author**: Joël Smith-Gravel
- **Email**: joel.smith-gravel@hotmail.ca
- **GitHub**: [@THET1TAN](https://github.com/THET1TAN)

---

⭐ **Star this repository if you find it helpful!**
