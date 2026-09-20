"""User-run regressions; HTTP is fake and all private files are temporary."""
from contextlib import closing
from dataclasses import replace
from dataclasses import asdict
from datetime import date
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from playback.backends import connect
from playback.config import Settings, load_cameras, load_settings
from playback.controller import Controller
from playback.diagnostics import ProtocolTrace
from playback.model import Camera, PlaybackError, parse_time
from playback.transport import xml_body
from playback.remote import worker_main
from tools.diagnose_playback import environment_snapshot

FIXTURES = Path(__file__).parent/'fixtures'/'playback'
START = parse_time('2026-09-18T12:00:00Z')
CAMERA = Camera(42, '192.0.2.99', 'synthetic-user', 'synthetic-password', zone='America/Toronto')
DEVICE = '/ISAPI/System/deviceInfo'
TRACKS = '/ISAPI/ContentMgmt/record/tracks'
SEARCH = '/ISAPI/ContentMgmt/search'
UID = '/cgi-bin/getuid'
QUERY = '/cgi-bin/get_record_query'
REJECTION = (b'<ResponseStatus xmlns="http://www.std-cgi.com/ver20/XMLSchema">'
             b'<statusCode>4</statusCode><subStatusCode>badXmlContent</subStatusCode></ResponseStatus>')
EMPTY = (b'<CMSearchResult xmlns="http://www.std-cgi.com/ver20/XMLSchema">'
         b'<responseStatus>true</responseStatus><responseStatusStrg>NO MATCHES</responseStatusStrg>'
         b'<numOfMatches>0</numOfMatches><matchList/></CMSearchResult>')


def fixture(name):
    return (FIXTURES/name).read_bytes()


class Response:
    def __init__(self, body, status=200, content_type='application/xml'):
        self.body, self.status_code = body, status
        self.headers = {'Content-Type': content_type, 'Content-Length': str(len(body))}

    def iter_content(self, _size):
        yield self.body

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class Session:
    def __init__(self, routes):
        self.routes, self.calls, self.headers, self.closed = routes, [], {}, False

    def request(self, method, url, **kwargs):
        endpoint = urlsplit(url).path
        self.calls.append((method, endpoint, kwargs))
        result = self.routes[endpoint].pop(0)
        return result if isinstance(result, Response) else Response(result)

    def close(self):
        self.closed = True


def isapi_session(searches):
    return Session({DEVICE: [fixture('isapi-device.xml')], TRACKS: [fixture('isapi-tracks.xml')], SEARCH: searches})


