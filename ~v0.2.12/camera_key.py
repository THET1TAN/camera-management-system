"""A Fernet key stored only as a Windows user-scoped DPAPI blob."""
import base64
import binascii
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from windows_security import ProtectionError, private_directory, protect, unprotect


KEY_FILE = '.camera_encryption.dpapi'
ARCHIVE_KEY_FILE = '.archive_encryption.dpapi'
LEGACY_KEY_FILE = '.camera_encryption.key'
KEY_ENV = 'CAMERA_ENCRYPTION_KEY'
MAGIC = b'CAMERA-DPAPI-USER-1\x00'


def validate_key(value):
    value = value.strip()
    try:
        decoded = base64.b64decode(value, altchars=b'-_', validate=True)
    except (ValueError, binascii.Error):
        raise ProtectionError('Invalid camera encryption key.') from None
    if len(decoded) != 32:
        raise ProtectionError('Invalid camera encryption key.')
    return value


def save_encryption_key(directory, key, *, filename=KEY_FILE):
    if filename not in (KEY_FILE, ARCHIVE_KEY_FILE):
        raise ProtectionError('Unknown camera key purpose.')
    directory = private_directory(directory)
    key = validate_key(key)
    payload = MAGIC + protect(key)
    if validate_key(unprotect(payload[len(MAGIC):])) != key:
        raise ProtectionError('Camera key protection verification failed.')
    path = directory / filename
    if path.exists():
        if load_encryption_key(directory, create=False, filename=filename) != key:
            raise ProtectionError('Another camera key already exists.')
        return key
    temporary = None
    try:
        with NamedTemporaryFile(dir=directory, prefix='.key-', suffix='.pending', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            # Windows rename refuses to overwrite a concurrent installation key.
            temporary.rename(path)
        except FileExistsError:
            if load_encryption_key(directory, create=False, filename=filename) != key:
                raise ProtectionError('Another camera key already exists.') from None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return key


def load_encryption_key(directory, *, create=True, filename=KEY_FILE):
    if filename not in (KEY_FILE, ARCHIVE_KEY_FILE):
        raise ProtectionError('Unknown camera key purpose.')
    if KEY_ENV in os.environ:
        raise ProtectionError('Portable camera keys are disabled. Remove CAMERA_ENCRYPTION_KEY.')
    directory = Path(directory)
    path = directory / filename
    if path.exists():
        payload = path.read_bytes()
        if not payload.startswith(MAGIC):
            raise ProtectionError('Invalid Windows-protected camera key.')
        return validate_key(unprotect(payload[len(MAGIC):]))
    if not create or filename != KEY_FILE or (directory / 'camera_credentials.db').exists():
        raise ProtectionError('Camera key unavailable. Re-enter the camera credentials on this Windows profile.')
    if (directory / LEGACY_KEY_FILE).exists():
        raise ProtectionError('Legacy camera credentials require migration.')
    return save_encryption_key(directory, base64.urlsafe_b64encode(os.urandom(32)))
