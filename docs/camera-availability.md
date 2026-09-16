# Camera availability — v0.2.8 / issue #10

The indicator reports service reachability, not successful playback, valid video
frames, or player recovery. It does not open an RTSP session or send SETUP/PLAY.

## Decision order

1. Send `OPTIONS * RTSP/1.0` to the discovered or configured RTSP endpoint. A
   valid 2xx response or 401 authentication challenge means **Online**. Credentials
   are not tested by this request. A valid RTSP error means **Degraded**, without
   further probes; 405/501 are described as a limited check.
2. If no valid RTSP response arrives, try a TCP connection to the configured
   HTTP/ONVIF port. Success means **Degraded**. This is port reachability, not an
   authenticated check of ONVIF health.
3. Only after both checks fail, send one ICMP echo. A successful echo means
   **Degraded — camera responds to ping, services unavailable**. Windows native
   IPv4/IPv6 APIs are used; no command prompt or ping child process is created.
4. If nothing establishes availability, count one failed round. Two consecutive
   failed rounds produce **Unreachable**. A TCP connection accepted by RTSP but
   yielding no valid response is still positive reachability evidence: **Degraded**.

The first state is gray / Checking. The previous state is retained during a single
failed round. Any positive check
resets the failure counter. A successful RTSP check restores Online immediately;
a ping-only recovery restores Degraded. Unreachable does not mean the camera is
physically powered off. ICMP failure alone never produces an offline decision.

## Endpoint discovery and configuration

Existing cameras use their saved address and credentials, with the existing
ONVIF default `http://<camera>/onvif/device_service` on port 80. The monitor makes
three read-only SOAP calls: GetCapabilities(Media), GetProfiles, GetStreamUri.
It uses the first media profile, matching the current player's profile selection.
It preserves the returned RTSP hostname, port and TLS choice. There is no
Hikvision path or unconditional RTSP port assumption. Port 554 (RTSP) or 322
(RTSPS) is used only when omitted from a valid URI.

The discovered endpoint is cached in memory for the Viewer session; userinfo,
path, query and credentials from the URI are discarded. There is no authenticated
ONVIF polling on every healthy check, no WSDL download, and no event subscription.
An unsuccessful discovery is retried after at least 60 seconds. After three RTSP
failures an existing discovery can also be refreshed, respecting the same delay.
The existing player's URI rewriting is unchanged by this issue; the indicator
describes the discovered service, not whether that player-specific path works.

For custom ONVIF ports/paths or a camera without usable ONVIF Media discovery,
copy `camera_health.example.json` to `camera_health.json` beside the active
scripts and replace the example IDs and addresses. Entries are keyed by camera ID.
`onvif_url` overrides the management service; optional `rtsp_url` bypasses discovery.
The RTSP URL's path is not transmitted by OPTIONS. URLs must not contain userinfo;
the encrypted database provides any credentials needed for ONVIF discovery.
Do not put credentials in URL paths or query strings. This local file is ignored
by Git. Invalid configuration leaves checks Unknown while Play/PTZ remain usable.
Restart Viewer after editing this file, or close Camera Manager to reload it.

ONVIF Media2-only discovery is not implemented; an explicit RTSP URL is supported.
TLS uses system certificate validation. ICMP fallback is implemented on Windows,
the supported desktop platform. On another OS it is unavailable, not a successful
host check. Multiple DNS addresses use the first resolved address for ICMP.

## Compact display

Each camera keeps its own dot, status and controls. The footer shows one aggregate
line, for example `2 online · 1 unreachable`, and refreshes automatically. It does
not switch to a single camera when hovered or focused, and no diagnostic text or
timestamps expand the footer at the expense of the camera list. Online means the
RTSP service is available; explanatory limits belong in this guide.

## Scheduling and lifetime

`HealthSettings` centralizes the initial values: 15 seconds between completed
rounds, 2-second network operation timeouts, two failed rounds, four workers,
60-second discovery retry, and three RTSP failures before rediscovery. A round
with fallbacks takes longer than a healthy round; discovery also has several
operations. Large camera lists may add queue time. There is at most one in-flight
round per camera and at most four overall, without an accumulating backlog.

One separate monitor process owns the fixed worker pool and all camera network
I/O, including DNS and discovery. Only status messages return to Tk; a Tk `after`
callback drains them every 100 ms. The monitor participates in the existing
child-process close contract. Closing signals cancellation immediately; if it
has not exited after 0.5 seconds, the parent terminates this monitor and keeps
polling for exit without blocking Tk. Video/PTZ/Manager shutdown notifications
are sent at the same time. The existing PTZ grace period is preserved. The
monitor also watches its parent so it exits if the Viewer process disappears.

The helper is deliberate: OS DNS and third-party HTTP calls cannot always be
interrupted reliably inside a Python thread. Isolating them gives the Viewer a
bounded shutdown even in those cases. A monitor crash turns stale colors gray.
No health-probe exception text, credentials, SOAP bodies, or authenticated URIs
are written to logs or returned to the UI.

## Validation

Automated tests cover fallback order (including ping only), RTSP 200/401/errors,
fragmented and malformed responses, wrong CSeq, silent sockets, timeouts,
anti-flapping, recovery, discovery/cache, custom ports, credential-free results,
bounded concurrency, real monitor-process shutdown, native IPv4/IPv6 ping, and
Tk message delivery, reload, aggregate status display, and preserved controls.

Read-only physical checks on September 13, 2026 discovered two configured cameras
and received positive RTSP responses. First checks took about 0.36 and 1.06 seconds;
cached checks took about 0.09 seconds or less. A third configured camera did not
establish availability. No camera connectivity or service was changed by this test.

A 30-second hidden Viewer check then displayed Online / Online / Unreachable
for those cameras, processed 277 Tk heartbeat callbacks (maximum gap 0.115 s),
and closed in about 0.067 s with its monitor process confirmed exited.
The automated suite currently contains **203 passing tests**.

The user's subsequent diagnostic reported successful OPTIONS and DESCRIBE for
both responding cameras. One camera omitted OPTIONS from its Public methods
header but accepted the request with status 200; the check uses the actual reply,
not that advertised list. The other required Digest authentication for DESCRIBE,
then returned 200. These results do not require changing the availability rules.

On September 16, 2026, the user confirmed the final display works correctly and
approved freezing v0.2.8 and merging PR #11 into main. The following checklist
distinguishes that confirmation from additional physical scenarios that have
not been individually documented; they are retained for future regression work.

- [x] User confirmed the actual Viewer display works correctly and approved v0.2.8.
- [ ] Disconnect a working camera; verify confirmation and Unreachable while other
      cameras and controls stay responsive.
- [ ] Reconnect it; verify automatic Online recovery.
- [ ] Make RTSP unavailable while HTTP/ONVIF responds: Degraded and no ping.
- [ ] Make RTSP and HTTP/ONVIF unavailable while ICMP responds: Degraded via ping.
- [ ] Block ICMP alone: a healthy RTSP service remains Online.
- [ ] Close Viewer during checks while a player/PTZ window is owned by it.
- [ ] Observe a longer run with active video/PTZ for unwanted load or disruption.

Protocol references: [RTSP 1.0](https://www.rfc-editor.org/rfc/rfc2326.html),
[ONVIF Media](https://www.onvif.org/specs/srv/media/ONVIF-Media-Service-Spec-v240.pdf),
[Windows ICMP](https://learn.microsoft.com/en-us/windows/win32/api/icmpapi/nf-icmpapi-icmpsendecho),
[Windows ICMPv6](https://learn.microsoft.com/en-us/windows/win32/api/icmpapi/nf-icmpapi-icmp6sendecho2).