class DiscoveryAndSearchTests(unittest.TestCase):
    def test_real_structure_discovery_pagination_selection_and_download_contract(self):
        session = isapi_session([fixture('isapi-search-page-0.xml'), fixture('isapi-search-page-1.xml')])
        session.routes['/ISAPI/ContentMgmt/download'] = [Response(b'synthetic-source-bytes', content_type='application/octet-stream')]
        events = []
        with patch('playback.transport.requests.Session', return_value=session):
            backend = connect(replace(CAMERA, backend='isapi', track='101'), threading.Event(), events.append)
            self.assertEqual(backend.tracks, ('101', '103'))
            self.assertEqual(backend.track_info, ({'track': '101', 'enabled': 'true'}, {'track': '103', 'enabled': 'true'}))
            result = backend.list_recordings(START, START+600, threading.Event())
            self.assertTrue(result.complete)
            self.assertEqual(len(result.records), 3)
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory)/'original.part'
                backend.download(result.records[0], target, threading.Event(), 1024, Mock())
                self.assertEqual(target.read_bytes(), b'synthetic-source-bytes')
            backend.close()
        search_xml = [xml_body(options['data']) for _, endpoint, options in session.calls if endpoint == SEARCH]
        self.assertEqual([root.findtext('searchResultPostion') for root in search_xml], ['0', '2'])
        self.assertEqual(search_xml[0].findtext('searchID'), search_xml[1].findtext('searchID'))
        self.assertEqual({root.findtext('.//trackID') for root in search_xml}, {'101'})
        self.assertEqual(search_xml[0].findtext('.//metadataDescriptor'), '//recordType.meta.std-cgi.com')
        method, endpoint, options = session.calls[-1]
        self.assertEqual((method, endpoint), ('GET', '/ISAPI/ContentMgmt/download'))
        self.assertEqual(xml_body(options['data']).findtext('playbackURI'), result.records[0].locator)
        self.assertNotIn(b'&amp;amp;', options['data'])
        self.assertTrue(any(e.get('xml_namespace') == 'http://www.std-cgi.com/ver20/XMLSchema' for e in events))
        self.assertTrue(any(e.get('http_status') == 200 and e.get('received', 0) > 0 for e in events))
        self.assertTrue(session.closed)

    def test_secondary_rejection_retains_primary_records_and_partial_cause(self):
        session = isapi_session([fixture('isapi-search-page-0.xml'), fixture('isapi-search-page-1.xml'), REJECTION])
        with patch('playback.transport.requests.Session', return_value=session):
            backend = connect(replace(CAMERA, backend='isapi'), threading.Event())
            result = backend.list_recordings(START, START+600, threading.Event())
            backend.close()
        self.assertEqual(len(result.records), 3)
        self.assertEqual({r.track for r in result.records}, {'101'})
        self.assertFalse(result.complete)
        self.assertEqual(result.reason, 'tracks-partial')
        detail = result.failures[0]
        self.assertEqual((detail['stage'], detail['requested_track']), ('search', '103'))
        self.assertEqual(detail['reason'], 'camera-application-error')
        self.assertEqual(detail['application_code'], '4')
        self.assertEqual(detail['application_subcode'], 'badXmlContent')

    def test_disabled_track_is_reported_without_assuming_no_historical_archives(self):
        session = isapi_session([EMPTY])
        session.routes[TRACKS] = [fixture('isapi-tracks.xml').replace(b'<Enable>true</Enable>', b'<Enable>false</Enable>')]
        with patch('playback.transport.requests.Session', return_value=session):
            backend = connect(replace(CAMERA, backend='isapi', track='101'), threading.Event())
            self.assertEqual(backend.track_info[0]['enabled'], 'false')
            self.assertIn('101', backend.tracks)
            result = backend.list_recordings(START, START+60, threading.Event())
            backend.close()
        self.assertTrue(result.complete)
        self.assertTrue(any(endpoint == SEARCH for _, endpoint, _ in session.calls))

    def test_mismatched_returned_track_is_diagnosed_not_silently_accepted(self):
        wrong = fixture('isapi-search-page-0.xml').replace(b'<trackID>101</trackID>', b'<trackID>103</trackID>')
        session = isapi_session([wrong])
        with patch('playback.transport.requests.Session', return_value=session):
            backend = connect(replace(CAMERA, backend='isapi', track='101'), threading.Event())
            with self.assertRaises(PlaybackError) as caught:
                backend.list_recordings(START, START+600, threading.Event())
            backend.close()
        self.assertEqual(caught.exception.code, 'track-mismatch')
        self.assertEqual(caught.exception.details[0]['requested_track'], '101')
        self.assertEqual(caught.exception.details[0]['returned_track'], '103')

    def test_valid_empty_and_invalid_search_are_distinct(self):
        for body, expected in ((EMPTY, None), (b'<Unknown/>', 'xml-root-unexpected')):
            with self.subTest(expected=expected):
                session = isapi_session([body])
                with patch('playback.transport.requests.Session', return_value=session):
                    backend = connect(replace(CAMERA, backend='isapi', track='101'), threading.Event())
                    if expected:
                        with self.assertRaisesRegex(PlaybackError, expected):
                            backend.list_recordings(START, START+60, threading.Event())
                    else:
                        result = backend.list_recordings(START, START+60, threading.Event())
                        self.assertTrue(result.complete)
                        self.assertEqual(result.records, ())
                    backend.close()

    def test_auto_failure_preserves_both_stages_and_html_200_is_not_support(self):
        first = Session({DEVICE: [Response(b'<html><img src="f404.jpg"></html>', content_type='text/html')]})
        second = Session({UID: [b'<Unknown/>']})
        events = []
        with patch('playback.transport.requests.Session', side_effect=[first, second]):
            with self.assertRaises(PlaybackError) as caught:
                connect(CAMERA, threading.Event(), events.append)
        self.assertEqual(caught.exception.code, 'backend-detection-failed')
        self.assertEqual([(d['backend'], d['stage'], d['reason']) for d in caught.exception.details],
            [('isapi', 'deviceInfo', 'html-error-page'), ('videolink', 'getuid', 'xml-root-unexpected')])
        self.assertEqual(caught.exception.details[0]['http_status'], 200)
        self.assertTrue(first.closed and second.closed)

    def test_auto_success_keeps_failed_attempt_and_cgi_zone_can_be_confirmed(self):
        first = Session({DEVICE: [Response(b'<html><img src="f404.jpg"></html>', content_type='text/html')]})
        second = Session({UID: [fixture('cgi-uid.xml')],
                          QUERY: [fixture('cgi-record-query.xml'), fixture('cgi-record-query.xml')]})
        events = []
        with patch('playback.transport.requests.Session', side_effect=[first, second]):
            backend = connect(replace(CAMERA, zone='', revision='synthetic-device'), threading.Event(), events.append)
            self.assertEqual(backend.name, 'videolink')
            self.assertEqual(backend.attempts[0]['reason'], 'html-error-page')
            with self.assertRaisesRegex(PlaybackError, 'timezone-required'):
                backend.list_recordings(START, START+600, threading.Event())
            self.assertFalse(any(endpoint == QUERY for _, endpoint, _ in second.calls))
            backend.camera = replace(backend.camera, zone='America/Toronto')
            result = backend.list_recordings(START, START+600, threading.Event())
            backend.close()
        self.assertEqual(len(result.records), 2)
        self.assertEqual(result.records[0].start, START)
        self.assertEqual({r.camera_id for r in result.records}, {42})  # NOT address suffix 99.
        self.assertFalse(result.complete)
        query = next(options['params'] for _, endpoint, options in second.calls if endpoint == QUERY)
        self.assertEqual((query['stream'], query['record_mode'], query['media_type']), (-1, -1, 3))
        serialized = json.dumps(events)
        for private in ('synthetic-user', 'synthetic-password', 'synthetic-session', '192.0.2.99', '/mnt/'):
            self.assertNotIn(private, serialized)


