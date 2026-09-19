"""Two observed read-only protocols; a 200 status is never capability evidence."""
from datetime import datetime, timedelta
from hashlib import sha256
import json
import re
import time
from urllib.parse import parse_qs, quote, unquote, urlsplit
from uuid import uuid4
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

from .model import Recording, SearchResult, PlaybackError, check_cancel, iso_utc, parse_time
from .transport import Transport, xml_body


def fingerprint(*values):
    return sha256('|'.join(str(v) for v in values).encode()).hexdigest()


class Backend:
    name = ''

    def __init__(self, camera, transport=None):
        self.camera = camera
        self.http = transport or Transport(camera)
        self.device = ''
        self.tracks = ()

    def close(self):
        self.http.close()

    def list_available_days(self, start, end, cancel):
        return self.list_recordings(start, end, cancel)

    def get_replay_uri(self, recording, stamp):
        # The RTSP-to-cache bridge is deliberately not advertised as qualified.
        raise PlaybackError('replay-bridge-unavailable')


class IsapiBackend(Backend):
    name = 'isapi'

    def probe(self, cancel):
        info = xml_body(self.http.read('GET', '/ISAPI/System/deviceInfo', cancel), ('DeviceInfo',))
        identity = info.findtext('serialNumber') or info.findtext('macAddress') or self.camera.revision
        if not identity:
            raise PlaybackError('device-identity-required')
        self.device = fingerprint(self.name, identity, info.findtext('model'),
                                  info.findtext('firmwareVersion'), self.camera.revision)
        root = xml_body(self.http.read('GET', '/ISAPI/ContentMgmt/record/tracks', cancel), ('TrackList',))
        tracks = []
        for node in root.findall('.//Track'):
            value = node.findtext('id') or node.findtext('trackID')
            if value and re.fullmatch(r'\d{1,10}', value) and value not in tracks:
                tracks.append(value)
        if not tracks:
            raise PlaybackError('no-recording-track')
        self.tracks = tuple(tracks)
        if self.camera.track and self.camera.track not in tracks:
            raise PlaybackError('track-unavailable')
        return self

    def list_recordings(self, start, end, cancel):
        records, seen = [], set()
        observed = time.time()
        # Every discovered track is searched unless the user explicitly filters.
        for track in ((self.camera.track,) if self.camera.track else self.tracks):
            search_id, position = str(uuid4()), 0
            for page in range(200):
                check_cancel(cancel)
                body = ET.Element('CMSearchDescription', version='2.0', xmlns='http://www.std-cgi.com/ver20/XMLSchema')
                ET.SubElement(body, 'searchID').text = search_id
                ET.SubElement(ET.SubElement(body, 'trackList'), 'trackID').text = track
                span = ET.SubElement(ET.SubElement(body, 'timeSpanList'), 'timeSpan')
                ET.SubElement(span, 'startTime').text = iso_utc(start-self.camera.time_shift)
                ET.SubElement(span, 'endTime').text = iso_utc(end-self.camera.time_shift)
                ET.SubElement(body, 'maxResults').text = '50'
                ET.SubElement(body, 'searchResultPostion').text = str(position)
                ET.SubElement(ET.SubElement(body, 'metadataList'), 'metadataDescriptor').text = '//recordType.meta.std-cgi.com'
                root = xml_body(self.http.read('POST', '/ISAPI/ContentMgmt/search', cancel,
                                               data=ET.tostring(body)), ('CMSearchResult',))
                status = (root.findtext('responseStatusStrg') or '').upper()
                if (root.findtext('responseStatus') or '').lower() not in ('true', '1') or status not in ('OK', 'MORE', 'NO MATCHES'):
                    raise PlaybackError('search-rejected')
                items = root.findall('.//searchMatchItem')
                try:
                    count = int(root.findtext('numOfMatches', '-1'))
                except ValueError:
                    raise PlaybackError('invalid-response') from None
                if count != len(items):
                    return SearchResult(tuple(records), False, 'pagination-count', observed)
                added = 0
                for item in items:
                    uri = item.findtext('.//playbackURI', '')
                    if len(uri)>4096:
                        raise PlaybackError('response-limit')
                    parts = urlsplit(uri)
                    # Userinfo is refused instead of being retained in an index or export.
                    if parts.scheme not in ('rtsp', 'rtsps') or not parts.hostname or parts.username is not None:
                        raise PlaybackError('invalid-recording-uri')
                    raw_start, raw_end = item.findtext('.//startTime', ''), item.findtext('.//endTime', '')
                    a = parse_time(raw_start, self.camera.zone, self.camera.time_shift)
                    b = parse_time(raw_end, self.camera.zone, self.camera.time_shift)
                    found_track = item.findtext('trackID', track)
                    if found_track != track:
                        raise PlaybackError('invalid-response')
                    try:
                        size = int(parse_qs(parts.query).get('size', ['0'])[0])
                    except ValueError:
                        raise PlaybackError('invalid-size') from None
                    record = Recording(self.camera.camera_id, self.device, self.name, track,
                        uri, a, b, size, uri, raw_start, raw_end, observed)
                    if record.key not in seen:
                        seen.add(record.key)
                        added += 1
                        if a < end and b > start:
                            records.append(record)
                if status != 'MORE':
                    break
                if not items or not added:
                    return SearchResult(tuple(records), False, 'pagination-repeated', observed)
                position += len(items)
            else:
                return SearchResult(tuple(records), False, 'pagination-limit', observed)
        return SearchResult(tuple(records), True, '', observed)

    def download(self, recording, target, cancel, limit, progress):
        if recording.device != self.device:
            raise PlaybackError('device-changed')
        body = ET.Element('downloadRequest')
        ET.SubElement(body, 'playbackURI').text = recording.locator
        return self.http.download('GET', '/ISAPI/ContentMgmt/download', target, cancel,
                                  limit, progress, data=ET.tostring(body))


