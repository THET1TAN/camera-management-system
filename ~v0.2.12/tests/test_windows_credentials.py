"""Migration failure safety, credential integrity and launch privacy; no cameras."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import io
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet, InvalidToken
from camera_credentials import prepare_store, load_camera, decrypt_field
from camera_key import ARCHIVE_KEY_FILE, KEY_ENV, KEY_FILE, LEGACY_KEY_FILE, load_encryption_key
from windows_security import ProtectionError, unprotect


@unittest.skipUnless(os.name == 'nt', 'Windows credential protection')
class CredentialMigrationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.legacy = self.root / 'legacy'
        self.legacy.mkdir()
        self.destination = self.root / 'credentials'
        self.key = Fernet.generate_key()
        self.cipher = Fernet(self.key)
        self.values = ('192.0.2.10', 'synthetic-user', 'synthetic-password')
        self.db = self.legacy / 'camera_credentials.db'
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('CREATE TABLE cameras (id INTEGER PRIMARY KEY, ip BLOB, username BLOB, password BLOB, ptz INTEGER)')
            db.execute('INSERT INTO cameras VALUES (42,?,?,?,1)',
                       tuple(self.cipher.encrypt(v.encode()) for v in self.values))
        (self.legacy / LEGACY_KEY_FILE).write_bytes(self.key + b'\n')
        self.environment = patch.dict(os.environ)
        self.environment.start()
        os.environ.pop(KEY_ENV, None)

    def tearDown(self):
        self.environment.stop()
        self.folder.cleanup()

    def migrate(self):
        return prepare_store(self.destination, self.legacy)

    def test_migration_preserves_credentials_and_retires_only_verified_raw_key(self):
        before = self.db.read_bytes()
        self.migrate()
        self.assertEqual(load_camera(42, self.destination), self.values)
        self.assertFalse(self.db.exists())
        self.assertFalse((self.legacy / LEGACY_KEY_FILE).exists())
        self.assertNotIn(self.key, (self.destination / KEY_FILE).read_bytes())
        self.assertTrue(unprotect((self.destination / 'legacy-database.dpapi').read_bytes()).startswith(b'SQLite'))
        self.assertEqual(self.migrate(), self.destination)

    def test_historical_source_key_cannot_decrypt_the_new_database(self):
        self.migrate()
        with closing(sqlite3.connect(self.destination / 'camera_credentials.db')) as db:
            token = db.execute('SELECT password FROM cameras WHERE id=42').fetchone()[0]
        with self.assertRaises(InvalidToken):
            self.cipher.decrypt(token)
        self.assertNotEqual(load_encryption_key(self.destination, create=False), self.key)

    def test_cached_archives_remain_readable_but_new_data_uses_the_new_key(self):
        self.migrate()
        from playback.config import load_cameras, Settings
        cameras, cache_cipher = load_cameras(Settings(), self.root)
        self.assertEqual(cameras[0].password, self.values[2])
        old_token = self.cipher.encrypt(b'existing encrypted archive metadata')
        self.assertEqual(cache_cipher.decrypt(old_token), b'existing encrypted archive metadata')
        with self.assertRaises(InvalidToken):
            self.cipher.decrypt(cache_cipher.encrypt(b'new archive metadata'))

    def test_damaged_ciphertext_stops_migration_without_reencrypting_it(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('UPDATE cameras SET password=?', (b'gAAAA-broken',))
        before = self.db.read_bytes()
        with self.assertRaises(ProtectionError):
            self.migrate()
        self.assertEqual(self.db.read_bytes(), before)
        self.assertTrue((self.legacy / LEGACY_KEY_FILE).exists())
        self.assertFalse((self.destination / 'camera_credentials.db').exists())

    def test_plaintext_is_not_silently_treated_as_a_valid_credential(self):
        with self.assertRaises(ProtectionError):
            decrypt_field(self.cipher, 'not-encrypted')

    def test_wrong_legacy_key_preserves_both_original_files(self):
        (self.legacy / LEGACY_KEY_FILE).write_bytes(Fernet.generate_key())
        before = self.db.read_bytes()
        with self.assertRaises(ProtectionError):
            self.migrate()
        self.assertEqual(before, self.db.read_bytes())
        self.assertTrue((self.legacy / LEGACY_KEY_FILE).exists())

    def test_failed_dpapi_leaves_original_database_and_key_usable(self):
        with patch('camera_credentials.protect', side_effect=ProtectionError('Test failure')):
            with self.assertRaises(ProtectionError):
                self.migrate()
        self.assertEqual((self.legacy / LEGACY_KEY_FILE).read_bytes().strip(), self.key)
        self.assertFalse((self.destination / 'camera_credentials.db').exists())
        self.migrate()
        self.assertEqual(load_camera(42, self.destination), self.values)

    def test_crash_after_publication_can_finish_retiring_raw_key(self):
        original = Path.unlink
        def fail_raw_key(path, *args, **kwargs):
            if path == self.legacy / LEGACY_KEY_FILE:
                raise PermissionError('simulated interruption')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'unlink', fail_raw_key):
            with self.assertRaises(PermissionError):
                self.migrate()
        self.assertEqual(load_camera(42, self.destination), self.values)
        self.migrate()
        self.assertFalse((self.legacy / LEGACY_KEY_FILE).exists())

    def test_existing_unrelated_store_is_never_replaced(self):
        prepare_store(self.destination, legacy_root=None)
        before = (self.destination / KEY_FILE).read_bytes()
        with self.assertRaises(ProtectionError):
            self.migrate()
        self.assertEqual((self.destination / KEY_FILE).read_bytes(), before)
        self.assertTrue((self.legacy / LEGACY_KEY_FILE).exists())

    def test_missing_key_does_not_turn_existing_store_into_an_empty_installation(self):
        self.migrate()
        (self.destination / KEY_FILE).unlink()
        before = (self.destination / 'camera_credentials.db').read_bytes()
        with self.assertRaises(ProtectionError):
            self.migrate()
        self.assertEqual((self.destination / 'camera_credentials.db').read_bytes(), before)

    def test_database_id_is_bound_as_data(self):
        self.migrate()
        with self.assertRaises(ProtectionError):
            load_camera('42 OR 1=1', self.destination)

    def test_reader_never_creates_a_missing_store(self):
        with self.assertRaises(ProtectionError):
            load_camera(42, self.destination)
        self.assertFalse(self.destination.exists())

    def test_simultaneous_startups_migrate_once_without_changing_the_key(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.migrate(), range(2)))
        self.assertEqual(results, [self.destination, self.destination])
        self.assertEqual(load_camera(42, self.destination), self.values)
        self.assertNotEqual(load_encryption_key(self.destination, create=False), self.key)
        self.assertEqual(load_encryption_key(self.destination, create=False, filename=ARCHIVE_KEY_FILE), self.key)

    def test_interruption_before_database_publication_is_retryable(self):
        original = Path.replace
        def fail_database(path, target):
            if path.name == 'migration.pending.db':
                raise OSError('simulated publication failure')
            return original(path, target)
        with patch.object(Path, 'replace', fail_database):
            with self.assertRaises(OSError):
                self.migrate()
        self.assertTrue((self.legacy / LEGACY_KEY_FILE).exists())
        self.migrate()
        self.assertEqual(load_camera(42, self.destination), self.values)


class LaunchPrivacyTests(unittest.TestCase):
    def test_legacy_player_arguments_are_rejected_without_echoing_secrets(self):
        result = subprocess.run([sys.executable, 'player_vilkin_hikvision.py',
                                 '42', '192.0.2.10', 'secret-user', 'secret-password'],
                                capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('secret-user', result.stdout + result.stderr)
        self.assertNotIn('secret-password', result.stdout + result.stderr)

    def test_ptz_legacy_arguments_are_rejected(self):
        import ptz_keyboard_control as ptz
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(ptz.main(['42', 'private-host', 'private-user', 'private-password']), 1)
        self.assertNotIn('private-password', output.getvalue())

    def test_manager_launch_commands_contain_only_database_id(self):
        import camera_manager as manager
        app = manager.CameraApp.__new__(manager.CameraApp)
        app.children = Mock()
        camera = (42, 'private-host', 'private-user', b'private-password', 1)
        app.play_camera_thread(camera)
        app.play_ptz_thread(camera)
        for call in app.children.spawn.call_args_list:
            self.assertEqual(call.args[0][2:], ['--camera-id', '42'])
            self.assertNotIn('private-', str(call))


if __name__ == '__main__':
    unittest.main()
