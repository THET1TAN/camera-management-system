"""Strict encrypted credential storage and one-way migration from the source folder."""
from contextlib import closing, contextmanager
import os
from pathlib import Path
import sqlite3
import time

from cryptography.fernet import Fernet, InvalidToken

from camera_key import (KEY_FILE, KEY_ENV, LEGACY_KEY_FILE, ARCHIVE_KEY_FILE, load_encryption_key,
                        save_encryption_key, validate_key)
from camera_paths import SOURCE_ROOT, credentials_directory, data_directory
from windows_security import ProtectionError, private_directory, protect, unprotect


DATABASE = 'camera_credentials.db'


@contextmanager
def _lock(directory):
    import msvcrt
    with (directory / '.migration.lock').open('a+b') as handle:
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        deadline = time.monotonic() + 15
        while True:
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise ProtectionError('Camera data is busy. Close other camera applications and retry.') from None
                time.sleep(.05)
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def decrypt_field(cipher, value):
    try:
        token = value.encode('ascii') if isinstance(value, str) else value
        return cipher.decrypt(token).decode('utf-8')
    except (InvalidToken, ValueError, TypeError, UnicodeError):
        raise ProtectionError('Camera credentials are damaged or cannot be decrypted. No data was changed.') from None


def _validate(database, cipher):
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        columns = {r[1] for r in db.execute('PRAGMA table_info(cameras)')}
        ptz = 'ptz' if 'ptz' in columns else '0'
        rows = db.execute('SELECT id, ip, username, password, ' + ptz + ' FROM cameras ORDER BY id').fetchall()
        for row in rows:
            for value in row[1:4]:
                decrypt_field(cipher, value)
        return rows


def _copy_database(source, target):
    with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as src, \
            closing(sqlite3.connect(target)) as dst:
        src.backup(dst)


def _plain_rows(database, cipher):
    return [(row[0], *(decrypt_field(cipher, v) for v in row[1:4]), row[4])
            for row in _validate(database, cipher)]