def cgi_items(data):
    """Accept the observed item records in XML or JSON; refuse HTML/login bodies."""
    if data.lstrip().startswith((b'{', b'[')):
        try:
            root = json.loads(data)
            items = root if isinstance(root, list) else root.get('items', root.get('item', []))
            if isinstance(items, dict):
                items = [items]
            if not isinstance(items, list) or any(not isinstance(i, dict) for i in items):
                raise ValueError
            if isinstance(root, dict) and not any(k in root for k in ('items', 'item')):
                raise ValueError
            more = isinstance(root, dict) and bool(root.get('has_more') or root.get('more'))
            return items, more
        except (ValueError, TypeError):
            raise PlaybackError('invalid-response') from None
    root = xml_body(data)
    if root.tag.lower() in ('html', 'uid', 'fault', 'error'):
        raise PlaybackError('invalid-response')
    nodes = [root] if root.tag in ('item', 'items') else root.findall('.//item')
    if not nodes and root.tag == 'RecordQueryInfo':
        nodes = root.findall('items')
    # Do not interpret an arbitrary empty XML root as a successful empty search.
    if not nodes and root.tag not in ('RecordQueryInfo', 'records', 'record', 'record_query', 'record_list', 'list', 'result'):
        raise PlaybackError('invalid-response')
    items = [dict(node.attrib, **{child.tag: child.text or '' for child in node}) for node in nodes]
    more = (root.findtext('more') or root.findtext('has_more') or '').lower() in ('true', '1')
    return items, more


