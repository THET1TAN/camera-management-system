# 📹 Camera Management System

A comprehensive Python-based camera management system with PTZ (Pan-Tilt-Zoom) control, ONVIF support, and encrypted credential storage.

## ✨ Features

- **🔐 Secure Credential Management**: Encrypted storage of camera credentials using Fernet encryption
- **📺 Camera Viewer**: Browse and play camera streams with an intuitive GUI
- **🎮 PTZ Control**: Real-time Pan-Tilt-Zoom control with keyboard shortcuts
- **🔧 Camera Manager**: Add, edit, and delete camera configurations
- **📡 ONVIF Support**: Full compatibility with ONVIF-compliant cameras
- **💾 SQLite Database**: Lightweight local database for camera storage
- **🖱️ User-Friendly Interface**: Clean Tkinter-based GUI for all operations

## 🚀 Quick Start

### Prerequisites

- Python 3.9+ (recommended for optimal ONVIF compatibility)
- Windows OS (current implementation)

### Installation

1. **Clone the repository**
   ```bash
   git clone https://github.com/yourusername/camera-management-system.git
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
| `Ctrl/Shift` | Zoom In/Out |
| `Q/E` | Focus In/Out |
| `M/N` | Increase/Decrease Speed |
| `1-9` | Camera Presets |
| `ESC` | Exit |

## 🏗️ Architecture

### Core Components

- **`camera_viewer.py`**: Main application interface
- **`camera_manager.py`**: Camera configuration management
- **`ptz_keyboard_control.py`**: Real-time PTZ control interface
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
The application uses a predefined encryption key. For production use, consider implementing user-specific keys:

```python
# Current implementation (camera_manager.py)
ENCRYPTION_KEY = b'g4ZltE3Vv2Xzq5y6Lq3l4f8Ozt2Ck2Tk6v5b0rN2ghE='
```

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
- **Email**: joel.smith-gravel@hotmail.com
- **GitHub**: [@THET1TAN](https://github.com/THET1TAN)

---

⭐ **Star this repository if you find it helpful!**
