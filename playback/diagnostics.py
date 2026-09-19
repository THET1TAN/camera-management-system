"""Small, rotating diagnostics with an explicit public-field allowlist."""
import json
import logging
from logging.handlers import RotatingFileHandler
import time
import re
from urllib.parse import urlsplit

FIELDS = {'camera_id', 'generation', 'elapsed', 'received', 'count', 'complete', 'reason',
          'state', 'position', 'rate', 'decoded', 'displayed', 'python', 'python_vlc', 'libvlc',
          'backend', 'stage', 'method', 'endpoint', 'http_status', 'content_type',
          'xml_root', 'xml_namespace', 'application_code', 'application_subcode', 'application_status', 'requested_track',
          'returned_track', 'page_position', 'announced_count', 'track_enabled', 'outcome',
          'python_executable_matches_parent'}

ENDPOINTS = {'/ISAPI/System/deviceInfo', '/ISAPI/ContentMgmt/record/tracks',
             '/ISAPI/ContentMgmt/search', '/ISAPI/ContentMgmt/download',
             '/cgi-bin/getuid', '/cgi-bin/get_record_query', '/onvif/device_service'}
APPLICATION_CODES = {'true', 'false', 'OK', 'MORE', 'NO MATCHES', 'badXmlContent',
                     'notSupport', 'invalidOperation', 'Invalid Operation', 'No Permission',
                     'unauthorized', 'deviceBusy', 'parameterError', 'ok', 'error'}


def public_code(value):
    value = str(value or '').strip()
    return value if value in APPLICATION_CODES or re.fullmatch(r'\d{1,6}', value) else 'unrecognized-code'


def safe_fields(values):
    result = {}
    for key, value in values.items():
        if key not in FIELDS or not isinstance(value, (str, int, float, bool)):
            continue
        if isinstance(value, str):
            value = value[:200].replace('\r', '').replace('\n', '')
            if key == 'endpoint' and value not in ENDPOINTS:
                value = '/playback/<recording>' if value.startswith('/playback/') else '<unrecognized>'
            if key in ('requested_track', 'returned_track') and not re.fullmatch(r'\d{1,10}', value):
                value = '<unrecognized>'
            if key == 'xml_root' and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]{0,79}', value):
                value = '<unrecognized>'
            if key == 'xml_namespace' and value:
                try:
                    parts = urlsplit(value)
                    valid = parts.scheme in ('http', 'https', 'urn') and not (parts.username or parts.query or parts.fragment)
                except ValueError:
                    valid = False
                if not valid:
                    value = '<unrecognized>'
            if key == 'content_type' and not re.fullmatch(r'[a-z0-9.+-]+/[a-z0-9.+-]+', value):
                value = '<unrecognized>'
            if key in ('application_code', 'application_subcode', 'application_status'):
                value = public_code(value)
        result[key] = value
    return result


class ProtocolTrace:
    """Only structural facts, never bodies, headers, URLs, identities or UID text."""
    def __init__(self, camera, backend, emit=None):
        self.camera_id, self.backend, self.emit = camera.camera_id, backend, emit
        self.secrets = [v for v in (camera.host, camera.username, camera.password) if v]
        self.context = {}

    def protect(self, value):
        if value:
            self.secrets.append(value)

    def begin(self, stage, method='', endpoint='', **values):
        self.context = dict(camera_id=self.camera_id, backend=self.backend, stage=stage, **values)
        if method:
            self.context['method'] = method
        if endpoint:
            self.context['endpoint'] = endpoint

    def note(self, **values):
        self.context.update(safe_fields(values))

    def snapshot(self, **values):
        result = safe_fields(dict(self.context, **values))
        for key, value in result.items():
            if isinstance(value, str):
                for secret in self.secrets:
                    # Exact values are redacted even for one-character passwords.
                    if value == secret or (len(secret) >= 3 and secret in value):
                        value = '<redacted>'
                result[key] = value
        return result

    def report(self, **values):
        result = self.snapshot(**values)
        if self.emit:
            self.emit(result)
        return result

    def reject(self, error):
        detail = self.report(outcome='rejected', reason=error.code)
        if not error.details:
            error.details = (detail,)
        return error


class Diagnostics:
    def __init__(self, directory):
        self.logger = logging.Logger('archive-playback')
        self.handler = RotatingFileHandler(directory/'events.jsonl',maxBytes=512*1024,backupCount=2,encoding='utf-8')
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.INFO)

    def event(self, event, **values):
        safe = safe_fields(values)
        self.logger.info(json.dumps(dict(event=event,monotonic=time.monotonic(),**safe),ensure_ascii=False))

    def close(self):
        self.handler.close()
        self.logger.removeHandler(self.handler)
