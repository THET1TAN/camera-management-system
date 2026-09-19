"""Local configuration, read-only credential reuse, no import-time I/O."""
from dataclasses import asdict, dataclass, field
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .model import Camera, PlaybackError

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    display_zone: str = 'America/Toronto'
    cache_gib: float = 8.
    free_gib: float = 2.
    ttl_days: int = 14
    reserve_seconds: int = 120
    max_archive_gib: float = 2.
    metadata_ttl: int = 900
    max_index_entries: int = 20000
    ffmpeg: str = 'ffmpeg'
    ffprobe: str = 'ffprobe'
    cameras: dict = field(default_factory=dict)
    # Use the user's local application data: video cache must not sync to OneDrive.
    cache_directory: str = ''

    @property
    def cache_path(self):
        return Path(self.cache_directory).expanduser().resolve() if self.cache_directory else (
            Path(os.getenv('LOCALAPPDATA', str(Path.home()/'.cache'))) / 'CameraManagementSystem' / 'playback')


def load_settings(root=ROOT):
    path = Path(root) / 'playback.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        settings = Settings(**data)
        ZoneInfo(settings.display_zone)
        if not (1 <= settings.cache_gib <= 1024 and .25 <= settings.free_gib <= 100 and
                .05 <= settings.max_archive_gib <= settings.cache_gib and
                1 <= settings.ttl_days <= 365 and 10 <= settings.reserve_seconds <= 600 and
                30 <= settings.metadata_ttl <= 86400 and 1000 <= settings.max_index_entries <= 100000):
            raise ValueError
        for key, values in settings.cameras.items():
            if not str(key).isdigit() or set(values) - {'backend', 'zone', 'endpoint', 'track', 'revision', 'time_shift'}:
                raise ValueError
            if values.get('backend', 'auto') not in ('auto', 'isapi', 'videolink'):
                raise ValueError
            if values.get('zone'):
                ZoneInfo(values['zone'])
            if abs(int(values.get('time_shift', 0))) > 86400:
                raise ValueError
        return settings
    except ZoneInfoNotFoundError:
        raise PlaybackError('timezone-unavailable') from None
    except (TypeError, ValueError, OSError):
        raise PlaybackError('configuration-invalid') from None


def save_settings(settings, root=ROOT):
    path = Path(root) / 'playback.json'
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(asdict(settings), indent=2, ensure_ascii=False)+'\n', encoding='utf-8')
    temporary.replace(path)


def load_cameras(settings, root=ROOT):
    from cryptography.fernet import Fernet
    from camera_key import load_encryption_key
    cipher = Fernet(load_encryption_key(root))
    def decode(value):
        value = value.encode() if isinstance(value, str) else value
        return cipher.decrypt(value).decode('utf-8')
    path = Path(root) / 'camera_credentials.db'
    if not path.exists():
        return (), cipher
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            rows = db.execute('SELECT id, ip, username, password FROM cameras').fetchall()
        cameras = tuple(Camera(int(row[0]), *(decode(v) for v in row[1:]),
                               **settings.cameras.get(str(row[0]), {})) for row in rows)
        return cameras, cipher
    except Exception:
        raise PlaybackError('credentials-unavailable') from None


def base_url(camera):
    value = camera.endpoint or camera.host
    if '://' not in value:
        # Bracket a bare IPv6 address. A hostname:port remains a normal authority.
        value = 'http://' + (f'[{value}]' if value.count(':') > 1 and not value.startswith('[') else value)
    try:
        parts = urlsplit(value)
        if (parts.scheme not in ('http', 'https') or not parts.hostname or
                parts.username is not None or parts.password is not None or
                parts.path not in ('', '/') or parts.query or parts.fragment or
                any(c.isspace() for c in parts.netloc) or not 1 <= (parts.port or 80) <= 65535):
            raise ValueError
        return value.rstrip('/')
    except ValueError:
        raise PlaybackError('endpoint-invalid') from None
