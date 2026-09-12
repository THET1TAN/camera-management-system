"""Load the installation's encryption key without embedding it in source code."""
import base64
import binascii
import os
from pathlib import Path
import re


KEY_FILE = '.camera_encryption.key'
KEY_ENV = 'CAMERA_ENCRYPTION_KEY'


def validate_key(value):
    value = value.strip()
    try:
        decoded = base64.b64decode(value, altchars=b'-_', validate=True)
    except (ValueError, binascii.Error):
        raise ValueError('Invalid camera encryption key format') from None
    if len(decoded) != 32:
        raise ValueError('Invalid camera encryption key length')
    return value


def load_encryption_key(directory):
    directory = Path(directory)
    env_key = os.environ.get(KEY_ENV)
    if env_key is not None:
        try:
            return validate_key(env_key.encode('ascii'))
        except UnicodeEncodeError:
            raise ValueError('Invalid camera encryption key format') from None
    path = directory / KEY_FILE
    if path.is_file():
        return validate_key(path.read_bytes())
    # Source snapshots beside the active application share its installation key.
    if re.fullmatch(r'~v\d+(?:\.\d+){1,3}', directory.name, re.IGNORECASE):
        shared = directory.parent / KEY_FILE
        if shared.is_file():
            return validate_key(shared.read_bytes())
    if (directory / 'camera_credentials.db').exists():
        raise RuntimeError(
            'Camera encryption key missing. Restore .camera_encryption.key beside '
            'camera_credentials.db, or set CAMERA_ENCRYPTION_KEY to the existing key. '
            'The database has not been changed.')
    key = base64.urlsafe_b64encode(os.urandom(32))
    try:
        # Exclusive creation prevents overwriting an existing installation key.
        with path.open('xb') as handle:
            handle.write(key + b'\n')
    except FileExistsError:
        return validate_key(path.read_bytes())
    return key
