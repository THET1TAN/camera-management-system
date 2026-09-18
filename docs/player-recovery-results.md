# Issue #9 validation results

Base: `main` 8c8a9d7, after v0.2.8 / PR #11. Date: 2026-09-18.

Environment: Windows 11 Pro 10.0.26200; Python 3.14.6, 64 bit; Tcl/Tk 8.6.15;
python-vlc 3.0.21203; libVLC 3.0.23 from the installed 64-bit VLC directory;
Intel UHD Graphics / D3D11VA observed in native synthetic-source diagnostics,
Intel driver 32.0.101.7085. An NVIDIA RTX 3060 Laptop GPU (driver 32.0.16.1060)
is also installed; it is not claimed as a separately validated renderer.

## Latest compatibility validation

After the [v0.2.8 behavior audit](player-compatibility-audit.md), **241 tests pass
in 13.058 seconds**, with no skips. This includes five-reading bitrate smoothing,
its one-second UI refresh, startup/reset/missing-counter handling, legacy empty
formatting, mute, resizing and missing-address validation. Playback parameters
passed to VLC are compared with the frozen release; scheme-prefixed ONVIF
addresses, explicit ports and IPv6 are checked without camera credentials.

With the restored **50 ms** playback cache, the updated native bench passed
**2/2 cycles** (5-second TCP interruption and 30-second open-socket silence).
Both screen comparisons show a changing synthetic image; the witness remains
generation 1. Recovery took **9.992 s / 6.552 s**, maximum Tk heartbeat gap was
**0.360 s**, and shutdown/reaping took **0.293 s** with no remaining owned workers.
Raw results: [compatibility rerun](validation/issue-9-compatibility.json).

The first attempt with the restored options failed its changing-image check.
Inspection found that the fixture waited a fixed 20 ms on receive before sending
each 20 ms audio packet, allowing its RTP clock to drift behind wall time. Its
receive timeout now respects the next packet deadline. The rerun above passed
with all restored playback options. This fixes test-source pacing; it does not
change camera settings or establish that all physical streams behave identically.

`git diff --check` passes. PTZ, availability, key handling, assets and historical
snapshots have no source diff from the base. The following 20-cycle measurements
are retained as earlier evidence; they predate the restored playback defaults
and must not be presented as a fresh 20-cycle run of this revision.

## Earlier recovery validation

The original baseline passed **203 tests** before changes. The updated full suite
passed **227 tests in 11.795 seconds** under Python 3.14.6 with no skips reported.
After limiting unbuffered-output changes to video children, the 12 child-process
regressions also passed (0.808 seconds). `git diff --check` passed. PTZ engine,
availability sources and all historical snapshots have zero diff from the base.

After restoring v0.2.8's icon-only mute button and removing the temporary volume
slider, all **227 tests passed again in 11.709 seconds**. The existing recovery
regression now clicks the real Tk button, observes mute in the replacement
process, checks that its icon/pressed state/title remain selected, then clicks
again and observes unmute. The Tk responsiveness test also uses the actual button.
These checks do not claim a physical listening test; the native audio owner is unchanged.

The earlier native run completed **20/20 cycles**, alternating TCP disconnect/refusal
and open-socket RTP silence, including outages of **5, 30 and 120 seconds** and an
initially unavailable source. Every cycle had advancing audio buffers and a
visibly changing synthetic image. All 20 screen comparisons passed; the witness
kept its original session (generation 1). The test took 462.885 seconds.

| Earlier 20-cycle measurement | Result |
| --- | --- |
| Source available → video/audio progress | **0.852–15.624 seconds**; 0/20 above the proposed 20-second target |
| Largest measured Tk heartbeat gap | **0.342 seconds**, below the 1-second target |
| Close → both workers reaped and fixtures closed | **0.441 seconds** |
| Remaining owned workers | **0** |
| Python threads while stable (test + two windows) | **10** throughout |
| RTSP sessions to interrupted fixture while stable | **1** throughout |
| Final supervisor / status-reader thread counts | **2 / 2**, for two windows |
| Interrupted worker RSS, first / last stable sample | **177.16 / 175.00 MiB** |
| Interrupted worker handles, first / last | **691 / 691** |
| Tk/test process RSS, first / last | **39.11 / 44.77 MiB** |
| Tk/test process handles, first / last | **268 / 268** |

Screen captures allocate temporary virtual-desktop image buffers in the test
process, so its intermediate RSS fluctuates. These measurements show no growth
in live workers, threads or sessions across this finite run; they do not establish
an unlimited-duration memory guarantee.

Raw numerical results: [final 20-cycle run](validation/issue-9-rtsp-final.json).
An earlier [calibration run](validation/issue-9-rtsp-calibration.json) also recovered
20/20 times, but a 15-second retry cap caused 8 recoveries above 20 seconds
(maximum 23.515 seconds). That led to the final 8-second cap. The final run also
includes the guard requiring both decode and presentation progress, preventing
repeated display of one old decoded frame from counting as recovery.

| Scenario | Evidence / status |
| --- | --- |
| Tk during hung discovery / native stop | Real disposable test processes, bounded replacement; real Tk heartbeat/close test |
| Error burst / old generation | Automated queue/state tests; one recovery, old generation ignored |
| Multi-brand URI/profile/IPv6 | Automated ONVIF response and URI tests; no vendor path substitution |
| Sparse frames / pipeline failure categories | Deterministic clock/counter tests; 1 frame / 5 seconds remains healthy |
| PTZ / availability / cascading close | Inherited regression suite; PTZ engine and availability sources unchanged |
| Real Windows VLC / RTSP TCP | Local synthetic H.264 + PCMU; source absence at launch and live outage/recovery |
| Earlier 20-cycle run | 20/20 before the compatibility restoration; measurements and raw results above |
| Current compatibility rerun | 2/2 with restored playback options and deadline-paced fixture; results above |
| Graphics device-removal recovery | Fixed log/event path and session replacement simulated; no real driver reset performed |
| Physical camera / H.265+ / UDP | Not performed |
| Audible sound recovery | Not listened to; automated evidence is advancing native audio output buffers while muted |

Early capture checks initially failed because a window capture returned cached
content and a screen capture did not include a secondary monitor with negative
coordinates. The bench now captures the foreground synthetic video rectangle
across all screens, checks image change and requires a substantial colored area.
Those early failures are not evidence that counter progress alone validates display.

The original user's precise deadlock has not been reproduced or attributed to a
particular internal libVLC lock. The regression tests exercise the hazardous
callback/native/Tk paths and process timeouts, and the native bench exercises real
RTSP interruptions without accessing a physical camera or changing the network.
