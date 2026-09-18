# Direct player recovery — issue #9

Development change based on `main` **8c8a9d7**, after v0.2.8 / PR #11.
This is not a new approved release. Merge requires Joël's explicit validation.
The v0.2.6, v0.2.7 and v0.2.8 snapshots remain untouched.

## What changes

An interrupted RTSP session no longer runs native VLC recovery on Tk's event
thread. The window stays available, covers the previous image with an unavailable
status, and retries automatically. Mute, volume and the existing window survive
replacement of the media session. There are no repeated modal dialogs.

The audio control retains v0.2.8's speaker and crossed-speaker icons, 30 px
button with a 16 px symbol, pressed appearance while muted, and `(Muted)` title
suffix. One click mutes, another restores sound; mute stays selected across
reconnection. The temporary text button and volume slider have been removed.
Tk reads the existing PNG assets directly. The click only changes the supervisor's
requested audio state; all native audio calls still run in the media process.

The historical filename `player_vilkin_hikvision.py` remains a compatible launch
entry point; its implementation is generic ONVIF/RTSP. It keeps the first ONVIF
media profile and the **complete discovered URI**, including a nonstandard host,
port, path and query. It percent-encodes credentials when adding missing userinfo;
it never substitutes `/Streaming/Channels/101`. IPv6 authorities are retained.
The change does not set a camera codec, alter camera settings or assume a brand.
The existing direct launch still discovers ONVIF on port 80. Media1 ONVIF and the
installed VLC's codecs/RTSP support remain compatibility limits; not every ONVIF
variant or proprietary camera extension has been tested.

The PTZ r9 B command engine and camera availability monitor are unchanged.
Availability remains a service-reachability indication and does not drive video
recovery. No database, encryption key or camera configuration is read by the test
bench or included in this change.

## Ownership and shutdown

* **Tk process / Tk thread:** widgets, a 100 ms timer, one HWND lookup after widget
  layout, immutable status snapshots and nonblocking close/audio requests. No
  `libvlc`, ONVIF, `sleep`, pipe I/O, native getters or joins run here. The video
  HWND remains mapped and sized; a sibling overlay hides stale video. The required
  Windows `WS_CLIPCHILDREN` style is set before starting playback.
* **One supervisor thread per window:** starts, supervises and reaps one owned
  media process at a time. Each generation receives new pipes and a bounded
  128-message status queue. A replacement starts only after the previous process
  has exited. Old generation messages are discarded.
* **One native owner in the media process:** performs discovery, create, attach,
  set-media, play, audio, stats and teardown in order. Event callbacks copy only a
  fixed event name and generation into a bounded nonblocking queue. The native
  log callback recognizes selected graphics-error format strings without
  formatting or retaining VLC's variable arguments. Callbacks never use Tk,
  stop/release, file logging, network I/O or waits.

This process boundary is intentional: timing out a Python thread cannot cancel
`libvlc_media_player_stop()` or another stuck native call. A timed-out operation
retires only the corresponding owned process. A normal teardown detaches events,
stops playback, clears HWND, releases player/media, detaches logging, then releases
the instance. All these operations have entry/exit traces. The supervisor allows
one second for cleanup before killing and reaping the process. Native resources
remaining after a forced exit belong to that process and are reclaimed by Windows.
Failure to confirm process termination stops replacement instead of creating
more sessions.

States are `STARTING`, `PLAYING`, `RECONNECTING`, `RECONNECT_WAIT`, `STOPPING`,
`CLOSED` (or `FAILED` for failed process cleanup). Closing has priority over retries.
Tk withdraws immediately and keeps servicing events until its media process is
reaped, then destroys the HWND. The Viewer's original stdin lifetime contract and
10-second child shutdown grace, including PTZ Stop, remain in place. The media
worker uses a **separate private stdin pipe** with the parent marker removed.
EOF also starts an independent two-second worker watchdog, so a crashed player
window cannot leave a native owner blocked forever. Raw stdin reads avoid a
buffered-reader lock at Python interpreter finalization.

## Detection and retries

Native operations have an 8-second watchdog; ONVIF discovery has 15 seconds overall
and uses bounded HTTP requests with 2-second network timeouts. A new default
session gets 30 seconds to establish video progress. The backoff is 1 / 2 / 4 / 8
seconds, capped at 8 and cancellable on close. An initial 15-second cap exceeded
the proposed 20-second recovery target in 8/20 trials because it added to VLC's
own RTSP timeout; the cap was calibrated down after those measurements.
It resets only after 30 seconds of video progress,
not after `play()` or a `Playing` event.

The owner samples input bytes, decoded video, displayed pictures, audio buffers,
dimensions and bitrate every 500 ms. Success requires new decoded **and** displayed
pictures, allowing the two counters to advance in different samples. Repeatedly
presenting an old picture is insufficient. Lack of video progress for 15 seconds triggers
replacement; diagnostics distinguish absent input, decode and display progress.
An `ESDeleted` event alone is not a failure (codec negotiation can legitimately
delete streams). Errors, end-of-stream and recognized graphics-loss messages can
request immediate retirement. Event bursts therefore cannot start concurrent
recoveries. A stalled source without events is covered by the progress deadline.

The counters are evidence of native pipeline activity, **not proof that a physical
screen or loudspeaker reproduced the content correctly**. The integration bench
also compares actual displayed synthetic images; sound validation below distinguishes
buffer output from listening. Nothing can provide a current image while the source
supplies no decodable data. Before the stall timeout elapses, loss may still be
undetectable and the previous image may remain visible.

