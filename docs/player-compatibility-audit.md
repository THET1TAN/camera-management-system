# Issue #9: audit against v0.2.8

Date: 2026-09-18. Baseline: main `8c8a9d7` / the frozen `~v0.2.8` source.
The review covers the actual player launch path, its controls and metrics,
playback options, ONVIF discovery, Viewer/Manager launch, PTZ, availability,
credentials, and shutdown. This is a source and automated-behavior comparison,
not a claim that every physical camera has been tested.

## Why the regressions happened

The issue required moving native work out of Tk and preventing callback reentry.
The implementation also replaced the presentation and statistics code without
first freezing their observable behavior. That unnecessarily removed the mute
icons/button styling and bitrate average, added a slider, and changed playback
defaults. Initial tests concentrated on recovery, isolation, process cleanup,
PTZ and availability. Passing those tests did not establish UI equivalence.
The earlier statement that behavior was preserved went beyond that evidence.
These were implementation and validation omissions, not changes requested by Joël.

A subsequent physical-camera report exposed a further gap in the bitrate checks:
libVLC's valid signed 32-bit byte count was treated as missing when it became
negative above 2 GiB. Local logs showed advancing decoded/displayed counters with
zero bitrate, including a session without recent replacement. The old logs had
already clamped negative counts, so they cannot prove the raw value for that exact
incident. The signed-counter defect was reproduced and fixed; tests now cross
both 2 GiB and 4 GiB boundaries, replace the media process, and verify the actual
Tk bitrate label. Native RTSP tests also require a numeric bitrate after recovery.

## Findings and disposition

| Behavior | v0.2.8 | First issue #9 implementation | Corrected behavior / evidence |
| --- | --- | --- | --- |
| Mute control | Speaker/crossed-speaker PNG, 16 px symbol, 30 px button; pressed style and `(Muted)` title | Text button and added volume slider | Existing icons, colors, style and title restored; slider removed. Real Tk clicks are tested through session replacement and unmute. |
| Bitrate smoothing | Mean of last five calculated readings | Raw approximately 500 ms deltas | Five-reading mean restored, new readings at intervals of at least one second. Uneven elapsed time, bursts, silence, reset and missing statistics tested. |
| Bitrate refresh / empty value | One-second UI timer; `-- Mbps` when unavailable/zero; two decimals; 100 Mbps display cap | Fast UI poll displayed each new raw value, including `0.00` | Independent one-second display timer, two decimals, empty value and legacy cap restored. Outage clears the stale number immediately. |
| Video latency and decoding | Effective `vlc_params` list: network/file/live caches 50 ms, no skip, drop late, two decoder threads, mux cache 0, no decorations/embedded UI | Network cache 300 ms and several options omitted | Playback options restored. Regression compares the options passed to VLC against constants extracted from the frozen release. Logging changes below remain intentional. |
| Scheme-prefixed ONVIF address | `ONVIFCamera(host, 80, ...)` accepts `http://host` / `https://host` | Shared bare-IP helper generated an invalid URL for these values | Reader accepts these forms again; explicit ports and IPv6 authorities retained. Fake SOAP requests verify URLs, first profile and complete RTSP URI. Bare hosts retain port 80. |
| Missing address on normal CLI launch | Argument error | Could open an indefinitely reconnecting window, because address became optional for the synthetic bench | Address required again unless an explicit loopback `--test-uri` is supplied; subprocess regression verifies argument error. |
| Initial size / resize | Initial 800 × 600; first valid video sizes to width 800 and native ratio plus 40 px controls | Same initial sizing, stable embedded surface | Source comparison and real Tk tests confirm initial ratio, unchanged HWND and preservation of a user's size through reconnection. Native synthetic test checks changing displayed images. |
| RTSP transport / stream selection | TCP, first ONVIF Media1 profile; hard-coded Hikvision URI fallback | TCP and first profile retained; discovered URI preserved | Intentional compatibility fix: use actual host, port, path, query and encoded credentials. No camera codec/configuration change. Hardware/Media2 support is not claimed. |
| Viewer / Manager | Launch player and PTZ as child processes | Adds argument separator and bounded video-output relay | Only video launch changes. Credentials beginning with a hyphen remain positional. PTZ launch and shutdown grace preserved; inherited launch/close tests remain. |
| PTZ / availability / database / icons | v0.2.8 sources and assets | Same sources/assets | Zero source diff for `ptz*`, `camera_health*`, `camera_key.py`, requirements, assets and release snapshots; existing regression tests run. Private database/key are excluded from publication and never overwritten during installation. |

## Necessary differences retained

- Native calls and ONVIF requests belong to the disposable media process, never
  Tk or a VLC event callback. Stop/cleanup can be bounded without freezing Tk.
- An unavailable stream shows a status overlay, and retries replace only its own
  session. Decoded/displayed counters, rather than the smoothed bitrate, determine
  recovery. Muting survives replacement. A window remains closable during failure.
- Resize no longer reattaches an HWND from Tk; the existing surface is resized and
  VLC keeps its default aspect-ratio scaling. Closing withdraws the window first,
  then destroys its HWND after its native worker is reaped.
- Unbounded verbose VLC files are replaced by bounded diagnostics without private
  URIs/SOAP. The native log callback recognizes graphics failure formats without
  formatting their arguments. Video-title overlays remain disabled to avoid
  displaying an authenticated media URI.
- ONVIF discovery uses bounded Media1 SOAP requests without creating an unrelated
  event subscription. It does not reintroduce the vendor-specific URI fallback.

## Verification boundaries

See [test results](player-recovery-results.md) for counts and native run evidence.
The new presentation and metrics tests exercise the behavior users see; the
playback-option regression references the frozen release rather than duplicating
the new implementation's option list. The RTSP bench verifies actual changing
images and process cleanup with real VLC, alongside an uninterrupted witness.

An audit cannot establish the absence of every possible regression. Physical
camera brands, H.265/H.265+, UDP, audio listening and a real GPU-removal event
remain unverified. The existing packaged-EXE build was not rebuilt or validated;
this PR and the local installation concern Python sources. The PR remains a
draft and must not be merged without Joël's explicit validation.
