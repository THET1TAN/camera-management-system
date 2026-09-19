import base64
import hashlib
import json
import os
from pathlib import Path
import queue
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from xml.etree import ElementTree as ET

from camera_health import (CameraProbe, CameraTarget, Cancelled, Endpoint, HealthSettings,
                           HealthState, Observation, RTSPReply, SCHEMA, MEDIA, WSSE, WSU,
                           device_url, discover_rtsp, endpoint_from_url, load_overrides,
                           ping_available, rtsp_options, soap_envelope)
from camera_health_monitor import HealthMonitor, run_monitor


def stalled_monitor(targets, settings, stop, output):
    # Stand-in for an OS resolver or library call that ignores cancellation.
    time.sleep(30)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.stop = threading.Event()
        self.target = CameraTarget(1, '192.0.2.1', 'private-user', 'private-password',
                                   rtsp_url='rtsp://192.0.2.1:8554/private-stream')
        self.options = Mock(return_value=RTSPReply(200, True))
        self.tcp = Mock(return_value=False)
        self.ping = Mock(return_value=False)
        self.discover = Mock(return_value=Endpoint('192.0.2.1', 9554))
        self.probe = CameraProbe(self.target, discover=self.discover, options=self.options,
                                 tcp=self.tcp, ping=self.ping)

    def test_rtsp_success_skips_every_fallback_and_discovery_for_override(self):
        self.assertEqual(self.probe.check(self.stop).state, 'online')
        self.tcp.assert_not_called()
        self.ping.assert_not_called()
        self.discover.assert_not_called()
        self.assertEqual(self.options.call_args.args[0].port, 8554)

    def test_authentication_challenge_means_service_alive(self):
        self.options.return_value = RTSPReply(401, True)
        self.assertEqual(self.probe.check(self.stop).state, 'online')
        self.ping.assert_not_called()

    def test_rtsp_error_is_degraded_without_fallback(self):
        for code in (403, 404, 405, 500, 501, 503):
            with self.subTest(code=code):
                self.options.return_value = RTSPReply(code, True)
                self.assertEqual(self.probe.check(self.stop).state, 'degraded')
        self.tcp.assert_not_called()
        self.ping.assert_not_called()

    def test_http_fallback_prevents_ping(self):
        self.options.return_value = RTSPReply()
        self.tcp.return_value = True
        self.assertEqual(self.probe.check(self.stop).state, 'degraded')
        self.ping.assert_not_called()

    def test_ping_is_last_and_ping_only_is_degraded(self):
        order = []
        self.options.side_effect = lambda *a: order.append('rtsp') or RTSPReply()
        self.tcp.side_effect = lambda *a: order.append('http') or False
        self.ping.side_effect = lambda *a: order.append('ping') or True
        result = self.probe.check(self.stop)
        self.assertEqual(order, ['rtsp', 'http', 'ping'])
        self.assertEqual(result.state, 'degraded')
        self.assertIn('ping', result.detail)

    def test_all_checks_fail(self):
        self.options.return_value = RTSPReply()
        self.assertEqual(self.probe.check(self.stop).state, 'offline')

    def test_open_but_silent_rtsp_is_not_a_false_offline(self):
        self.options.return_value = RTSPReply(connected=True)
        self.assertEqual(self.probe.check(self.stop).state, 'degraded')

    def test_cancel_before_check_performs_no_network(self):
        self.stop.set()
        with self.assertRaises(Cancelled):
            self.probe.check(self.stop)
        self.options.assert_not_called()

    def test_cancel_between_primary_and_fallback(self):
        def cancel(*args):
            self.stop.set()
            return RTSPReply()
        self.options.side_effect = cancel
        with self.assertRaises(Cancelled):
            self.probe.check(self.stop)
        self.tcp.assert_not_called()
        self.ping.assert_not_called()

    def test_discovery_cached_and_rediscovered_only_after_persistent_failure(self):
        now = [0.0]
        self.probe = CameraProbe(CameraTarget(1, '192.0.2.1'), discover=self.discover,
                                 options=self.options, tcp=self.tcp, ping=self.ping, clock=lambda: now[0])
        self.probe.check(self.stop)
        self.probe.check(self.stop)
        self.discover.assert_called_once()
        self.options.return_value = RTSPReply()
        for _ in range(4):
            self.probe.check(self.stop)
        self.discover.assert_called_once()
        now[0] = 61
        self.probe.check(self.stop)
        self.assertEqual(self.discover.call_count, 2)

    def test_failed_discovery_has_backoff_and_safe_details(self):
        self.discover.side_effect = RuntimeError('private-password rtsp://private-user:private-password@camera')
        probe = CameraProbe(CameraTarget(1, '192.0.2.1'), discover=self.discover,
                            options=self.options, tcp=Mock(return_value=True), ping=self.ping)
        result = probe.check(self.stop)
        probe.check(self.stop)
        self.discover.assert_called_once()
        self.options.assert_not_called()
        self.assertEqual(result.state, 'degraded')
        self.assertNotIn('private', repr(result))


