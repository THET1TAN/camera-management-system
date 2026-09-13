"""Camera availability probes. Never opens a video session or logs private data."""
from dataclasses import dataclass, field
import base64
import ctypes
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import struct
import time
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape


@dataclass(frozen=True)
class HealthSettings:
    interval: float = 15.0
    timeout: float = 2.0
    failures: int = 2
    workers: int = 4
    discovery_retry: float = 60.0
    rediscover_after: int = 3


@dataclass(frozen=True)
class CameraTarget:
    camera_id: int
    host: str = field(repr=False)
    username: str = field(default='', repr=False)
    password: str = field(default='', repr=False)
    onvif_url: str = field(default='', repr=False)
    rtsp_url: str = field(default='', repr=False)


@dataclass(frozen=True)
class Endpoint:
    host: str = field(repr=False)
    port: int
    tls: bool = False


@dataclass(frozen=True)
class Observation:
    state: str
    detail: str


@dataclass(frozen=True)
class RTSPReply:
    code: object = None
    connected: bool = False


class Cancelled(Exception):
    pass


def check_cancel(stop):
    if stop.is_set():
        raise Cancelled()


def endpoint_from_url(url, schemes=('rtsp', 'rtsps')):
    parts = urlsplit(url)
    if parts.scheme not in schemes or not parts.hostname:
        raise ValueError('Unsupported service address')
    defaults = {'rtsp': 554, 'rtsps': 322, 'http': 80, 'https': 443}
    port = parts.port if parts.port is not None else defaults[parts.scheme]
    if not 1 <= port <= 65535 or any(c.isspace() for c in parts.hostname):
        raise ValueError('Invalid service address')
    # Deliberately retain no userinfo, path, query, profile token or password.
    return Endpoint(parts.hostname, port, parts.scheme in ('rtsps', 'https'))


def device_url(target):
    if target.onvif_url:
        endpoint_from_url(target.onvif_url, ('http', 'https'))
        if urlsplit(target.onvif_url).username is not None:
            raise ValueError('Use the saved camera credentials')
        return target.onvif_url
    host = target.host
    if ':' in host and not host.startswith('['):
        host = '[' + host + ']'
    return 'http://' + host + '/onvif/device_service'


def load_overrides(directory):
    path = Path(directory) / 'camera_health.json'
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError('Invalid camera health configuration')
    for camera_id, values in data.items():
        if not str(camera_id).isdigit() or not isinstance(values, dict):
            raise ValueError('Invalid camera health configuration')
        if set(values) - {'onvif_url', 'rtsp_url'}:
            raise ValueError('Unknown camera health setting')
        for key, url in values.items():
            endpoint_from_url(url, ('http', 'https') if key == 'onvif_url' else ('rtsp', 'rtsps'))
            if urlsplit(url).username is not None:
                raise ValueError('Use the saved camera credentials')
    return data


def _connect(endpoint, timeout):
    sock = socket.create_connection((endpoint.host, endpoint.port), timeout)
    if endpoint.tls:
        try:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=endpoint.host)
        except Exception:
            sock.close()
            raise
    return sock


def rtsp_options(endpoint, timeout, stop):
    check_cancel(stop)
    deadline = time.monotonic() + timeout
    connected = False
    try:
        with _connect(endpoint, timeout) as sock:
            connected = True
            check_cancel(stop)
            sock.settimeout(max(0.01, deadline - time.monotonic()))
            sock.sendall(b'OPTIONS * RTSP/1.0\r\nCSeq: 1\r\nUser-Agent: CameraViewer-health\r\n\r\n')
            data = bytearray()
            while b'\r\n\r\n' not in data and len(data) < 8192:
                check_cancel(stop)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return RTSPReply(connected=True)
                sock.settimeout(min(0.2, remaining))
                try:
                    chunk = sock.recv(min(1024, 8192 - len(data)))
                except socket.timeout:
                    continue
                if not chunk:
                    break
                data.extend(chunk)
            if b'\r\n\r\n' not in data:
                return RTSPReply(connected=True)
            lines = bytes(data).split(b'\r\n\r\n', 1)[0].split(b'\r\n')
            match = re.fullmatch(rb'RTSP/1\.0 ([1-5][0-9]{2})(?: [^\r\n]*)?', lines[0])
            cseq = [line.split(b':', 1)[1].strip() for line in lines[1:]
                    if line.split(b':', 1)[0].lower() == b'cseq']
            if match and cseq == [b'1']:
                return RTSPReply(int(match.group(1)), True)
    except (OSError, ValueError):
        pass
    check_cancel(stop)
    return RTSPReply(connected=connected)


def tcp_available(endpoint, timeout, stop):
    check_cancel(stop)
    try:
        # A TCP handshake is only a secondary host/service reachability signal.
        with socket.create_connection((endpoint.host, endpoint.port), timeout):
            check_cancel(stop)
            return True
    except OSError:
        check_cancel(stop)
        return False


