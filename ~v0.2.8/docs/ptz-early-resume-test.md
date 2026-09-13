# r9 C withdrawn — partial key-release regression

**Withdrawn after the September 11, 2026 camera test. Do not use C as a candidate.**
r9 B was approved for main as v0.2.7 on September 12, with correct partial releases and
simultaneous movement and zoom in its initial user test. B still has a release
pause and needs extended field testing. The previous baseline is now replaced by B.

## Observed regression

The user held a diagonal, released one direction, and observed continued diagonal
movement. The C trace confirms that the released tilt axis was detected and sent
as zero. The sequence relative to the partial release was:

| Event | Approximate elapsed time |
| --- | --- |
| Input changes from (0.5, -0.5, 0) to (0.5, 0, 0) | 0 ms |
| Full neutral vector dispatched | 1 ms |
| Early resume (0.5, 0, 0) dispatched | 90 ms |
| Early resume response accepted | 237 ms |
| Neutral response accepted | 263 ms |
| Latest vector (0.5, 0, 0) reapplied | 263 ms |
| Reapplication response accepted | 316 ms |

All three requests returned HTTP 200. Despite the accepted reapplication, the
user observed that the released axis continued. A full release later sent Stop.
The issue is therefore not explained by a missed key-up event in this trace.

The internal execution order is unknown. An overlapping move may interfere with
the neutral transition, but the trace does not prove the firmware mechanism.
The simulations covered late neutral execution; they did not establish that an
overlapping move cannot cancel or alter a neutral transition inside the camera.
HTTP success and final command order are insufficient to infer mechanical state.

## Decision and current entry points

- The application no longer constructs or selects EarlyResumeWorker or its second
  ONVIF client. The module remains solely as a historical experiment for analysis.
- `camera_viewer_early_resume_test.py` announces that C was withdrawn and launches
  sequential B instead. Existing local C shortcuts consequently return to B too.
- A stale CAMERA_PTZ_EARLY_RESUME=1 setting on direct PTZ startup also selects B;
  it cannot reactivate overlap. The local diagnostic trace records this fallback.
- B's command worker is unchanged. It still waits for the neutral response before
  resuming all held axes, keeping its known pause.

After closing old PTZ windows, use:

```powershell
python camera_viewer_direct_test.py
```

The title must show **v0.2.7 r9 - Neutral transition B**. No new C test is requested.
The C withdrawal did not change camera settings or stored credentials. The later
v0.2.7 release promotes B as the normal application behavior.

## Retained research and measurements

[ONVIF PTZ section 5.3](https://www.onvif.org/specs/srv/ptz/ONVIF-PTZ-Service-Spec.pdf)
describes replaceable move commands without guaranteeing a response latency or
the execution order of concurrent requests. C attempted one early resume after
about 60 ms on a separate HTTP session, then reapplied the latest state after both
responses. The actual dispatch in this test was about 89 ms after the neutral.

The preceding B session measured a 272 ms median neutral response. A zero-only
stationary probe measured about 252 ms for ContinuousMove with an explicit native
timeout, 245 ms with the default timeout and 242 ms for RelativeMove with zero
translation (three samples per variant). These measurements did not show a useful
gain from merely changing the neutral command. They do not measure motor latency.

## Checks after withdrawal

The release suite now contains 162 passing tests, including the existing B regressions and new checks
that an old C launcher clears the overlap setting and that direct PTZ startup
with a stale C environment creates only the sequential B worker. Archived C
simulation results remain historical; they are not evidence of physical validity.

Future work on the pause must preserve B's observed release behavior. The failed
concurrent strategy must not be re-enabled on the basis of HTTP success alone.
