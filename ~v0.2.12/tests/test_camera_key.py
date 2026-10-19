"""Real Windows DPAPI round trips, strict key loading and no portable fallback."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from camera_key import (KEY_ENV, KEY_FILE, LEGACY_KEY_FILE, load_encryption_key,
                        save_encryption_key)
from windows_security import ProtectionError, protect, unprotect


@unittest.skipUnless(os.name == 'nt', 'Windows credential protection')
class CameraKeyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name) / 'credentials'
        self.directory.mkdir()
        self.environment = patch.dict(os.environ)
        self.environment.start()
        os.environ.pop(KEY_ENV, None)
        self.key = Fernet.generate_key()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def test_key_on_disk_is_protected_and_reusable(self):
        save_encryption_key(self.directory, self.key)
        payload = (self.directory / KEY_FILE).read_bytes()
        self.assertNotIn(self.key, payload)
        self.assertEqual(load_encryption_key(self.directory), self.key)
        self.assertFalse((self.directory / LEGACY_KEY_FILE).exists())

    def test_key_is_not_replaced_when_database_exists(self):
        database = self.directory / 'camera_credentials.db'
        database.write_bytes(b'existing-database')
        with self.assertRaises(ProtectionError):
            load_encryption_key(self.directory)
        self.assertFalse((self.directory / KEY_FILE).exists())
        self.assertEqual(database.read_bytes(), b'existing-database')

    def test_new_installations_get_distinct_keys(self):
        first = load_encryption_key(self.directory)
        self.assertEqual(first, load_encryption_key(self.directory))
        self.assertNotEqual(first, load_encryption_key(self.directory / 'other'))

    def test_environment_key_is_rejected_even_with_valid_protected_key(self):
        save_encryption_key(self.directory, self.key)
        os.environ[KEY_ENV] = self.key.decode()
        with self.assertRaises(ProtectionError) as error:
            load_encryption_key(self.directory)
        self.assertNotIn(self.key.decode(), str(error.exception))

    def test_raw_key_requires_verified_database_migration(self):
        (self.directory / LEGACY_KEY_FILE).write_bytes(self.key)
        with self.assertRaises(ProtectionError):
            load_encryption_key(self.directory)
        self.assertFalse((self.directory / KEY_FILE).exists())

    def test_no_parent_key_lookup_in_old_snapshots(self):
        save_encryption_key(self.directory, self.key)
        snapshot = self.directory / '~v0.2.11'
        snapshot.mkdir()
        with self.assertRaises(ProtectionError):
            load_encryption_key(snapshot, create=False)

    def test_different_profile_failure_never_regenerates_key(self):
        save_encryption_key(self.directory, self.key)
        before = (self.directory / KEY_FILE).read_bytes()
        with patch('camera_key.unprotect', side_effect=ProtectionError('Other profile')):
            with self.assertRaises(ProtectionError):
                load_encryption_key(self.directory)
        self.assertEqual((self.directory / KEY_FILE).read_bytes(), before)

    def test_tampered_blob_is_rejected(self):
        save_encryption_key(self.directory, self.key)
        path = self.directory / KEY_FILE
        payload = bytearray(path.read_bytes())
        payload[-1] ^= 1
        path.write_bytes(payload)
        with self.assertRaises(ProtectionError):
            load_encryption_key(self.directory)

    def test_actual_dpapi_authenticates_data_and_round_trips(self):
        protected = protect(b'synthetic credential')
        self.assertNotIn(b'synthetic credential', protected)
        self.assertEqual(unprotect(protected), b'synthetic credential')
        with self.assertRaises(ProtectionError):
            unprotect(protected[:-10])

    def test_conflicting_key_is_never_overwritten(self):
        save_encryption_key(self.directory, self.key)
        with self.assertRaises(ProtectionError):
            save_encryption_key(self.directory, Fernet.generate_key())
        self.assertEqual(load_encryption_key(self.directory), self.key)


if __name__ == '__main__':
    unittest.main()