class VideoLinkBackend(Backend):
    name = 'videolink'

    def login(self, cancel):
        root = xml_body(self.http.read('GET', '/cgi-bin/getuid', cancel,
            params={'username': self.camera.username, 'password': self.camera.password}), ('uid',))
        uid = (root.text or root.get('value') or root.findtext('value') or '').strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', uid) or uid.lower() in ('0', '-1', 'error', 'false', 'null'):
            raise PlaybackError('authentication-failed')
        self.uid = uid

    def probe(self, cancel):
        self.login(cancel)
        # getuid is a session identifier, NOT a device identity. Obtain a stable
        # ONVIF serial if possible, otherwise require an explicit local revision.
        identity = self.camera.revision
        if not identity:
            try:
                from camera_health import CameraTarget, DEVICE, soap_request
                endpoint = self.http.base + '/onvif/device_service'
                target = CameraTarget(self.camera.camera_id, self.camera.host,
                    self.camera.username, self.camera.password, onvif_url=endpoint)
                root = soap_request(endpoint, target, DEVICE+'/GetDeviceInformation',
                    f'<tds:GetDeviceInformation xmlns:tds="{DEVICE}"/>', 3., cancel)
                values = {node.tag.rsplit('}', 1)[-1]: node.text for node in root.iter()}
                if values.get('SerialNumber'):
                    identity = '|'.join(values.get(k) or '' for k in ('Manufacturer', 'Model', 'SerialNumber', 'FirmwareVersion'))
            except Exception:
                check_cancel(cancel)
        if not identity:
            raise PlaybackError('device-identity-required')
        self.device = fingerprint(self.name, identity, self.camera.revision)
        return self

    def _day(self, day, cancel):
        for attempt in range(2):
            try:
                return cgi_items(self.http.read('GET', '/cgi-bin/get_record_query', cancel,
                    params={'year': day.year, 'month': day.month, 'day': day.day,
                            'stream': -1, 'record_mode': -1, 'media_type': 3, 'uid': self.uid}))
            except PlaybackError as exc:
                if attempt or exc.code not in ('authentication-failed', 'invalid-response'):
                    raise
                self.login(cancel)

    def list_recordings(self, start, end, cancel):
        if not self.camera.zone:
            raise PlaybackError('timezone-required')
        zone = ZoneInfo(self.camera.zone)
        first = datetime.fromtimestamp(start-self.camera.time_shift, zone).date()-timedelta(days=1)
        last = datetime.fromtimestamp(end-1-self.camera.time_shift, zone).date()
        records, seen = [], set()
        observed = time.time()
        incomplete = False
        for n in range((last-first).days+1):
            check_cancel(cancel)
            items, more = self._day(first+timedelta(days=n), cancel)
            incomplete |= more or len(items) >= 10000
            if len(items) > 10000:
                items = items[:10000]
            for item in items:
                # A media_type explicitly describing a still image is not a video badge.
                if str(item.get('media_type', '3')) not in ('3', 'video'):
                    continue
                path = str(item.get('filepath', ''))
                if len(path)>2048:
                    raise PlaybackError('response-limit')
                decoded = unquote(path)
                if (not decoded.startswith('/') or any(v in decoded for v in ('\\', '\x00', '?', '#'))
                        or '..' in decoded.split('/') or urlsplit(decoded).netloc):
                    raise PlaybackError('invalid-recording-uri')
                raw_start = str(item.get('start_time', ''))
                raw_end = str(item.get('end_time', ''))
                a = parse_time(raw_start, zone, self.camera.time_shift)
                b = parse_time(raw_end, zone, self.camera.time_shift) if raw_end else None
                track = str(item.get('stream_index', 'unknown'))[:32]
                if self.camera.track and track != self.camera.track:
                    continue
                try:
                    size = int(item.get('filesize', 0))
                except (ValueError, TypeError):
                    raise PlaybackError('invalid-size') from None
                record = Recording(self.camera.camera_id, self.device, self.name, track,
                    path, a, b, size, '/playback/'+quote(decoded.lstrip('/'), safe='/'),
                    raw_start, raw_end, observed)
                if record.key not in seen:
                    seen.add(record.key)
                    if a < end and (b is None or b > start):
                        records.append(record)
        self.tracks = tuple(sorted({r.track for r in records}))
        # This firmware API has no established pagination/exhaustion contract.
        # A list, even empty, is not proof of complete day coverage.
        return SearchResult(tuple(records), False,
            'cgi-result-limit' if incomplete else 'cgi-coverage-unconfirmed', observed)

    def download(self, recording, target, cancel, limit, progress):
        if recording.device != self.device:
            raise PlaybackError('device-changed')
        for attempt in range(2):
            try:
                return self.http.download('GET', recording.locator, target, cancel,
                    limit, progress, params={'uid': self.uid})
            except PlaybackError as exc:
                if attempt or exc.code not in ('authentication-failed', 'invalid-media-response'):
                    raise
                self.login(cancel)


def connect(camera, cancel):
    candidates = {'isapi': IsapiBackend, 'videolink': VideoLinkBackend}
    names = (camera.backend,) if camera.backend != 'auto' else ('isapi', 'videolink')
    errors = []
    for name in names:
        backend = candidates[name](camera)
        try:
            return backend.probe(cancel)
        except PlaybackError as exc:
            backend.close()
            errors.append(exc.code)
            check_cancel(cancel)
    # Retain actionable failures rather than permanently caching 'unsupported'.
    for reason in ('device-identity-required', 'authentication-failed', 'temporarily-unreachable'):
        if reason in errors:
            raise PlaybackError(reason)
    raise PlaybackError(errors[-1] if errors else 'unsupported')
