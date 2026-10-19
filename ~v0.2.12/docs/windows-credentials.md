# Windows user-bound camera credentials

This protection is included in the main v0.2.12 sources and the matching
`~v0.2.12` snapshot. Earlier `~v*` snapshots remain unchanged; use the current
main sources after migration.

## Daily use and storage

Windows unlocks the stored key for the signed-in user without an extra application
password. A fresh installation generates a random Fernet key. DPAPI stores that
key as an authenticated user-scoped blob, never as a raw key in the application
directory. The code never uses `CRYPTPROTECT_LOCAL_MACHINE`, which would grant
other local users decryption access.

Active data is under `%LOCALAPPDATA%\CameraManagementSystem`:

| Location | Contents |
| --- | --- |
| `credentials/camera_credentials.db` | Fernet-encrypted camera address, username and password |
| `credentials/.camera_encryption.dpapi` | User-protected encryption key |
| `credentials/.archive_encryption.dpapi` | Separately user-protected legacy key, only for existing cache metadata |
| `credentials/legacy-database.dpapi` | User-protected migration recovery copy, when migrated |
| `camera_window_positions.db` | Window geometry, session membership and mute preferences |
| `camera_health.json`, `playback.json` | Existing settings, copied once when present |
| `player-logs`, `ptz-logs` | New local diagnostics |
| `playback` | Existing media cache; old metadata stays readable, new metadata uses the new key |

The profile and credential directories have protected inheritable permissions for
the current Windows user and SYSTEM. `CAMERA_DATA_DIRECTORY` is a path override
for isolated tests; it does not bypass DPAPI. `CAMERA_ENCRYPTION_KEY` is rejected.
`CAMERA_USERNAME` and `CAMERA_PASSWORD` are no longer player inputs and are removed
from child launch environments.

## First updated startup

Close every older Viewer, Manager, player and PTZ window first. Start the updated
main `Lancer-Camera.cmd` as the same Windows user who normally runs it.

1. Acquire an installation migration lock and reserve the legacy SQLite database
   against writes. Validate every encrypted field using the existing raw key.
2. Back up the database to a private staging file; verify every encrypted row,
   camera ID and PTZ flag. No failed decryption is accepted as plaintext.
3. Create and round-trip a DPAPI-protected recovery copy. Generate a new random
   credential key, protect it with DPAPI and re-encrypt every credential. Historical
   sources contain the old key, so simply wrapping it would be insufficient. Keep
   a separately protected old key solely for existing archive cache metadata.
4. Publish and reopen the protected store, compare decrypted data with the original,
   then retire the database and raw key from the source directory. The protected
   recovery copy remains available only under this Windows profile.
5. Copy existing settings/layout only when no current-profile version exists.
   Future writes use the local profile.

An interruption before publication leaves the legacy files usable. An interruption
after publication but before raw-key retirement can be resumed after verifying
the published database, protected backup and both protected keys. Conflicting independent
stores are never merged or overwritten automatically. Readers never create keys.

Updates preserve the protected key. Deleting or losing it does not generate a
replacement over an existing database. To use a new Windows profile, start with a
fresh application installation and enter the camera credentials again. Do not copy
an unavailable store over the new profile's directory.

## Process privacy

Viewer and Manager launch video/PTZ windows with `--camera-id ID` only. Those
processes read and decrypt the one requested camera locally. Legacy commands
containing an address, username and password are rejected without echoing values.
The supervised native player continues using its private input pipe for session
configuration. The loopback-only `--test-uri` fixture mode remains available with
`--camera-id` and rejects embedded user credentials.

## Limits

Simple copying of the database and protected-key file does not grant access on a
different profile or PC. DPAPI is not a hardware non-exportability guarantee:
enterprise roaming/recovery arrangements are exceptions. Code controlling the
authorized Windows account or application can still retrieve usable secrets.
The camera's own passwords and network transport are not changed.

Migration cannot revoke raw keys already present in old backups, source archives
or OneDrive version history. Remove those separately where appropriate. If a key
or password was exposed, change the camera passwords; protecting the current key
does not invalidate previously copied credentials.

References: [Microsoft DPAPI](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata),
[Fernet](https://cryptography.io/en/stable/fernet/).

## Offline verification

Use an isolated `CAMERA_DATA_DIRECTORY` and `CAMERA_WINDOW_POSITIONS_FILE`, then run
`python -m unittest discover -s tests -v` on Windows with the application interpreter.
The implementation passed 465 tests on Windows / Python 3.9.13 in 148.582 seconds.
The suite uses generated credentials and temporary databases. DPAPI/ACL tests must
run as a normal Windows user process, because an AppContainer sandbox does not have
the permissions of that user's ordinary desktop applications. These checks cover
real DPAPI round trips, malformed blobs, migration interruption, conflicts, strict
decryption and private launch commands. They do not connect to physical cameras.