def _initialize(database):
    with closing(sqlite3.connect(database)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS cameras (id INTEGER PRIMARY KEY, '
                   'ip BLOB NOT NULL, username BLOB NOT NULL, password BLOB NOT NULL, ptz INTEGER DEFAULT 0)')
        columns = {r[1] for r in db.execute('PRAGMA table_info(cameras)')}
        if 'ptz' not in columns:
            db.execute('ALTER TABLE cameras ADD COLUMN ptz INTEGER DEFAULT 0')


def _migrate_locked(directory, legacy):
    source = legacy / DATABASE
    legacy_key = legacy / LEGACY_KEY_FILE
    target = directory / DATABASE
    if not legacy_key.exists():
        if source.exists() and not target.exists():
            raise ProtectionError('Legacy camera key missing. Migration stopped without changing the database.')
        return
    if not source.exists():
        # Resume after database retirement, before the raw key was removed.
        if not target.exists():
            raise ProtectionError('Legacy camera database missing. Migration stopped.')
        old_key = validate_key(legacy_key.read_bytes())
        if load_encryption_key(directory, create=False, filename=ARCHIVE_KEY_FILE) != old_key:
            raise ProtectionError('Legacy key does not belong to this protected installation.')
        _validate(target, Fernet(load_encryption_key(directory, create=False)))
        if not unprotect((directory / 'legacy-database.dpapi').read_bytes()).startswith(b'SQLite format 3'):
            raise ProtectionError('Camera migration backup verification failed.')
        return
    key_bytes = legacy_key.read_bytes()
    key = validate_key(key_bytes)
    cipher = Fernet(key)
    original_rows = _validate(source, cipher)
    backup = directory / 'legacy-database.dpapi'
    if target.exists():
        # Resume a crash between publication and retirement of the old raw key.
        if (not backup.exists() or load_encryption_key(directory, create=False, filename=ARCHIVE_KEY_FILE) != key or
                not unprotect(backup.read_bytes()).startswith(b'SQLite format 3') or
                _plain_rows(target, Fernet(load_encryption_key(directory, create=False))) != _plain_rows(source, cipher)):
            raise ProtectionError('Both legacy and protected camera stores exist. Migration requires review.')
        return
    temporary = directory / 'migration.pending.db'
    if temporary.exists():
        temporary.unlink()
    try:
        _copy_database(source, temporary)
        _initialize(temporary)
        if _validate(temporary, cipher) != original_rows:
            raise ProtectionError('Camera migration verification failed.')
        # A recovery copy is protected by this profile; no raw key is backed up.
        contents = temporary.read_bytes()
        protected = protect(contents)
        if unprotect(protected) != contents:
            raise ProtectionError('Camera migration backup verification failed.')
        backup_pending = directory / 'legacy-backup.pending.dpapi'
        with backup_pending.open('wb') as handle:
            handle.write(protected)
            handle.flush()
            os.fsync(handle.fileno())
        backup_pending.replace(backup)
        save_encryption_key(directory, key, filename=ARCHIVE_KEY_FILE)
        # Historical source snapshots contained the old key: the active
        # credential database MUST use a new key, not merely wrap the old one.
        new_key = load_encryption_key(directory)
        new_cipher = Fernet(new_key)
        with closing(sqlite3.connect(temporary)) as db, db:
            for row in original_rows:
                sealed = tuple(new_cipher.encrypt(decrypt_field(cipher, value).encode('utf-8')) for value in row[1:4])
                db.execute('UPDATE cameras SET ip=?, username=?, password=? WHERE id=?', (*sealed, row[0]))
        if _plain_rows(temporary, new_cipher) != _plain_rows(source, cipher):
            raise ProtectionError('Camera re-encryption verification failed.')
        if legacy_key.read_bytes() != key_bytes or _validate(source, cipher) != original_rows:
            raise ProtectionError('Legacy camera data changed during migration. Close all camera windows and retry.')
        temporary.replace(target)
        _validate(target, Fernet(load_encryption_key(directory, create=False)))
    finally:
        if temporary.exists():
            temporary.unlink()


def _migrate(directory, legacy):
    source = legacy / DATABASE
    if not source.exists() or not (legacy / LEGACY_KEY_FILE).exists():
        _migrate_locked(directory, legacy)
    else:
        # Block writers while the verified snapshot is copied and re-encrypted.
        with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=rw', uri=True, timeout=3)) as lock:
            lock.execute('BEGIN IMMEDIATE')
            try:
                _migrate_locked(directory, legacy)
            finally:
                lock.rollback()
    legacy_key = legacy / LEGACY_KEY_FILE
    if legacy_key.exists():
        # Windows refuses database deletion while an older process still owns
        # a SQLite handle. Leave the raw key intact on any such failure.
        if source.exists():
            old_cipher = Fernet(validate_key(legacy_key.read_bytes()))
            new_cipher = Fernet(load_encryption_key(directory, create=False))
            if _plain_rows(source, old_cipher) != _plain_rows(directory / DATABASE, new_cipher):
                raise ProtectionError('Legacy camera data changed before retirement. Close all old camera applications.')
            source.unlink()
        legacy_key.unlink()


def prepare_store(directory=None, legacy_root=SOURCE_ROOT):
    if KEY_ENV in os.environ:
        raise ProtectionError('Portable camera keys are disabled. Remove CAMERA_ENCRYPTION_KEY.')
    directory = private_directory(directory or credentials_directory())
    with _lock(directory):
        if legacy_root is not None and Path(legacy_root).resolve() != directory.resolve():
            _migrate(directory, Path(legacy_root))
        key = load_encryption_key(directory)
        _initialize(directory / DATABASE)
        _validate(directory / DATABASE, Fernet(key))
    return directory


def load_camera(camera_id, directory=None):
    """Read one camera locally; process commands carry only this database ID."""
    directory = Path(directory or credentials_directory())
    cipher = Fernet(load_encryption_key(directory, create=False))
    try:
        with closing(sqlite3.connect((directory / DATABASE).resolve().as_uri() + '?mode=ro', uri=True)) as db:
            row = db.execute('SELECT ip, username, password FROM cameras WHERE id=?', (camera_id,)).fetchone()
    except sqlite3.Error:
        raise ProtectionError('Camera credentials unavailable for this Windows profile.') from None
    if row is None:
        raise ProtectionError('Camera not found in this Windows profile.')
    return tuple(decrypt_field(cipher, value) for value in row)


def migrate_local_settings(legacy_root=SOURCE_ROOT):
    """Preserve existing settings/layout; never replace a current profile's files."""
    destination = private_directory(data_directory())
    for name in ('playback.json', 'camera_health.json', 'camera_window_positions.db'):
        source, target = Path(legacy_root) / name, destination / name
        if not source.exists() or target.exists():
            continue
        temporary = destination / (name + '.migration.tmp')
        try:
            if temporary.exists():
                temporary.unlink()
            if name.endswith('.db'):
                _copy_database(source, temporary)
            else:
                with temporary.open('xb') as handle:
                    handle.write(source.read_bytes())
            temporary.replace(target)
        finally:
            if temporary.exists():
                temporary.unlink()


def application_store():
    directory = prepare_store()
    with _lock(directory):
        migrate_local_settings()
    return directory
