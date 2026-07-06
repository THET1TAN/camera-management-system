"""Diagnostic tool for ONVIF/RTSP audio backchannel support.

This script checks whether a configured camera appears to support sending audio
from the client to the camera speaker.

It is intentionally read-only: it does not stream microphone audio yet. It only:
  1. Reads camera credentials from the existing local DB, or CLI arguments.
  2. Queries ONVIF media capabilities related to audio output/decoder config.
  3. Sends an RTSP DESCRIBE request with the ONVIF backchannel Require header.
  4. Prints a compact report to help decide whether Push-to-Talk can be added.

Examples:
    python camera_audio_backchannel_probe.py --camera-id 1
    python camera_audio_backchannel_probe.py --ip 192.168.1.50 --username admin --password secret
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import re
import socket
import sys
from dataclasses import dataclass
from getpass import getpass
from typing import Dict, Iterable, Optional, Tuple
from urllib.parse import quote, urlparse, urlunparse

try:
    from onvif import ONVIFCamera
except Exception:  # pragma: no cover - this is a user environment diagnostic
    ONVIFCamera = None

BACKCHANNEL_REQUIRE_HEADER = "www.onvif.org/ver20/backchannel"


@dataclass
class CameraCredentials:
    camera_id: Optional[int]
    ip: str
    username: str
    password: str
    onvif_port: int = 80


@dataclass
class RtspResponse:
    status_code: Optional[int]
    status_line: str
    headers: Dict[str, str]
    body: str
    raw: str


def _sanitize_secret(value: Optional[str]) -> str:
    if not value:
        return ""
    return "***"


def _redact_uri(uri: str) -> str:
    try:
        parsed = urlparse(uri)
        if parsed.username or parsed.password:
            host = parsed.hostname or ""
            if parsed.port:
                host = f"{host}:{parsed.port}"
            return urlunparse((parsed.scheme, host, parsed.path, parsed.params, parsed.query, parsed.fragment))
    except Exception:
        pass
    return uri


def load_camera_from_db(camera_id: int) -> CameraCredentials:
    """Load camera credentials from camera_manager.py's existing encrypted DB."""
    try:
        import camera_manager  # Local project module
    except Exception as exc:
        raise RuntimeError(f"Unable to import camera_manager.py: {exc}") from exc

    for camera in camera_manager.get_cameras():
        current_id, ip, username, encrypted_password, _ptz = camera
        if int(current_id) != int(camera_id):
            continue
        try:
            password = camera_manager.cipher.decrypt(encrypted_password).decode()
        except Exception as exc:
            raise RuntimeError(f"Unable to decrypt password for camera {camera_id}: {exc}") from exc
        return CameraCredentials(
            camera_id=int(current_id),
            ip=str(ip),
            username=str(username),
            password=password,
        )

    raise RuntimeError(f"Camera id {camera_id} was not found in the local DB")


def get_onvif_report(creds: CameraCredentials, profile_index: int) -> Tuple[Optional[str], Dict[str, object]]:
    """Query ONVIF media service for audio output/backchannel-related objects."""
    report: Dict[str, object] = {
        "onvif_available": ONVIFCamera is not None,
        "connected": False,
        "profiles": 0,
        "selected_profile_token": None,
        "stream_uri": None,
        "audio_sources": None,
        "audio_outputs": None,
        "audio_output_configurations": None,
        "audio_decoder_configurations": None,
        "errors": [],
    }

    if ONVIFCamera is None:
        report["errors"].append("onvif-zeep is not importable in this Python environment")
        return None, report

    try:
        camera = ONVIFCamera(creds.ip, creds.onvif_port, creds.username, creds.password)
        media_service = camera.create_media_service()
        report["connected"] = True
    except Exception as exc:
        report["errors"].append(f"ONVIF connection failed: {exc}")
        return None, report

    profiles = []
    try:
        profiles = media_service.GetProfiles()
        report["profiles"] = len(profiles)
    except Exception as exc:
        report["errors"].append(f"GetProfiles failed: {exc}")

    selected_profile = None
    if profiles:
        try:
            selected_profile = profiles[profile_index]
        except IndexError:
            report["errors"].append(f"Profile index {profile_index} is out of range; using profile 0")
            selected_profile = profiles[0]

    if selected_profile is not None:
        report["selected_profile_token"] = getattr(selected_profile, "token", None)
        try:
            stream_uri = media_service.GetStreamUri({
                "StreamSetup": {"Stream": "RTP-Unicast", "Transport": "RTSP"},
                "ProfileToken": selected_profile.token,
            }).Uri
            report["stream_uri"] = _redact_uri(stream_uri)
        except Exception as exc:
            report["errors"].append(f"GetStreamUri failed: {exc}")
            stream_uri = None
    else:
        stream_uri = None

    def count_or_error(method_name: str) -> None:
        try:
            method = getattr(media_service, method_name)
            result = method()
            if result is None:
                report[_to_snake_plural(method_name)] = 0
            elif isinstance(result, list):
                report[_to_snake_plural(method_name)] = len(result)
            else:
                report[_to_snake_plural(method_name)] = 1
        except Exception as exc:
            report[_to_snake_plural(method_name)] = "unsupported_or_failed"
            report["errors"].append(f"{method_name} failed: {exc}")

    count_or_error("GetAudioSources")
    count_or_error("GetAudioOutputs")
    count_or_error("GetAudioOutputConfigurations")
    count_or_error("GetAudioDecoderConfigurations")

    return stream_uri, report


