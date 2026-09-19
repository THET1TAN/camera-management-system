"""Read-only camera HTTP: bounded bodies, no redirects/proxies, no private logs."""
import time
from urllib.parse import urlsplit
import requests
from requests.auth import HTTPDigestAuth
from xml.etree import ElementTree as ET

from .config import base_url
from .model import PlaybackError, check_cancel


def xml_body(data, roots=(), trace=None):
    if len(data) > 4*1024*1024:
        raise PlaybackError('response-limit')
    lowered = data.lower()
    if b'<html' in lowered[:1024] or b'<!doctype html' in lowered[:1024]:
        if trace:
            trace.note(xml_root='html', xml_namespace='')
        reason = 'html-error-page' if b'f404.jpg' in lowered else 'html-login-page' if b'password' in lowered else 'html-response'
        raise PlaybackError(reason)
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise PlaybackError('xml-entities-forbidden')
    try:
        root = ET.fromstring(data)
        namespace = root.tag[1:].split('}', 1)[0] if root.tag.startswith('{') else ''
        if trace:
            trace.note(xml_root=root.tag.rsplit('}', 1)[-1], xml_namespace=namespace)
        # Namespace-independent parsing; validate the actual root first.
        for node in root.iter():
            node.tag = node.tag.rsplit('}', 1)[-1]
        if trace:
            code = root.findtext('statusCode') or root.findtext('responseStatusStrg') or root.findtext('subStatusCode')
            if code is not None:
                trace.note(application_code=code)
            if root.findtext('subStatusCode'):
                trace.note(application_subcode=root.findtext('subStatusCode'))
            if root.findtext('responseStatus'):
                trace.note(application_status=root.findtext('responseStatus'))
        if root.tag == 'Fault' or root.find('.//Fault') is not None:
            raise PlaybackError('soap-fault')
        if roots and root.tag not in roots:
            raise PlaybackError('camera-application-error' if root.tag in ('ResponseStatus', 'error', 'Error') else 'xml-root-unexpected')
        return root
    except ET.ParseError:
        raise PlaybackError('xml-malformed') from None


class Transport:
    def __init__(self, camera, trace=None):
        self.camera = camera
        self.trace = trace
        self.base = base_url(camera)
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.auth = HTTPDigestAuth(camera.username, camera.password)
        self.session.headers.update({'Accept-Encoding': 'identity', 'User-Agent': 'CameraPlayback/0.2.11-dev'})

    def close(self):
        self.session.close()

    def request(self, method, path, cancel, *, params=None, data=None):
        check_cancel(cancel)
        if not path.startswith('/') or path.startswith('//') or '\\' in path or urlsplit(path).netloc:
            raise PlaybackError('endpoint-invalid')
        try:
            response = self.session.request(method, self.base + path, params=params, data=data,
                headers={'Content-Type': 'application/xml'} if data else {},
                timeout=(3, 3), stream=True, allow_redirects=False)
            if self.trace:
                self.trace.note(http_status=response.status_code,
                    content_type=response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower(), received=0)
            if response.status_code in (401, 403):
                response.close()
                raise PlaybackError('authentication-failed')
            if response.status_code != 200:
                code = 'unsupported' if response.status_code in (404, 405, 501) else 'http-error'
                response.close()
                raise PlaybackError(code)
            return response
        except requests.RequestException:
            raise PlaybackError('temporarily-unreachable') from None

    def read(self, method, path, cancel, **kwargs):
        with self.request(method, path, cancel, **kwargs) as response:
            body = bytearray()
            deadline = time.monotonic()+20
            try:
                for chunk in response.iter_content(65536):
                    check_cancel(cancel)
                    body.extend(chunk)
                    if len(body) > 4*1024*1024 or time.monotonic() > deadline:
                        raise PlaybackError('response-limit')
            except requests.RequestException:
                raise PlaybackError('temporarily-unreachable') from None
            finally:
                if self.trace:
                    self.trace.note(received=len(body))
            return bytes(body)

    def download(self, method, path, target, cancel, limit, progress, **kwargs):
        """Never append an HTTP 200 to an earlier partial file."""
        with self.request(method, path, cancel, **kwargs) as response:
            content_type = response.headers.get('Content-Type', '').lower()
            if any(s in content_type for s in ('text/', 'xml', 'json')):
                raise PlaybackError('invalid-media-response')
            try:
                expected = int(response.headers.get('Content-Length', '0'))
            except ValueError:
                raise PlaybackError('invalid-response') from None
            if expected < 0 or expected > limit:
                raise PlaybackError('archive-too-large')
            received = 0
            began = time.monotonic()
            try:
                with target.open('wb') as handle:
                    for chunk in response.iter_content(256*1024):
                        check_cancel(cancel)
                        if not chunk:
                            continue
                        if received == 0 and chunk.lstrip()[:32].lower().startswith((b'<!doctype', b'<html', b'<?xml', b'{')):
                            raise PlaybackError('invalid-media-response')
                        received += len(chunk)
                        if self.trace:
                            self.trace.note(received=received)
                        if received > limit or time.monotonic()-began > 1800:
                            raise PlaybackError('archive-too-large')
                        handle.write(chunk)
                        handle.flush()  # A progressive reader only sees received bytes.
                        progress(received, expected, max(.001, time.monotonic()-began))
                if not received or (expected and received != expected):
                    raise PlaybackError('incomplete-download')
                return received
            except requests.RequestException:
                raise PlaybackError('incomplete-download') from None
