# Archive playback — issue #15, development build

Status: **v0.2.11-dev, implementation awaiting execution and qualification**.
Base: `69627fa2e29ce40abdaa4df853c5feb7e79c243f` (v0.2.10).
The user requested that development sources remain at the repository/application
root. `~v0.2.10` already contained the same active sources before this work
(ignoring CRLF/LF differences). Historical snapshots, credentials, encryption key,
camera configuration and saved live-window positions are not moved or replaced.

## Starting the browser

In Camera Viewer, select **Enregistrements** or the film icon next to a camera.
Both open the same non-modal archive window with its own embedded video surface.
The calendar does not start a video. Choose a camera, a day, then click the
timeline or enter `HH:mm:ss` and select the clock button. The initial viewport
spans 30 minutes. Use ±10 s, zoom, horizontal scrolling and the explicit next
recording command. Changing camera during playback preserves the requested time.

The active camera uses its real database ID (`C42` means camera ID 42).
Selecting several cameras changes calendar badges and coverage tracks; only
one archive video is played. A badge is presence in the index, not complete
coverage of a day. `*` denotes locally cached data; details retain query time
and error/partial status. A failed query does not erase earlier results.
Unknown CGI durations remain point markers until inspection of actual media.

Sound starts **muted**. The speaker button enables it. Pause, chosen speed
(0.5/1/2/4), mute and volume belong to the archive controls and survive media
replacement. There is no automatic reduction to 1× and no speed-dependent mute.
An unsupported speed or unconfirmed seek is reported; a stale image is covered
on a gap, buffering or failure. Native counter observations are not a guarantee
of actual screen or loudspeaker output.

The side panel contains calendar/camera filtering, details/exports and local
settings. At compact width or low height it becomes a dismissible drawer in the
same window. It may cover part of the video while open; the rendering HWND stays
mapped underneath. Toolbar groups wrap. Video is never reparented/recreated by
layout. The provisional minimum size is 520×460; Windows Snap/DPI ergonomics must
be calibrated through the user tests below. Playback geometry is not persisted
in the live-window position database.

## Local settings and device identity

Settings are stored in ignored `playback.json`. There is no automatic credential
copy: `camera_credentials.db` is opened read-only using the installation's
existing encryption key. The new index encrypts recording locators with that key.
No camera password, UID, replay URI or camera IP is included in logs or subprocess
arguments. Worker commands contain only local executable/script paths; the
session configuration is sent through a private pipe.

For VideoLink, **confirm the camera timezone** in Réglages, for example
`America/Toronto`, then apply the local setting. It is separate from
the display timezone. `tzdata` supplies IANA zones on Windows. Naive CGI times
during a repeated/nonexistent hour are rejected explicitly instead of inventing
a unique UTC instant. Date arithmetic uses 23/24/25-hour days; a known recording
crossing local midnight marks each intersected day.

ISAPI timestamps with explicit offsets keep those offsets. The optional local
`time_shift` correction is explicit, in seconds, applied consistently to searches
and results. The observed ANPVIZ `-05:00` anomaly is not silently generalized.
Check the visible camera OSD before deciding to configure a correction. The
application never sets the camera clock.

ISAPI identity uses discovered serial/MAC, model and firmware, hashed locally.
VideoLink attempts read-only ONVIF GetDeviceInformation with the existing
Digest/WS-Security helper. If no stable identity is returned, provide a local
`revision` label and change it after replacing the device. A UID is not identity.
Caches from different identities/revisions are not interchangeable remote files.

`playback.example.json` documents the schema. Keys under `cameras` are actual
database IDs. Optional `endpoint` accepts an HTTP(S) origin and port without
userinfo/path/query. TLS certificate verification remains enabled. `track` can
restrict a discovered ISAPI track or CGI stream index; an empty field permits
automatic selection. Different track IDs are not inferred to be different
physical cameras. Applying settings cancels the previous jobs, waits outside Tk
for metadata owners to retire, reloads cameras by database ID, and starts a fresh
search for the selected camera/day. Credentials, keys, original media and exports
are retained. No archive window restart is required for these settings.

## Backends and bounded searches

ISAPI validates DeviceInfo/TrackList/CMSearchResult roots and application statuses,
uses a stable searchID, advances `searchResultPostion` by the returned count,
deduplicates and reports repeated/count-mismatched/limited pages as partial.
The original complete playbackURI is XML-escaped exactly once for download.

