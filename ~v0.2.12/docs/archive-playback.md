# Archive playback — issue #15, development build

Status: **v0.2.11-dev, implementation awaiting execution and qualification**.
Base: `69627fa2e29ce40abdaa4df853c5feb7e79c243f` (v0.2.10).
The user requested that development sources remain at the repository/application
root. `~v0.2.10` already contained the same active sources before this work
(ignoring CRLF/LF differences). Historical snapshots, credentials, encryption key,
camera configuration and saved live-window positions are not moved or replaced.

## Starting the browser

In Camera Viewer, select **Recordings** or the film icon next to a camera.
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
An unsupported speed or unconfirmed seek is reported outside the image. A last
known preview remains visible with its actual time distinct from the requested time. Native counter observations are not a guarantee
of actual screen or loudspeaker output.

The side panel contains calendar/camera filtering, details/exports and local
settings. At compact width or low height it uses a dismissible separate row
below the timeline, outside the image. The rendering HWND stays mapped. Toolbar groups wrap. Video is never reparented/recreated by
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

For VideoLink, **confirm the camera timezone** in Settings, for example
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
the requested day independently, with export discovery there are at most three metadata workers;
playback has at most two preparation jobs and export has one serial source
pipeline, with a writer lock preventing duplicate production for the same archive. There are no automatic historical scans, thumbnail downloads or NVR
recording jobs. No ONVIF Profile G, Dahua or RTSP-to-cache bridge is claimed.

## Received bytes, preparation and playback

The coordinator writes an original `.part`, enforces size/time/free-space bounds,
checks transport length when available and probes actual codecs/duration. A
validated received snapshot becomes `original.bin` by same-directory rename.
Its generic extension does not imply MP4. File age, size and a successful probe
do **not** establish camera-side finalization. Observed revisions include native
identity, reported size and raw bounds. A refreshed growing file can produce a
new revision; older received bytes are not called the full latest file.

Progressive eligibility depends on received media, not the backend name. A separate
owned job probes finite prefixes at 256 KiB, 1, 4, 16 and 64 MiB, with at most
8 seconds per attempt. It requires a supported codec, dimensions and finite
start timestamps, but no global duration. Insufficient bytes trigger a later
bounded attempt; HTML/XML/JSON is rejected. Full-file validation still requires
a positive finite duration at transport completion.

MPEG/MPEG-TS and MP4 with a complete initialization box before media can feed
FFmpeg through a growing binary pipe. MP4 layouts with media before metadata
wait for the complete response, with a visible reason. A header is eligibility,
not proof that every MP4 layout can be streamed: producer failure before any
publication falls back; failure after publication stops without overwriting
segments a player may still be reading. Original bytes are retained. No camera
HTTP Range support or proportional byte seek is assumed. Container/mode/reason
are logged for each actual transfer; compatibility on each physical camera
still requires user testing.

The media pipeline copies H.264/HEVC video. AAC is copied; other detected audio is
converted to AAC for HLS. No audio track is allowed. FFmpeg prepares nominal
four-second MPEG-TS segments and atomically publishes finalized segment/playlist
files. The application reads actual EXTINF durations and first video packet PTS
for absolute segment starts; gaps are not replaced with cumulative EXTINF time.
No `independent_segments` claim or artificial `-readrate` throttle is added.

libVLC loads a generated playlist over a private HTTP loopback port. The server
has an opaque session URL, explicit resource allowlist, no directory listing,
no redirects, no external roots, six concurrent clients, read timeouts, MIME and
no-store headers. Nothing is served to the LAN. An EVENT generation keeps all
referenced files pinned; one native file ending does not immediately end the
session. At most two preparation jobs overlap. The next archive can start
downloading within a horizon of at least 180 viewing seconds while the current
file is still arriving. Publication waits for the preceding producer to finish.

Successive groups use explicit discontinuities. Loopback responses translate
PTS/DTS/PCR/OPCR by a constant per archive into a common media clock, leaving
cached files intact and preserving packet spacing and A/V offsets. Byte ranges
receive the same transformed bytes as complete responses. Real segment time maps to
archive time per group, retaining offsets instead of compressing gaps. A join
currently allows at most 0.5 seconds of boundary difference due to timestamp
precision; larger gaps/overlaps stop the chain and require explicit navigation.
This tolerance is an implementation choice **awaiting OSD/frame-counter
qualification**, not proof of lossless boundaries. Sessions hold at most four
native archives; a new generation continues at their end with preserved controls.
This rollover can introduce buffering and must be checked on the actual player.

The first public HLS generation uses the ceiling of the longest segment actually
available. A later longer segment retires the native owner and creates a new
playlist URL immediately, without waiting for the complete archive. Each public
URL retains its original TARGETDURATION. Video is copied, segments are cut at
keyframes, and no independent-segment guarantee is invented. Variable GOPs may
still cause an interruption; measured durations and generation changes are logged.
The opening reserve is two viewing seconds with a minimum of two archive seconds
(8 seconds at 4×), separate from the 120-second comfort reserve.

## Scrubbing and evidence