def ping_available(host, timeout, stop):
    """Windows ICMP APIs: no shell, localized output, or child process."""
    check_cancel(stop)
    if os.name != 'nt':
        return None
    try:
        # DNS, if needed, stays inside the killable health process.
        addresses = socket.getaddrinfo(host, None, type=socket.SOCK_DGRAM)
        if not addresses:
            return None
        family, _, _, _, sockaddr = addresses[0]
        api = ctypes.WinDLL('iphlpapi', use_last_error=True)
        if family == socket.AF_INET6:
            return _ping_ipv6(api, sockaddr, timeout, stop)
        address = ipaddress.ip_address(sockaddr[0])
        api.IcmpCreateFile.restype = ctypes.c_void_p
        api.IcmpCloseHandle.argtypes = [ctypes.c_void_p]
        api.IcmpSendEcho.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                                    ctypes.c_ushort, ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.c_uint32, ctypes.c_uint32]
        api.IcmpSendEcho.restype = ctypes.c_uint32
        handle = api.IcmpCreateFile()
        if handle in (None, ctypes.c_void_p(-1).value):
            return None
        try:
            reply = ctypes.create_string_buffer(256)
            request = ctypes.create_string_buffer(b'health')
            count = api.IcmpSendEcho(handle, struct.unpack('=I', address.packed)[0],
                                     request, 6, None, reply, len(reply), int(timeout * 1000))
            check_cancel(stop)
            # The second DWORD is IP_STATUS; unreachable replies are not success.
            return bool(count and struct.unpack_from('=I', reply, 4)[0] == 0)
        finally:
            api.IcmpCloseHandle(handle)
    except (OSError, ValueError):
        return None


def _ping_ipv6(api, sockaddr, timeout, stop):
    api.Icmp6CreateFile.restype = ctypes.c_void_p
    api.IcmpCloseHandle.argtypes = [ctypes.c_void_p]
    api.Icmp6SendEcho2.argtypes = [ctypes.c_void_p] * 7 + [ctypes.c_ushort] + [ctypes.c_void_p] * 2 + [ctypes.c_uint32] * 2
    api.Icmp6SendEcho2.restype = ctypes.c_uint32
    handle = api.Icmp6CreateFile()
    if handle in (None, ctypes.c_void_p(-1).value):
        return None
    try:
        # SOCKADDR_IN6: family, port, flowinfo, 16-byte address, scope id.
        source = ctypes.create_string_buffer(struct.pack('=HHI16sI', socket.AF_INET6, 0, 0, b'\0' * 16, 0))
        destination = ctypes.create_string_buffer(struct.pack('=HHI16sI', socket.AF_INET6, 0, 0,
                                                  socket.inet_pton(socket.AF_INET6, sockaddr[0].split('%')[0]), sockaddr[3]))
        reply = ctypes.create_string_buffer(256)
        request = ctypes.create_string_buffer(b'health')
        count = api.Icmp6SendEcho2(handle, None, None, None, source, destination, request,
                                  6, None, reply, len(reply), int(timeout * 1000))
        check_cancel(stop)
        # ICMPV6_ECHO_REPLY: IPV6_ADDRESS_EX (28 bytes), IP_STATUS, RTT.
        return bool(count and struct.unpack_from('=I', reply, 28)[0] == 0)
    finally:
        api.IcmpCloseHandle(handle)


SOAP = 'http://www.w3.org/2003/05/soap-envelope'
DEVICE = 'http://www.onvif.org/ver10/device/wsdl'
MEDIA = 'http://www.onvif.org/ver10/media/wsdl'
SCHEMA = 'http://www.onvif.org/ver10/schema'
WSSE = 'http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd'
WSU = 'http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd'
TOKEN = 'http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0'


def soap_envelope(username, password, body):
    nonce = os.urandom(16)
    created = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()
    return (f'<s:Envelope xmlns:s="{SOAP}" xmlns:wsse="{WSSE}" xmlns:wsu="{WSU}">'
            '<s:Header><wsse:Security s:mustUnderstand="1"><wsse:UsernameToken>'
            f'<wsse:Username>{escape(username)}</wsse:Username>'
            f'<wsse:Password Type="{TOKEN}#PasswordDigest">{digest}</wsse:Password>'
            '<wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">'
            f'{base64.b64encode(nonce).decode()}</wsse:Nonce><wsu:Created>{created}</wsu:Created>'
            '</wsse:UsernameToken></wsse:Security></s:Header>'
            f'<s:Body>{body}</s:Body></s:Envelope>').encode()


