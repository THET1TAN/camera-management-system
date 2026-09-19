"""HTTP observation must never change PTZ requests, response order or failures."""
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock
from xml.etree import ElementTree as ET

from ptz_http_timing import TimedPTZTransport, install_http_timing


def envelope(operation='Stop'):
    return ET.fromstring('''<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
      xmlns:p="http://www.onvif.org/ver20/ptz/wsdl">
      <s:Header><password>private-password</password></s:Header><s:Body><p:''' + operation + '''>
      <p:ProfileToken>private-token</p:ProfileToken></p:''' + operation + '''></s:Body></s:Envelope>''')


class FakeTransport:
    def __init__(self):
        self.now = 0.0
        self.original_hook = Mock(side_effect=lambda response, **kwargs: response)
        self.session = NS(hooks={'response': [self.original_hook]}, auth=object())
        self.operation_timeout = 1
        self.received = []
        self.error = None
        self.response = NS(status_code=200, content=b'private-response')

    def post_xml(self, *args):
        self.received.append(args)
        self.now += 0.2
        for hook in list(self.session.hooks['response']):
            hook(self.response)
        self.now += 0.03
        if self.error:
            raise self.error
        return self.response


class TimingTests(unittest.TestCase):
    def setUp(self):
        self.transport = FakeTransport()
        self.diagnostics = Mock()
        self.service = NS(zeep_client=NS(transport=self.transport))
        self.assertTrue(install_http_timing(self.service, self.diagnostics, lambda: self.transport.now))
        self.proxy = self.service.zeep_client.transport

    def send(self, operation='Stop'):
        self.xml = envelope(operation)
        self.headers = {'Authorization': 'private-auth'}
        return self.proxy.post_xml('http://private-address/ptz', self.xml, self.headers)

    def test_headers_and_remaining_response_time_are_distinguished(self):
        self.send()
        self.diagnostics.record.assert_called_once_with(
            'http_timing', operation='Stop', transport_seconds=0.23,
            headers_seconds=0.2, after_headers_seconds=0.03, status_code=200, error_type=None)

    def test_original_request_objects_and_response_are_unchanged(self):
        xml = envelope('ContinuousMove')
        before = ET.tostring(xml)
        headers = {'Authorization': 'private-auth'}
        response = self.proxy.post_xml('http://private-address/ptz', xml, headers)
        self.assertIs(response, self.transport.response)
        self.assertIs(self.transport.received[0][1], xml)
        self.assertIs(self.transport.received[0][2], headers)
        self.assertEqual(ET.tostring(xml), before)
        self.assertNotIn('private-', str(self.diagnostics.record.call_args))

    def test_requests_keep_their_original_order_and_number(self):
        for operation in ('ContinuousMove', 'Stop', 'ContinuousMove', 'Stop'):
            self.send(operation)
        self.assertEqual(len(self.transport.received), 4)
        self.assertEqual([TimedPTZTransport._operation(args[1]) for args in self.transport.received],
                         ['ContinuousMove', 'Stop', 'ContinuousMove', 'Stop'])

    def test_exception_is_preserved_without_logging_its_private_message(self):
        error = TimeoutError('private-password private-address')
        self.transport.error = error
        with self.assertRaises(TimeoutError) as caught:
            self.send()
        self.assertIs(caught.exception, error)
        values = self.diagnostics.record.call_args.kwargs
        self.assertEqual(values['error_type'], 'TimeoutError')
        self.assertNotIn('private-', str(values))
        self.assertIsNone(self.proxy._pending.value)

    def test_log_failure_cannot_prevent_stop_or_replace_the_response(self):
        self.diagnostics.record.side_effect = OSError('disk full')
        self.assertIs(self.send(), self.transport.response)
        self.assertEqual(len(self.transport.received), 1)

    def test_clock_failure_cannot_prevent_stop(self):
        self.proxy._clock = Mock(side_effect=RuntimeError('clock failed'))
        self.assertIs(self.send(), self.transport.response)
        self.diagnostics.record.assert_not_called()

    def test_unrelated_operations_and_outside_responses_are_not_recorded(self):
        self.send('GetStatus')
        self.proxy._capture_headers(self.transport.response)
        self.diagnostics.record.assert_not_called()

    def test_invalid_envelope_is_still_passed_to_original_transport(self):
        value = object()
        self.assertIs(self.proxy.post_xml('address', value, {}), self.transport.response)
        self.assertIs(self.transport.received[0][1], value)
        self.diagnostics.record.assert_not_called()

    def test_existing_hooks_authentication_and_timeouts_are_preserved(self):
        self.assertIs(self.proxy.session, self.transport.session)
        self.assertIs(self.proxy.session.hooks['response'][0], self.transport.original_hook)
        self.proxy.operation_timeout = 2
        self.assertEqual(self.transport.operation_timeout, 2)
        self.send()
        self.transport.original_hook.assert_called_once()

    def test_repeated_installation_does_not_duplicate_hooks(self):
        self.assertTrue(install_http_timing(self.service, self.diagnostics))
        self.assertEqual(len(self.transport.session.hooks['response']), 2)
        self.send()
        self.diagnostics.record.assert_called_once()

    def test_missing_hook_is_reported_as_unknown_instead_of_zero(self):
        self.proxy.detach()
        self.send()
        values = self.diagnostics.record.call_args.kwargs
        self.assertIsNone(values['headers_seconds'])
        self.assertIsNone(values['after_headers_seconds'])
        self.assertEqual(values['transport_seconds'], 0.23)

    def test_failed_installation_rolls_back_its_hook(self):
        transport = FakeTransport()
        class ReadOnlyClient:
            @property
            def transport(self):
                return transport
        self.assertFalse(install_http_timing(NS(zeep_client=ReadOnlyClient()), self.diagnostics))
        self.assertEqual(transport.session.hooks['response'], [transport.original_hook])

    def test_missing_session_hooks_do_not_break_startup(self):
        self.assertFalse(install_http_timing(NS(), self.diagnostics))
        self.assertFalse(install_http_timing(NS(zeep_client=NS(transport=NS())), self.diagnostics))
        self.assertFalse(install_http_timing(self.service, None))


if __name__ == '__main__':
    unittest.main()