Timeline motion coalesces the latest target every 125 ms instead of postponing
all work until release. An independent mailbox services it while downloads or
preparation block the coordinator. Seeking within prepared coverage reuses the
same libVLC owner, playlist, producer and download. A target not yet received in
the same archive waits for its bytes. Dragging outside the current source holds
an explicitly labelled previous frame; committing that target opens the new
source once. A distant cold target still waits for sequential reception: an RTSP
bridge and camera Range support are not implemented.

Preview temporarily mutes and freezes the native player after a confirmed frame;
it never changes the selected rate, volume or saved pause/mute state. Release
issues a final target and restores those controls after new decoded/displayed
counters plus an observed landing or successive advancing positions compatible
with elapsed time and rate. Invalid/reset statistics require a fresh baseline;
unchanged counters or an implausible position are insufficient. This is native evidence, not frame-exact positioning
or proof of pixels on screen. Requested and last confirmed preview times are
shown separately; local seeks keep the stable surface with a small status strip.

English UI text, months, weekday labels, tooltips, errors and CLI help live in
`playback/presentation.py`. Dates remain YYYY-MM-DD with explicit time zones;
America/Toronto and DST rules are unchanged. F8 records an explicit user report
of a visible new frame, including reaction delay. It is distinct from native
counter events. See the [native-boundary evidence and two-file trial](archive-playback-boundaries.md)
and [progressive and scrubbing trial](archive-playback-progressive.md)
for cold-cache, slowed-transfer and visual qualification commands.

## Cache, export and shutdown

See the [consolidated implementation and qualification guide](archive-playback-consolidated.md)
for the current selection/export and storage behavior. Store owns a shared 8 GiB
media budget, 2 GiB free-space margin, 14-day inactivity retention and 90%/75%
preventive thresholds. Settings are preserved and editable in Storage & Cache.
Atomic reservations distinguish written bytes from remaining allocations;
per-owner reference counts protect playback, playlists, HTTP readers, preparation
and export. Maintenance belongs to application lifecycle, outside Tk. Unknown
files and links/junctions are preserved, never followed during deletion.

Manual cleanup previews scope and estimated space, then confirms and rechecks
leases. Eviction removes cached state while preserving observed remote metadata.
Download original saves a complete received original outside the cache; cache is
not permanent backup. Incident holds are explicitly identified in local settings.

Select range stores camera, track and UTC endpoints independently of playback.
Export selection creates a separate Precise job across all intersecting archives,
not just the current playback generation. Useful portions are decoded/re-encoded
and assembled to one normalized MP4 with an audit sidecar. Confirmed gaps require
an explicit neutral/available-only/cancel choice; unknown coverage cannot become
a confirmed gap. Source frames limit precision, and OSD/audio still need visual
qualification. Exports use bounded temporary workspaces and protect only each
source stage. No keyframe-remux mode is exposed as Precise.

Closing cancels producers and export, reaps owned processes, closes the player,
clears published resources and releases each owner's protections. Outstanding
HTTP readers retain their own leases until their handles close. No external
process is killed. Source files, credentials and stable snapshots are preserved.

Rotating `events.jsonl` in the cache records public event categories, counts,
bytes, durations and sampled native counters. It contains no request URL, token,
raw native stderr, exception body or media. Monotonic milestones distinguish prefix
analysis, publication, native counter deltas and user-reported screen observations
(F8). No displayed-first-frame SLA is claimed.

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

**Agent verification:** static source review and Python 3.9 syntax inspection only;
no tests, application, camera request, FFmpeg, VLC, video or audio launched.

**User evidence at `4366478`:** all 44 Playback tests passed in 0.547 s under
Python 3.9.13. C3/101 returns 50 then 14 archives, complete. C3/103 returns
track 101 and is rejected as `track-mismatch`; auto retains the 64 good records
with `tracks-partial`. This secondary-track cause is confirmed only for this
camera and search. These results do not validate progressive start or scrubbing.

**Current changes:** additional prefix/HLS/seek/UI regressions and an explicit
slowed-transfer fixture with frame counter are prepared but not run. The
[progressive trial guide](archive-playback-progressive.md) includes full commands,
expected evidence and remaining limitations. Pointer targets are sampled at 125 ms; the native regulator admits one operation
and one latest pending target. Neither value is a measured preview frame rate. Physical container
compatibility, HLS growth, 0.5/1/2/4×, seek latency/OSD, dynamic joins, pauses,
audio sync, responsive layout and DPI still need user qualification.

Cross-archive Precise export is implemented but unqualified; the independent
job includes ranges spanning more than four archives. The optional RTSP bridge
and Profile G are not implemented. Keep the issue open and the PR in draft.

See [the user-run test guide](archive-playback-testing.md).

Implementation references: [FFmpeg HLS and muxers](https://ffmpeg.org/ffmpeg-formats.html),
[python-vlc API](https://python-vlc.readthedocs.io/en/latest/api.html),
[Bootstrap Icons](https://icons.getbootstrap.com/),
[Tk geometry management](https://docs.python.org/3/library/tkinter.html#geometry-management).