class StateTests(unittest.TestCase):
    def test_initial_failure_needs_confirmation(self):
        state = HealthState()
        fail = Observation('offline', 'unavailable')
        self.assertEqual(state.apply(fail).state, 'unknown')
        self.assertEqual(state.apply(fail).state, 'offline')

    def test_transient_loss_and_recovery_reset_the_counter(self):
        state = HealthState()
        online = Observation('online', 'responds')
        offline = Observation('offline', 'unavailable')
        state.apply(online)
        self.assertEqual(state.apply(offline).state, 'online')
        self.assertEqual(state.apply(online).state, 'online')
        self.assertEqual(state.apply(offline).state, 'online')
        self.assertEqual(state.apply(offline).state, 'offline')
        self.assertEqual(state.apply(online).state, 'online')

    def test_ping_recovery_is_degraded_and_resets_offline_failures(self):
        state = HealthState()
        state.apply(Observation('offline', 'unavailable'))
        state.apply(Observation('offline', 'unavailable'))
        self.assertEqual(state.apply(Observation('degraded', 'ping responds')).state, 'degraded')
        self.assertEqual(state.failed_checks, 0)


class EndpointTests(unittest.TestCase):
    def test_preserves_discovered_port_and_discards_every_private_uri_part(self):
        endpoint = endpoint_from_url('rtsp://user:password@192.0.2.9:9554/path?token=secret')
        self.assertEqual(endpoint, Endpoint('192.0.2.9', 9554))
        self.assertNotIn('password', repr(endpoint))
        self.assertNotIn('secret', repr(endpoint))

    def test_url_defaults_ipv6_and_tls(self):
        self.assertEqual(endpoint_from_url('rtsp://[::1]/a'), Endpoint('::1', 554))
        self.assertEqual(endpoint_from_url('rtsps://camera/a'), Endpoint('camera', 322, True))
        self.assertEqual(device_url(CameraTarget(1, '::1')), 'http://[::1]/onvif/device_service')

    def test_invalid_addresses_are_rejected(self):
        for url in ('', 'http://camera', 'rtsp://', 'rtsp://camera:0', 'rtsp://camera:99999', 'rtsp://bad host'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                endpoint_from_url(url)

    def test_optional_config_supports_custom_ports_and_rejects_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(load_overrides(folder), {})
            path = Path(folder) / 'camera_health.json'
            data = {'1': {'onvif_url': 'http://camera:8080/custom', 'rtsp_url': 'rtsp://camera:8554/path'}}
            path.write_text(json.dumps(data))
            self.assertEqual(load_overrides(folder), data)
            data['1']['rtsp_url'] = 'rtsp://user:password@camera/path'
            path.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                load_overrides(folder)

    def test_target_repr_does_not_expose_credentials_or_addresses(self):
        target = CameraTarget(1, 'secret-host', 'secret-user', 'secret-password', 'secret-url')
        self.assertEqual(repr(target), 'CameraTarget(camera_id=1)')


class DiscoveryTests(unittest.TestCase):
    def test_onvif_reads_only_capabilities_profiles_and_uri(self):
        documents = [
            f'<r xmlns:tt="{SCHEMA}"><tt:Media><tt:XAddr>http://camera:8080/media</tt:XAddr></tt:Media></r>',
            f'<r xmlns:trt="{MEDIA}"><trt:Profiles token="profile&amp;one"/></r>',
            f'<r xmlns:tt="{SCHEMA}"><tt:Uri>rtsp://user:password@camera:8554/custom</tt:Uri></r>'
        ]
        request = Mock(side_effect=[ET.fromstring(d) for d in documents])
        result = discover_rtsp(CameraTarget(1, 'camera'), 2, threading.Event(), request=request)
        self.assertEqual(result, Endpoint('camera', 8554))
        self.assertEqual([c.args[2].split('/')[-1] for c in request.call_args_list],
                         ['GetCapabilities', 'GetProfiles', 'GetStreamUri'])
        body = request.call_args_list[2].args[3]
        self.assertEqual(ET.fromstring(body).findtext(f'{{{MEDIA}}}ProfileToken'), 'profile&one')
        self.assertEqual(request.call_args_list[1].args[0], 'http://camera:8080/media')

    def test_missing_profiles_fails_without_a_manufacturer_fallback(self):
        request = Mock(side_effect=[ET.fromstring(f'<r xmlns:tt="{SCHEMA}"><tt:Media><tt:XAddr>http://camera/media</tt:XAddr></tt:Media></r>'), ET.fromstring('<r/>')])
        with self.assertRaises(ValueError):
            discover_rtsp(CameraTarget(1, 'camera'), 2, threading.Event(), request=request)
        self.assertEqual(request.call_count, 2)

    def test_wsse_digest_and_xml_escaping(self):
        xml = soap_envelope('user<&', 'password', '<request/>')
        root = ET.fromstring(xml)
        self.assertNotIn(b'>password<', xml)
        self.assertEqual(root.findtext(f'.//{{{WSSE}}}Username'), 'user<&')
        nonce = base64.b64decode(root.findtext(f'.//{{{WSSE}}}Nonce'))
        created = root.findtext(f'.//{{{WSU}}}Created').encode()
        expected = base64.b64encode(hashlib.sha1(nonce + created + b'password').digest()).decode()
        self.assertEqual(root.findtext(f'.//{{{WSSE}}}Password'), expected)


class RTSPWireTests(unittest.TestCase):
    def exchange(self, response, timeout=0.3, hold=False):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        request = []
        release = threading.Event()
        def server():
            try:
                with listener, listener.accept()[0] as client:
                    client.settimeout(1)
                    request.append(client.recv(4096))
                    if response:
                        # Deliberately split the response to exercise partial reads.
                        client.sendall(response[:8])
                        client.sendall(response[8:])
                    if hold:
                        release.wait(2)
            except OSError:
                pass
        thread = threading.Thread(target=server)
        thread.start()
        try:
            result = rtsp_options(Endpoint('127.0.0.1', port), timeout, threading.Event())
        finally:
            release.set()
            thread.join(2)
        return result, request

    def test_real_socket_sends_only_options_and_parses_split_reply(self):
        result, request = self.exchange(b'RTSP/1.0 200 OK\r\nCSeq: 1\r\nPublic: OPTIONS\r\n\r\n')
        self.assertEqual(result, RTSPReply(200, True))
        self.assertEqual(len(request), 1)
        self.assertTrue(request[0].startswith(b'OPTIONS * RTSP/1.0\r\n'))
        self.assertNotIn(b'Authorization', request[0])
        self.assertNotIn(b'PLAY', request[0])

    def test_401_is_parsed_without_authenticating(self):
        result, _ = self.exchange(b'RTSP/1.0 401 Unauthorized\r\nCSeq: 1\r\n\r\n')
        self.assertEqual(result.code, 401)

    def test_malformed_http_wrong_cseq_and_incomplete_replies_are_not_online(self):
        for response in (b'HTTP/1.1 200 OK\r\nCSeq: 1\r\n\r\n',
                         b'RTSP/1.0 200 OK\r\nCSeq: 99\r\n\r\n',
                         b'RTSP/1.0 200 OK\r\nCSeq: 1\r\n'):
            with self.subTest(response=response):
                result, _ = self.exchange(response)
                self.assertIsNone(result.code)
                self.assertTrue(result.connected)

    def test_silent_service_has_bounded_timeout(self):
        start = time.monotonic()
        result, _ = self.exchange(b'', hold=True)
        self.assertEqual(result, RTSPReply(connected=True))
        self.assertLess(time.monotonic() - start, 1.5)

    def test_cancel_during_receive_exits_promptly(self):
        stop = threading.Event()
        sock = Mock()
        sock.__enter__ = Mock(return_value=sock)
        sock.__exit__ = Mock()
        def receive(*a):
            stop.set()
            raise socket.timeout()
        sock.recv.side_effect = receive
        with patch('camera_health._connect', return_value=sock), self.assertRaises(Cancelled):
            rtsp_options(Endpoint('camera', 554), 2, stop)

    @unittest.skipUnless(os.name == 'nt', 'Windows ICMP API')
    def test_native_ping_loopback(self):
        self.assertTrue(ping_available('127.0.0.1', 0.5, threading.Event()))

    @unittest.skipUnless(os.name == 'nt' and socket.has_ipv6, 'Windows IPv6 ICMP API')
    def test_native_ping_ipv6_loopback(self):
        self.assertTrue(ping_available('::1', 0.5, threading.Event()))


class SchedulerTests(unittest.TestCase):
    def test_stalled_helper_is_terminated_after_grace_without_blocking_poll(self):
        with patch('camera_health_monitor._entry', stalled_monitor):
            monitor = HealthMonitor([])
        try:
            start = time.monotonic()
            monitor.stdin.close()
            while monitor.poll() is None and time.monotonic() - start < 2:
                time.sleep(0.02)
            self.assertIsNotNone(monitor.poll())
            self.assertLess(time.monotonic() - start, 1.5)
        finally:
            monitor.kill()
            monitor.process.join(2)

    def test_one_slow_camera_does_not_block_others_and_pool_is_bounded(self):
        stop = threading.Event()
        release = threading.Event()
        output = queue.Queue()
        active = [0, 0]
        lock = threading.Lock()
        class Probe:
            def __init__(self, target, settings):
                self.id = target.camera_id
            def check(self, stop):
                with lock:
                    active[0] += 1
                    active[1] = max(active)
                try:
                    if self.id == 1:
                        release.wait(2)
                    return Observation('online', 'responds')
                finally:
                    with lock:
                        active[0] -= 1
        targets = [CameraTarget(i, 'test') for i in range(1, 7)]
        thread = threading.Thread(target=run_monitor,
                                  args=(targets, HealthSettings(workers=2), stop, output, Probe))
        thread.start()
        try:
            update = output.get(timeout=2)
            self.assertNotEqual(update.camera_id, 1)
            self.assertEqual(update.state, 'online')
            self.assertLessEqual(active[1], 2)
        finally:
            stop.set()
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_failure_does_not_kill_supervision_or_expose_exception(self):
        stop = threading.Event()
        output = queue.Queue()
        class Broken:
            def __init__(self, *args):
                pass
            def check(self, stop):
                raise ValueError('private-password')
        thread = threading.Thread(target=run_monitor,
                                  args=([CameraTarget(1, 'test')], HealthSettings(), stop, output, Broken))
        thread.start()
        try:
            update = output.get(timeout=2)
            self.assertEqual(update.state, 'unknown')
            self.assertNotIn('private-password', repr(update))
        finally:
            stop.set()
            thread.join(2)

    def test_real_process_closes_during_a_silent_rtsp_request(self):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        listener.settimeout(4)
        monitor = HealthMonitor([CameraTarget(1, '127.0.0.1',
                                rtsp_url=f'rtsp://127.0.0.1:{listener.getsockname()[1]}/')],
                                HealthSettings(timeout=10))
        try:
            with listener.accept()[0]:
                start = time.monotonic()
                monitor.stdin.close()
                while monitor.poll() is None and time.monotonic() - start < 2:
                    time.sleep(0.02)
                self.assertIsNotNone(monitor.poll())
                self.assertLess(time.monotonic() - start, 1.5)
        finally:
            listener.close()
            monitor.kill()
            monitor.process.join(2)


if __name__ == '__main__':
    unittest.main()