VideoLink supports the observed `RecordQueryInfo` response containing repeated
`items` elements with attributes (`filepath`, `filesize`, `start_time`, etc.).
Its query uses video `media_type=3`; a previous camera-local day is included to
find archives that may cross midnight. The response's exhaustion contract is
not established: even an empty list remains **unconfirmed coverage**, never a
confirmed empty day. Session renewal is bounded. CGI passwords/UIDs in legacy
HTTP queries are not encrypted by Digest; use a trusted network and validated
HTTPS where the camera supports it. No raw request logs are retained.

One background worker progressively indexes only the visible month. It caches
successful queries for 15 minutes and incomplete/errors for 60 seconds; Refresh
overrides this. Changes of month/filter cancel obsolete work. Playback can query
the requested day independently, so at most two metadata operations may overlap;
there is one media download pipeline globally (therefore no more than one per
camera). There are no automatic historical scans, thumbnail downloads or NVR
recording jobs. No ONVIF Profile G, Dahua or RTSP-to-cache bridge is claimed.

## Received bytes, preparation and playback

The coordinator writes an original `.part`, enforces size/time/free-space bounds,
checks transport length when available and probes actual codecs/duration. A
validated received snapshot becomes `original.bin` by same-directory rename.
Its generic extension does not imply MP4. File age, size and a successful probe
do **not** establish camera-side finalization. Observed revisions include native
identity, reported size and raw bounds. A refreshed growing file can produce a
new revision; older received bytes are not called the full latest file.

For ISAPI, after a 2 MiB prefix a separate probe checks whether MPEG/MPEG-TS is
usable progressively. When so, a bounded disk reader feeds received bytes to
FFmpeg's binary stdin, waits at temporary EOF and closes only at transport EOF.
The original and HLS output both count toward the quota. If the prefix is not
usable and nothing was published, the complete-file path remains available.
MP4/CGI waits for receipt of the complete transport response before preparation;
no proportional byte seek or unverified HTTP Range resume is used.

The media pipeline copies H.264/HEVC video. AAC is copied; other detected audio is
converted to AAC for HLS. No audio track is allowed. FFmpeg prepares nominal
four-second MPEG-TS segments and atomically publishes finalized segment/playlist
files. The application reads actual EXTINF durations, not `segment number × 4`.
No `independent_segments` claim or artificial `-readrate` throttle is added.

libVLC loads a generated playlist over a private HTTP loopback port. The server
has an opaque session URL, explicit resource allowlist, no directory listing,
no redirects, no external roots, six concurrent clients, read timeouts, MIME and
no-store headers. Nothing is served to the LAN. An EVENT generation keeps all
referenced files pinned; one native file ending does not immediately end the
session. The next archive is prepared when the reserve falls below 120 seconds
of viewing time (480 seconds of archive at 4×).

Successive groups use explicit discontinuities. Real segment time maps to
archive time per group, retaining offsets instead of compressing gaps. A join
currently allows at most 0.5 seconds of boundary difference due to timestamp
precision; larger gaps/overlaps stop the chain and require explicit navigation.
This tolerance is an implementation choice **awaiting OSD/frame-counter
qualification**, not proof of lossless boundaries. Sessions hold at most four
native archives; a new generation continues at their end with preserved controls.
This rollover can introduce buffering and must be checked on the actual player.

The initial HLS target duration is fixed at 12 seconds. If a longer GOP produces
a larger segment, publication waits for finalized preparation, then retires the
old native owner and rebases into a new playlist generation with the measured
target duration. This fallback preserves the requested position/controls, but
may buffer and still needs qualification. Accurate decoding from arbitrary
keyframe layouts is not certified. Native seeks are accepted only after the
reported position is within 1.5 seconds and decode/display counters exist; this
is a bounded confirmation, **not frame-exact positioning**.

## Cache, export and shutdown

Default cache: `%LOCALAPPDATA%\CameraManagementSystem\playback`, outside OneDrive.
Default quota 8 GiB, free-space reserve 2 GiB, archive limit 2 GiB, inactive TTL
14 days. `cache_directory` may point to another dedicated local folder. Do not
store evidence in this evictable cache. All received originals, partial files,
segments and producer temporaries are counted. Active writers/playlists are
pinned. Cleanup deletes only inactive owned cache files; exports are outside
that policy. An OS lock prevents two windows from mutating the same cache.
The independent SQLite schema is version 1; unknown newer schemas are refused.
No migration of the camera-credential database is performed.

