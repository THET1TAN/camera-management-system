# 📹 Camera Management System

A comprehensive Python-based camera management system with PTZ (Pan-Tilt-Zoom) control, ONVIF support, and encrypted credential storage.

## Test version: v0.2.6 r8

Keyboard PTZ control tracks each held physical key independently and recovers
missed release events under Windows. A single background worker keeps only the
latest complete input state and rereads it after each network response. Rapid
changes cannot build a queue of obsolete movements.

Revision 8 restores targeted Stop before resuming held axes on a release or
reversal. The direct zero-velocity transitions tried in r7 reintroduced a held
direction on the reported camera. Stop remains the default for compatibility.
Pan and tilt share a stop group; the resulting pause remains, with Stop responses
of about 220–270 ms measured in the r6 trace. Devices verified to honor zero
velocity can opt into direct transitions with `CAMERA_PTZ_CONSERVATIVE_STOPS=0`.

Pan/tilt and zoom are sent together in ContinuousMove, using velocity spaces and
signed ranges advertised by the camera. Revision 8 prefers the declared native
movement timeout when it is within the advertised range. On the configured
camera this is 60 seconds, instead of the one-second timeout imposed by previous
revisions. An unchanged command is renewed after one third of its duration,
measured from transmission time. Changed input is sent immediately, independently
of this renewal schedule. Missing valid defaults fall back to a supported short
duration, or the implicit device default if capabilities cannot be read.

Complete release, loss of focus, shutdown and stale keyboard input still request
an explicit Stop immediately; they do not wait for the movement timeout. If the
network prevents Stop from reaching the camera, its last accepted movement may
continue until that timeout (60 seconds on the reported device).

The user confirms that r8 keeps zoom running during a diagonal hold and correctly
stops released directions. The transition pause remains. With r6/r7, zoom stopped after about one second
in either direction before its physical limit. TinyCam configured for ONVIF
Profile S can combine lateral movement and optical zoom on the same camera,
confirmed in Camera Viewer's image. Its exact requests are unknown. The native
timeout in r8 removes the one-second expiry implicated by the successful user test.
GetStatus did not provide usable feedback, so internal firmware behavior is not measured.

Closing Camera Viewer closes its players, PTZ controllers and Camera Manager
windows, including their children. PTZ requests its stops before exiting. Tk
remains responsive; an unresponsive owned child is terminated after 10 seconds.
Windows launched independently remain independent.

The local bounded `ptz_control_<process-id>.log` records requested axes, serialized
velocity fields, request duration and results, without camera addresses,
credentials, profile tokens or SOAP payloads. No diagnostic text or gamepad
support is added to the interface.

114 automated tests pass, including simulated cameras that ignore zero velocity,
latest-state handling during slow replies, native-timeout holds and prompt stops.
Real ONVIF/Zeep serialization is checked offline. Hardware results above apply to
the tested camera; other devices still need validation.
See [ONVIF PTZ](https://www.onvif.org/specs/srv/ptz/ONVIF-PTZ-Service-Spec-v250a.pdf).

Close old PTZ windows, launch `python camera_viewer.py` from the root and open a
controller marked `v0.2.6 r8`. Hold a diagonal with Shift/Ctrl for at least five
seconds, then release each key individually. Restart all Viewer/Manager windows
if you have not loaded the parent/child lifetime changes yet.

The root and `~v0.2.6/` contain identical source files, available in
[PR #3](https://github.com/THET1TAN/camera-management-system/pull/3), kept as a draft.
Main is unchanged. No executable has been rebuilt. Camera databases, local keys,
logs and generated executables are excluded from the source snapshot.
See [release notes](./note_de_version-v0.2.6.txt).

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
