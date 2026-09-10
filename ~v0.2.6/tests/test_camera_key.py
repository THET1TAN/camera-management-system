"""Installation key compatibility; test keys are generated and never published."""
import base64
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from camera_key import KEY_ENV, KEY_FILE, load_encryption_key


class CameraKeyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.environment = patch.dict(os.environ)
        self.environment.start()
        os.environ.pop(KEY_ENV, None)
        self.key = base64.urlsafe_b64encode(os.urandom(32))

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def test_existing_key_is_retained_with_existing_database(self):
        (self.directory / KEY_FILE).write_bytes(self.key + b'\n')
        database = self.directory / 'camera_credentials.db'
        database.write_bytes(b'existing-database-content')
        self.assertEqual(load_encryption_key(self.directory), self.key)
        self.assertEqual(database.read_bytes(), b'existing-database-content')

    def test_missing_key_does_not_generate_a_replacement_for_existing_database(self):
        database = self.directory / 'camera_credentials.db'
        database.write_bytes(b'preserve-this-database')
        with self.assertRaisesRegex(RuntimeError, 'Restore .camera_encryption.key'):
            load_encryption_key(self.directory)
        self.assertFalse((self.directory / KEY_FILE).exists())
        self.assertEqual(database.read_bytes(), b'preserve-this-database')

    def test_fresh_installation_generates_and_reuses_its_key(self):
        key = load_encryption_key(self.directory)
        self.assertEqual(len(base64.urlsafe_b64decode(key)), 32)
        self.assertEqual(load_encryption_key(self.directory), key)
        other = self.directory / 'other-installation'
        other.mkdir()
        self.assertNotEqual(load_encryption_key(other), key)

    def test_environment_key_works_without_creating_a_key_file(self):
        os.environ[KEY_ENV] = self.key.decode('ascii')
        self.assertEqual(load_encryption_key(self.directory), self.key)
        self.assertFalse((self.directory / KEY_FILE).exists())

    def test_source_snapshot_can_read_parent_installation_key(self):
        (self.directory / KEY_FILE).write_bytes(self.key)
        snapshot = self.directory / '~v0.2.6'
        snapshot.mkdir()
        self.assertEqual(load_encryption_key(snapshot), self.key)
        self.assertFalse((snapshot / KEY_FILE).exists())

    def test_other_directories_do_not_reuse_parent_keys(self):
        (self.directory / KEY_FILE).write_bytes(self.key)
        nested = self.directory / '~vacation'
        nested.mkdir()
        self.assertNotEqual(load_encryption_key(nested), self.key)

    def test_invalid_key_errors_do_not_include_its_contents(self):
        for invalid in (b'not-a-valid-private-key', base64.urlsafe_b64encode(b'too-short')):
            with self.subTest(invalid_length=len(invalid)):
                (self.directory / KEY_FILE).write_bytes(invalid)
                with self.assertRaises(ValueError) as error:
                    load_encryption_key(self.directory)
                self.assertNotIn(invalid.decode(), str(error.exception))

    def test_existing_snapshot_key_takes_precedence_over_parent_key(self):
        (self.directory / KEY_FILE).write_bytes(self.key)
        snapshot = self.directory / '~v0.2.6'
        snapshot.mkdir()
        own = base64.urlsafe_b64encode(os.urandom(32))
        (snapshot / KEY_FILE).write_bytes(own)
        self.assertEqual(load_encryption_key(snapshot), own)


if __name__ == '__main__':
    unittest.main()