def _to_snake_plural(onvif_method_name: str) -> str:
    mapping = {
        "GetAudioSources": "audio_sources",
        "GetAudioOutputs": "audio_outputs",
        "GetAudioOutputConfigurations": "audio_output_configurations",
        "GetAudioDecoderConfigurations": "audio_decoder_configurations",
    }
    return mapping.get(onvif_method_name, onvif_method_name)


def ensure_rtsp_uri(stream_uri: Optional[str], creds: CameraCredentials, fallback_path: str) -> str:
    """Return an RTSP URI containing credentials when needed."""
    if not stream_uri:
        stream_uri = f"rtsp://{creds.ip}:554{fallback_path}"

    parsed = urlparse(stream_uri)
    if parsed.scheme.lower() != "rtsp":
        raise ValueError(f"Expected an rtsp:// URI, got: {_redact_uri(stream_uri)}")

    if parsed.username:
        return stream_uri

    host = parsed.hostname or creds.ip
    if parsed.port:
        host = f"{host}:{parsed.port}"

    username = quote(creds.username, safe="")
    password = quote(creds.password, safe="")
    netloc = f"{username}:{password}@{host}"
    return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))


def describe_rtsp(uri: str, require_backchannel: bool, timeout: float = 5.0) -> RtspResponse:
    """Send RTSP DESCRIBE, retrying once if Basic/Digest auth is requested."""
    response = _send_rtsp_describe(uri, require_backchannel, timeout, authorization=None)
    if response.status_code != 401:
        return response

    auth_header = response.headers.get("www-authenticate")
    if not auth_header:
        return response

    authorization = _build_authorization_header(uri, auth_header, method="DESCRIBE")
    if not authorization:
        return response

    return _send_rtsp_describe(uri, require_backchannel, timeout, authorization=authorization)


def _send_rtsp_describe(uri: str, require_backchannel: bool, timeout: float, authorization: Optional[str]) -> RtspResponse:
    parsed = urlparse(uri)
    host = parsed.hostname
    port = parsed.port or 554
    if not host:
        raise ValueError(f"Invalid RTSP URI: {_redact_uri(uri)}")

    request_uri = _rtsp_request_uri_without_credentials(parsed)
    headers = [
        f"DESCRIBE {request_uri} RTSP/1.0",
        "CSeq: 1",
        "Accept: application/sdp",
        "User-Agent: camera-management-system/audio-backchannel-probe",
    ]
    if require_backchannel:
        headers.append(f"Require: {BACKCHANNEL_REQUIRE_HEADER}")
    if authorization:
        headers.append(f"Authorization: {authorization}")
    headers.append("\r\n")
    payload = "\r\n".join(headers).encode("utf-8")

    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(payload)
        chunks = []
        while True:
            try:
                chunk = sock.recv(8192)
            except socket.timeout:
                break
            if not chunk:
                break
            chunks.append(chunk)
            raw_so_far = b"".join(chunks)
            if b"\r\n\r\n" in raw_so_far:
                header_bytes, body_bytes = raw_so_far.split(b"\r\n\r\n", 1)
                match = re.search(br"(?im)^Content-Length:\s*(\d+)", header_bytes)
                if not match or len(body_bytes) >= int(match.group(1)):
                    break

    raw = b"".join(chunks).decode("utf-8", errors="replace")
    return _parse_rtsp_response(raw)