For unusually low-cadence streams, allow more than the expected interval between
frames (including GOP/jitter). Set this before launching either the Viewer or a
standalone player:

```powershell
$env:CAMERA_PLAYER_STALL_SECONDS = '60'
python camera_viewer.py
```

Values from 5 through 300 seconds are accepted. Invalid values use 15 seconds.
Startup allowance becomes at least twice this interval (minimum 25 seconds).
Remove the environment variable to restore defaults. No scene-motion test is used;
an unchanged scene with normally delivered frames remains healthy.

## Diagnostics

`player-logs/` contains rotating application logs (512 KiB and two backups per
player process). A local camera ID identifies the files. Older retained files are
pruned to 24 per ID on subsequent starts; files still open by active Windows
processes cannot be deleted. Logs are retained at normal close. They record
timestamp, monotonic time, PID/thread, generation, state/reason/attempt, operation
entry/exit, 5-second pipeline measurements and Python/Tk/python-vlc/libVLC details.
The last native operation with an entry and no exit identifies where supervision
timed out; it does not establish the internals of a libVLC/GPU lock.

The supervisor detects a late Tk heartbeat independently while a session is
running. An optional all-thread stack dump can provide more context:

```powershell
$env:CAMERA_PLAYER_DUMP_SECONDS = '5'
python camera_viewer.py
```

Dumps are armed around worker native calls and rearmed by the Tk heartbeat, kept
in local `*-worker-stacks.log` / `*-tk-stacks.log` files with bounded rotation.
Choose 5–7 seconds to capture a worker before the normal 8-second watchdog.
These are Python stacks; a native dump may still be necessary to inspect native
locks. Review local file paths in stack dumps before sharing. Dumps are not uploaded.

Standalone launch writes structured diagnostics to the terminal. Viewer/Manager
video launches explicitly drain stdout/stderr on a background thread and relay
through a bounded terminal queue. A slow or absent terminal drops terminal copies
without blocking the pipe drainer; application files retain the diagnostics.
The relay leaves PTZ startup and shutdown behavior unchanged.

No authenticated URI, credentials, SOAP payload, raw exception body or command
line is logged. Arbitrary native VLC output is not forwarded (it can contain
secrets). Graphics detection uses fixed categories only. Raw native formats were
inspected locally against the **unauthenticated synthetic source only** during
development; those scratch diagnostics are not repository artifacts.
Launcher video arguments use `--` so credentials starting with a dash remain
positional values, and command-line parsing errors omit the rejected values.

## Reproducible tests

Run the regression suite from the active root sources:

```powershell
python -m unittest discover -s tests
```

Tests cover bounded callbacks, event bursts and stale generations, serialized
teardown, generic URI/IPv6/profile preservation, errors without secret disclosure,
input/decode/display progress, low cadence, cancellable backoff, real hung worker
replacement, independent sessions, Tk responsiveness/close, EOF after parent loss,
terminal draining, and the inherited availability/PTZ regressions.

The native bench additionally requires Windows, VLC matching Python's architecture,
FFmpeg with `libx264` on PATH, and Pillow for `--capture`. It generates H.264 video
with a changing frame/time pattern and PCMU tone, then serves RTP interleaved over
RTSP/TCP on **127.0.0.1 and ephemeral ports only**. A second source is a continuous
witness. `offline` closes sessions/refuses connections; `silence` keeps TCP/RTSP
open but withholds RTP. No firewall, NIC, router, camera or GPU setting is changed.

```powershell
python tools/test_rtsp_recovery.py --cycles 20 --durations 5,5,5,30,5,5,5,5,5,120,5,5,5,5,5,5,5,5,5,5 --initial-offline 5 --capture --output .release-work/rtsp-results.json
```

`--ffmpeg` accepts an explicit executable path when a package-manager alias is
unavailable. `--capture` keeps the synthetic window on top and captures only its
video rectangle, including monitors with negative coordinates. Do not cover or
move it during capture. The bench checks changing, sufficiently colored images,
video/audio counter progress, witness generation, Tk heartbeat, memory/handles,
thread/session counts and final worker reaping. It saves two synthetic screenshots.
Tone output is muted; automated audio evidence is buffer progression, not listening.
The test accepts a 35-second safety timeout and records actual recovery times;
compare them with the issue's proposed 20-second target.

The fixture closes its sockets and the players in `finally`. All fixture listener
threads are daemon threads in the test process; they cannot survive its exit.
Closing the test windows or Ctrl+C cancels the owned media processes. No external
RTSP daemon or firewall rule needs cleanup.

## Validation record and remaining work

See [measured results](player-recovery-results.md). Simulated native hangs and real
RTSP/GPU rendering checks are reported separately. Physical camera recovery,
H.265/H.265+, UDP transport, a real GPU device-removal incident and audible sound
recovery still require targeted validation; this change does not claim them.
The exact native lock behind the original field incident has not been captured.

To return to the released implementation, close the Viewer and its children and
switch the source checkout back to `main`/v0.2.8 (or use its preserved source
snapshot with the existing matching database/key arrangement). No schema, key or
camera-setting migration is involved. Never replace the existing encryption key.

## API references

* [Python Tkinter threading model](https://docs.python.org/3/library/tkinter.html#threading-model)
* [VideoLAN libVLC 3 media player API, including HWND requirements](https://videolan.videolan.me/vlc-3.0/group__libvlc__media__player.html)
* [python-vlc source and callback bindings](https://github.com/oaubert/python-vlc)
* [Python faulthandler](https://docs.python.org/3/library/faulthandler.html)