**Original** preserves the exact received source bytes plus metadata/SHA-256,
outside the evictable cache. It is an observed export, not proof of camera-side
finalization or cryptographic authenticity. No silent overwrite is allowed.
**A/B → Exporter la plage** remuxes within the currently received native archive
to MKV, copying video and adapting audio if required. Cross-archive range export
and frame-exact re-encoding are not yet exposed. The remux preserves packet
timestamps with an origin at the original archive start; ffprobe measures the
actual first/last video packet bounds. The sidecar records requested and effective
intervals, their mapping basis and precision warning. If bounds cannot be
verified, the export fails explicitly instead of inventing them. This mapping
still needs source/OSD qualification; packet timestamps are not independent
evidence of camera-clock correctness.

Camera HTTP/DNS live in separate, killable owned processes. Private pipes carry
commands/results; UI cancellation terminates only the corresponding camera job.
Replies and index views are bounded (20,000 entries per view by default), with
explicit errors rather than false complete coverage at the limit.
Native operations stay in one disposable libVLC owner. A supervisor bounds
operations and reaps it before replacement. EOF/pause/buffering are archive
states, not live reconnection triggers. FFmpeg/ffprobe are hidden owned processes
with Windows kill-on-owner-exit jobs. Closing Playback cancels only its jobs,
server and player. Viewer waits for the archive HWND's owner to retire, while
notifying live/PTZ children through their existing shutdown contract. Media is
unpinned only after active local HTTP readers have drained. Local filesystem
failure remains distinct from a bounded camera/network or native-operation timeout.

Rotating `events.jsonl` in the cache records public event categories, counts,
bytes, durations and sampled native counters. It contains no request URL, token,
raw native stderr, exception body or media. Only a transport-completion duration
is currently measured end-to-end; no displayed-first-frame SLA is claimed.

## Visual assets and distribution

Bootstrap Icons **v1.13.1**, official SVG sources and MIT license are vendored in
`assets/bootstrap-icons`. A transparent PNG sprite contains separately rasterized
20/25/30/40 px versions (100/125/150/200% starting points), normal/disabled colors.
Tk crops and caches images from this sprite; it requires no native SVG support,
icon font, browser, CDN or runtime image-conversion dependency. Shared semantic
buttons provide hover/focus help with a single delayed in-window tooltip and
text fallback. Focus/disabled states are visible; screen-reader support remains
to be assessed on the actual runtime.

Regeneration, for developers only: `tools/fetch_playback_icons.ps1`, then
`node tools/generate_playback_icons.cjs` with development dependency `sharp@0.35.4`.
The application performs neither operation. No executable was built or tested.

## Evidence and remaining acceptance work

**During this implementation:** repository/source comparison, reading of saved
XML structures, asset generation and static source review only. No application,
camera probe, FFmpeg, VLC, test suite, video or audio has been run. No GUI captures
or performance measurements of this implementation exist. Historical issue #15
observations remain observations of earlier prototypes.

**First user trial and follow-up correction:** the user observed configuration
and generic response errors in the UI. Their test command used a different
Python and failed to import `cryptography`; it did not execute the tests. The
subsequent correction preserves successful ISAPI tracks on a secondary failure,
retains each auto-detection attempt, and adds redacted per-stage protocol traces.
Track discovery reports `Enable` as information, not proof of archive presence.
HTML HTTP-200 replies remain rejected. See the
[diagnostic guide](archive-playback-diagnostics.md) for evidence versus hypotheses,
the explicit interpreter and sequential, one-day commands. Corrected runtime
behavior has not yet been validated; no camera was contacted for the correction.

The code includes synthetic unit checks, an optional media fixture generator and
a separate offline browser mode. All execution results remain **pending the
user's tests**. In particular: camera compatibility, HLS growth/reload,
0.5/1/2/4×, actual seek time/OSD, dynamic archive joins, long pauses, loss/recovery,
audio sync, responsive HWND stability and 100/125/150/200% DPI are unvalidated.
Cross-archive range export remains an outstanding implementation item; ranges
inside one received native archive are supported. The optional RTSP bridge
and Profile G are not implemented. Keep the issue open and the PR in draft.

See [the user-run test guide](archive-playback-testing.md).

Implementation references: [FFmpeg HLS and muxers](https://ffmpeg.org/ffmpeg-formats.html),
[python-vlc API](https://python-vlc.readthedocs.io/en/latest/api.html),
[Bootstrap Icons](https://icons.getbootstrap.com/),
[Tk geometry management](https://docs.python.org/3/library/tkinter.html#geometry-management).