def _rtsp_request_uri_without_credentials(parsed) -> str:
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunparse((parsed.scheme, host, parsed.path or "/", parsed.params, parsed.query, parsed.fragment))


def _parse_rtsp_response(raw: str) -> RtspResponse:
    header_text, _, body = raw.partition("\r\n\r\n")
    lines = header_text.splitlines()
    status_line = lines[0] if lines else ""
    status_code = None
    match = re.match(r"RTSP/\d\.\d\s+(\d+)", status_line)
    if match:
        status_code = int(match.group(1))

    headers: Dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip().lower()] = value.strip()

    return RtspResponse(status_code=status_code, status_line=status_line, headers=headers, body=body, raw=raw)


def _build_authorization_header(uri: str, www_authenticate: str, method: str) -> Optional[str]:
    parsed = urlparse(uri)
    username = parsed.username or ""
    password = parsed.password or ""
    request_uri = _rtsp_request_uri_without_credentials(parsed)

    if www_authenticate.lower().startswith("basic"):
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        return f"Basic {token}"

    if not www_authenticate.lower().startswith("digest"):
        return None

    values = _parse_digest_challenge(www_authenticate)
    realm = values.get("realm")
    nonce = values.get("nonce")
    if not realm or not nonce:
        return None

    qop = values.get("qop", "")
    if qop:
        qop = "auth" if "auth" in qop else qop.split(",")[0].strip()

    nc = "00000001"
    cnonce = hashlib.md5(os.urandom(8)).hexdigest()[:16]
    ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode("utf-8")).hexdigest()
    ha2 = hashlib.md5(f"{method}:{request_uri}".encode("utf-8")).hexdigest()

    if qop:
        response = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode("utf-8")).hexdigest()
    else:
        response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode("utf-8")).hexdigest()

    parts = {
        "username": username,
        "realm": realm,
        "nonce": nonce,
        "uri": request_uri,
        "response": response,
    }
    if values.get("opaque"):
        parts["opaque"] = values["opaque"]
    if qop:
        parts["qop"] = qop
        parts["nc"] = nc
        parts["cnonce"] = cnonce

    rendered = []
    for key, value in parts.items():
        if key in {"qop", "nc"}:
            rendered.append(f"{key}={value}")
        else:
            rendered.append(f'{key}="{value}"')
    return "Digest " + ", ".join(rendered)


def _parse_digest_challenge(header: str) -> Dict[str, str]:
    challenge = header[len("Digest"):].strip()
    values: Dict[str, str] = {}
    for match in re.finditer(r'(\w+)=(?:"([^"]*)"|([^,]*))', challenge):
        key = match.group(1).lower()
        value = match.group(2) if match.group(2) is not None else match.group(3).strip()
        values[key] = value
    return values


def analyze_sdp_for_backchannel(sdp: str) -> Dict[str, object]:
    lower = sdp.lower()
    audio_sections = [section for section in re.split(r"(?=^m=)", sdp, flags=re.MULTILINE) if section.lower().startswith("m=audio")]
    backchannel_sections = [
        section for section in audio_sections
        if "backchannel" in section.lower()
        or "sendonly" in section.lower()
        or "recvonly" in section.lower()
    ]

    codec_lines = []
    for line in sdp.splitlines():
        if line.lower().startswith("a=rtpmap:"):
            codec_lines.append(line.strip())

    return {
        "audio_sections": len(audio_sections),
        "backchannel_candidate_sections": len(backchannel_sections),
        "mentions_backchannel": "backchannel" in lower,
        "has_sendonly": "a=sendonly" in lower,
        "has_recvonly": "a=recvonly" in lower,
        "codec_lines": codec_lines,
    }