def soap_request(url, target, action, body, timeout, stop):
    """Small SOAP requests avoid WSDL downloads and event subscriptions."""
    import requests
    from requests.auth import HTTPDigestAuth
    check_cancel(stop)
    endpoint_from_url(url, ('http', 'https'))
    if urlsplit(url).username is not None:
        raise ValueError('Invalid ONVIF endpoint')
    with requests.Session() as session:
        session.trust_env = False
        session.auth = HTTPDigestAuth(target.username, target.password)
        with session.post(url, data=soap_envelope(target.username, target.password, body),
                          headers={'Content-Type': f'application/soap+xml; charset=utf-8; action="{action}"'},
                          timeout=timeout, allow_redirects=False, stream=True) as response:
            response.raise_for_status()
            deadline = time.monotonic() + timeout
            data = bytearray()
            for chunk in response.iter_content(4096):
                check_cancel(stop)
                data.extend(chunk)
                if len(data) > 512 * 1024 or time.monotonic() > deadline:
                    raise ValueError('ONVIF response limit exceeded')
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ValueError('Unsupported ONVIF XML')
    root = ET.fromstring(data)
    if root.find(f'.//{{{SOAP}}}Fault') is not None:
        raise ValueError('ONVIF discovery refused')
    return root


def discover_rtsp(target, timeout, stop, request=soap_request):
    caps = request(device_url(target), target, DEVICE + '/GetCapabilities',
                   f'<tds:GetCapabilities xmlns:tds="{DEVICE}"><tds:Category>Media</tds:Category></tds:GetCapabilities>', timeout, stop)
    media_url = caps.findtext(f'.//{{{SCHEMA}}}Media/{{{SCHEMA}}}XAddr')
    if not media_url:
        raise ValueError('No ONVIF Media service')
    profiles = request(media_url, target, MEDIA + '/GetProfiles',
                       f'<trt:GetProfiles xmlns:trt="{MEDIA}"/>', timeout, stop)
    profile = profiles.find(f'.//{{{MEDIA}}}Profiles')
    if profile is None or not profile.get('token'):
        raise ValueError('No ONVIF media profile')
    body = (f'<trt:GetStreamUri xmlns:trt="{MEDIA}" xmlns:tt="{SCHEMA}">'
            '<trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream><tt:Transport>'
            '<tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>'
            f'<trt:ProfileToken>{escape(profile.get("token"))}</trt:ProfileToken></trt:GetStreamUri>')
    uri = request(media_url, target, MEDIA + '/GetStreamUri', body, timeout, stop)
    return endpoint_from_url(uri.findtext(f'.//{{{SCHEMA}}}Uri') or '')


class CameraProbe:
    def __init__(self, target, settings=HealthSettings(), discover=discover_rtsp,
                 options=rtsp_options, tcp=tcp_available, ping=ping_available, clock=time.monotonic):
        self.target, self.settings = target, settings
        self.discover, self.options, self.tcp, self.ping, self.clock = discover, options, tcp, ping, clock
        self.endpoint = endpoint_from_url(target.rtsp_url) if target.rtsp_url else None
        self.next_discovery = 0.0
        self.rtsp_failures = 0

    def check(self, stop):
        check_cancel(stop)
        target, settings = self.target, self.settings
        if not target.rtsp_url and self.clock() >= self.next_discovery and (
                self.endpoint is None or self.rtsp_failures >= settings.rediscover_after):
            self.next_discovery = self.clock() + settings.discovery_retry
            try:
                self.endpoint = self.discover(target, settings.timeout, stop)
            except Cancelled:
                raise
            except Exception:
                pass  # Exceptions may contain passwords or authenticated URIs.
        check_cancel(stop)
        reply = self.options(self.endpoint, settings.timeout, stop) if self.endpoint else RTSPReply()
        check_cancel(stop)
        if reply.code == 401 or (reply.code is not None and 200 <= reply.code < 300):
            self.rtsp_failures = 0
            return Observation('online', 'RTSP service available.')
        self.rtsp_failures += 1
        if reply.code is not None:
            detail = 'Limited check: RTSP OPTIONS is not supported.' if reply.code in (405, 501) else f'RTSP reports error {reply.code}.'
            return Observation('degraded', detail)
        secondary = endpoint_from_url(device_url(target), ('http', 'https'))
        if self.tcp(secondary, settings.timeout, stop):
            return Observation('degraded', 'HTTP/ONVIF port reachable; RTSP unavailable or not yet discovered.')
        check_cancel(stop)
        if self.ping(target.host, settings.timeout, stop):
            return Observation('degraded', 'Camera responds to ping; video and HTTP/ONVIF services unavailable.')
        check_cancel(stop)
        if reply.connected:
            return Observation('degraded', 'RTSP port accepts connections but gives no valid OPTIONS response.')
        return Observation('offline', 'RTSP, HTTP/ONVIF and fallback ping did not establish availability.')


class HealthState:
    def __init__(self, failures=2):
        self.threshold = failures
        self.failed_checks = 0
        self.current = Observation('unknown', 'First verification in progress.')

    def apply(self, observation):
        if observation.state == 'offline':
            self.failed_checks += 1
            if self.failed_checks < self.threshold:
                return Observation(self.current.state, 'No response; confirming availability on the next check.')
        else:
            self.failed_checks = 0
        self.current = observation
        return observation
