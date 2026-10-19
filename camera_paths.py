"""Installed data lives in the local Windows profile, independently of source paths."""
import os
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parent


def data_directory():
    # Path override supports isolated tests; it never supplies an encryption key.
    override = os.environ.get('CAMERA_DATA_DIRECTORY')
    if override:
        return Path(override).resolve()
    local = os.environ.get('LOCALAPPDATA')
    if not local:
        raise RuntimeError('Windows local application data directory unavailable.')
    return Path(local) / 'CameraManagementSystem'


def credentials_directory():
    return data_directory() / 'credentials'