class EnvironmentAndConfigurationTests(unittest.TestCase):
    def test_environment_mode_needs_no_optional_dependency_import(self):
        with patch('tools.diagnose_playback.importlib.util.find_spec', return_value=None):
            result = environment_snapshot()
        self.assertFalse(result['dependencies']['cryptography']['available_on_path'])
        self.assertTrue(result['executable'])

    def test_credentials_map_configuration_by_database_id_and_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = Fernet.generate_key()
            cipher = Fernet(key)
            (root/'.camera_encryption.key').write_bytes(key)
            # Commit the fixture, then release its Windows file handle before cleanup.
            with closing(sqlite3.connect(root/'camera_credentials.db')) as db, db:
                db.execute('CREATE TABLE cameras (id INTEGER, ip BLOB, username BLOB, password BLOB)')
                db.execute('INSERT INTO cameras VALUES (42,?,?,?)',
                           tuple(cipher.encrypt(value.encode()) for value in ('192.0.2.99', 'test', 'test-secret')))
            before = (root/'camera_credentials.db').read_bytes()
            with patch.dict('os.environ', {}, clear=True):
                cameras, _ = load_cameras(Settings(cameras={'42': {'zone': 'America/Toronto'}}), root)
            self.assertEqual((cameras[0].camera_id, cameras[0].zone), (42, 'America/Toronto'))
            self.assertEqual((root/'camera_credentials.db').read_bytes(), before)
            self.assertEqual((root/'.camera_encryption.key').read_bytes(), key)

    def test_missing_key_is_not_created_by_diagnostics_or_playback(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {}, clear=True):
            with self.assertRaisesRegex(PlaybackError, 'credentials-unavailable'):
                load_cameras(Settings(), Path(directory))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_apply_reloads_cameras_and_requeries_only_selected_day_without_reopening(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller.__new__(Controller)
            controller.root = Path(directory)
            controller.exporter = None
            controller.stop_event = threading.Event()
            controller.index_idle = threading.Event()
            controller.index_idle.set()
            controller.store = Mock()
            controller.month = Mock()
            settings = Settings(cameras={'42': {'zone': 'America/Toronto'}, '7': {'zone': 'UTC'}})
            day = date(2026, 9, 18)
            with patch('playback.controller.load_cameras', return_value=((CAMERA,), Mock())):
                controller._apply_configuration((settings, 42, day))
            self.assertEqual(load_settings(controller.root).cameras, settings.cameras)
            self.assertEqual(controller.cameras[0].camera_id, 42)
            controller.store.invalidate_searches.assert_called_once_with(42)
            controller.month.assert_called_once_with(2026, 9, (42,), force=True, day=day)
            self.assertEqual(controller.configuration_status, 'complete')

    def test_trace_never_exports_extra_sensitive_fields_or_download_path(self):
        trace = ProtocolTrace(CAMERA, 'videolink')
        trace.protect('synthetic-session')
        trace.begin('download', 'GET', '/playback/private-recording.mp4')
        trace.note(password='synthetic-password', uid='synthetic-session', cookie='private',
                   xml='private', authorization='private', application_code='synthetic-session')
        result = trace.snapshot()
        self.assertEqual(result['endpoint'], '/playback/<recording>')
        for private in ('private-recording', 'synthetic-password', 'synthetic-session', 'cookie', 'authorization'):
            self.assertNotIn(private, json.dumps(result))

    def test_camera_worker_preserves_detection_failure_context_over_private_pipe(self):
        details = ({'camera_id': 42, 'backend': 'isapi', 'stage': 'deviceInfo', 'reason': 'html-error-page'},
                   {'camera_id': 42, 'backend': 'videolink', 'stage': 'getuid', 'reason': 'xml-root-unexpected'})
        command = json.dumps({'operation': 'connect', 'camera': asdict(CAMERA)})+'\n'
        output = io.StringIO()
        with patch('playback.remote.sys.stdin', SimpleNamespace(buffer=io.BytesIO(command.encode()))), \
                patch('playback.remote.sys.stdout', output), \
                patch('playback.backends.connect', side_effect=PlaybackError('backend-detection-failed', details)):
            worker_main()
        result = json.loads(output.getvalue())
        self.assertEqual(result['code'], 'backend-detection-failed')
        self.assertEqual(result['details'], list(details))
        self.assertNotIn(CAMERA.password, output.getvalue())


if __name__ == '__main__':
    unittest.main()