def print_report(creds: CameraCredentials, onvif_report: Dict[str, object], rtsp_response: Optional[RtspResponse], sdp_report: Optional[Dict[str, object]], rtsp_uri: Optional[str]) -> int:
    print("\n=== Camera Audio Backchannel Probe ===")
    print(f"Camera ID: {creds.camera_id if creds.camera_id is not None else '-'}")
    print(f"IP: {creds.ip}")
    print(f"Username: {creds.username}")
    print(f"Password: {_sanitize_secret(creds.password)}")

    print("\n--- ONVIF media report ---")
    for key in [
        "connected",
        "profiles",
        "selected_profile_token",
        "stream_uri",
        "audio_sources",
        "audio_outputs",
        "audio_output_configurations",
        "audio_decoder_configurations",
    ]:
        print(f"{key}: {onvif_report.get(key)}")

    errors = onvif_report.get("errors") or []
    if errors:
        print("ONVIF notes/errors:")
        for error in errors:
            print(f"  - {error}")

    print("\n--- RTSP backchannel DESCRIBE ---")
    if rtsp_uri:
        print(f"RTSP URI: {_redact_uri(rtsp_uri)}")
    if rtsp_response is None:
        print("RTSP result: not tested")
        return 2

    print(f"Status: {rtsp_response.status_line or rtsp_response.status_code}")
    if rtsp_response.status_code == 551:
        print("Result: camera/RTSP server explicitly says ONVIF backchannel is not supported.")
        return 1
    if rtsp_response.status_code == 401:
        print("Result: authentication failed or unsupported auth challenge.")
        return 2
    if rtsp_response.status_code and rtsp_response.status_code >= 400:
        print("Result: RTSP server returned an error; inspect camera model/vendor docs.")
        return 2

    print("\n--- SDP analysis ---")
    if sdp_report:
        for key, value in sdp_report.items():
            if key == "codec_lines":
                print("codec_lines:")
                for codec_line in value:
                    print(f"  - {codec_line}")
            else:
                print(f"{key}: {value}")

        if sdp_report.get("backchannel_candidate_sections"):
            print("\nLikely result: audio backchannel candidate detected. Push-to-Talk is worth implementing next.")
            return 0

    print("\nLikely result: no obvious audio backchannel candidate found in SDP.")
    print("The camera may still support vendor-specific talk APIs, but generic ONVIF backchannel was not detected.")
    return 1


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe a camera for ONVIF/RTSP audio backchannel support")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--camera-id", type=int, help="Camera ID from camera_credentials.db")
    source.add_argument("--ip", help="Camera IP address")
    parser.add_argument("--username", help="Camera username; required with --ip")
    parser.add_argument("--password", help="Camera password; prompted when --ip is used and omitted")
    parser.add_argument("--onvif-port", type=int, default=80, help="ONVIF HTTP port, default: 80")
    parser.add_argument("--profile-index", type=int, default=0, help="ONVIF media profile index, default: 0")
    parser.add_argument("--fallback-rtsp-path", default="/Streaming/Channels/101", help="Fallback RTSP path if ONVIF GetStreamUri fails")
    parser.add_argument("--timeout", type=float, default=5.0, help="Socket timeout in seconds")
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)

    try:
        if args.camera_id is not None:
            creds = load_camera_from_db(args.camera_id)
            creds.onvif_port = args.onvif_port
        else:
            if not args.username:
                raise RuntimeError("--username is required when using --ip")
            password = args.password if args.password is not None else getpass("Camera password: ")
            creds = CameraCredentials(None, args.ip, args.username, password, args.onvif_port)

        stream_uri, onvif_report = get_onvif_report(creds, args.profile_index)
        rtsp_uri = ensure_rtsp_uri(stream_uri, creds, args.fallback_rtsp_path)

        try:
            rtsp_response = describe_rtsp(rtsp_uri, require_backchannel=True, timeout=args.timeout)
            sdp_report = analyze_sdp_for_backchannel(rtsp_response.body) if rtsp_response.body else None
        except Exception as exc:
            print(f"RTSP probe failed: {exc}")
            rtsp_response = None
            sdp_report = None

        return print_report(creds, onvif_report, rtsp_response, sdp_report, rtsp_uri)
    except Exception as exc:
        print(f"Fatal error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
