"""Advertised speed spaces and inspection of serialized ONVIF motion fields."""
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock
from xml.etree import ElementTree as ET

from ptz_command_worker import ControlState, PTZCommandWorker
from ptz_diagnostics import PTZWireTrace, install_wire_trace
from ptz_velocity import VelocitySpaces


def space(uri, x=(-1, 1), y=None):
    value = NS(URI=uri, XRange=NS(Min=x[0], Max=x[1]))
    if y is not None:
        value.YRange = NS(Min=y[0], Max=y[1])
    return value


class VelocityTests(unittest.TestCase):
    def options(self, pan_tilt=(), zoom=()):
        return NS(Spaces=NS(ContinuousPanTiltVelocitySpace=list(pan_tilt),
                            ContinuousZoomVelocitySpace=list(zoom)))

    def test_advertised_profile_defaults_are_selected(self):
        options = self.options([space('first', y=(-1, 1)), space('chosen-pt', y=(-2, 3))],
                               [space('chosen-zoom')])
        config = NS(DefaultContinuousPanTiltVelocitySpace='chosen-pt',
                    DefaultContinuousZoomVelocitySpace='chosen-zoom')
        selected = VelocitySpaces.from_options(config, options)
        payload = selected.build((0.5, -0.5, 0.25))
        self.assertEqual(payload, {'PanTilt': {'x': 0.5, 'y': -1, 'space': 'chosen-pt'},
                                   'Zoom': {'x': 0.25, 'space': 'chosen-zoom'}})

    def test_signed_asymmetric_ranges_preserve_zero_and_direction(self):
        selected = VelocitySpaces(('pt', (-2, 4), (-6, 8)), ('zoom', (-0.2, 0.6)))
        self.assertEqual(selected.build((-0.5, 0.5, -0.5)), {
            'PanTilt': {'x': -1, 'y': 4, 'space': 'pt'}, 'Zoom': {'x': -0.1, 'space': 'zoom'}})
        self.assertEqual(selected.build((0, 0.25, 0)), {'PanTilt': {'x': 0, 'y': 2, 'space': 'pt'}})

    def test_generic_advertised_space_is_preferred_when_default_missing(self):
        generic = 'http://www.onvif.org/ver10/tptz/PanTiltSpaces/VelocityGenericSpace'
        options = self.options([space('other', y=(-2, 2)), space(generic, y=(-1, 1))])
        selected = VelocitySpaces.from_options(NS(), options)
        self.assertEqual(selected.pan_tilt[0], generic)

    def test_invalid_ranges_are_ignored_without_inventing_a_space(self):
        for limits in [(0, 0), (1, -1), (float('nan'), 1), (-1, float('inf')), (0, 1)]:
            with self.subTest(limits=limits):
                options = self.options([space('invalid', x=limits, y=(-1, 1))])
                selected = VelocitySpaces.from_options(NS(), options)
                self.assertIsNone(selected.pan_tilt)
                self.assertEqual(selected.build((0.5, 0.5, 0)), {'PanTilt': {'x': 0.5, 'y': 0.5}})

    def test_unavailable_options_keep_existing_normalized_requests(self):
        self.assertEqual(VelocitySpaces.from_options(None, None).build((0.5, -0.5, 0.5)), {
            'PanTilt': {'x': 0.5, 'y': -0.5}, 'Zoom': {'x': 0.5}})

    def test_speed_is_clamped_inside_selected_ranges(self):
        selected = VelocitySpaces(('pt', (-2, 4), (-6, 8)), ('zoom', (-0.2, 0.6)))
        payload = selected.build((2, -3, 2))
        self.assertEqual((payload['PanTilt']['x'], payload['PanTilt']['y'], payload['Zoom']['x']),
                         (4, -6, 0.6))

    def test_capability_summary_excludes_device_specific_uri(self):
        selected = VelocitySpaces(('private-device-uri', (-1, 1), (-1, 1)),
                                  ('another-private-uri', (-1, 1)))
        self.assertNotIn('private', str(selected.summary()))

    def test_zeroed_groups_keep_their_advertised_space(self):
        selected = VelocitySpaces(('pt', (-2, 4), (-6, 8)), ('zoom', (-0.2, 0.6)))
        self.assertEqual(selected.build((0, 0, 0.5), stop_pan_tilt=True), {
            'PanTilt': {'x': 0, 'y': 0, 'space': 'pt'}, 'Zoom': {'x': 0.3, 'space': 'zoom'}})
        self.assertEqual(selected.build((0.5, 0, 0), stop_zoom=True), {
            'PanTilt': {'x': 2, 'y': 0, 'space': 'pt'}, 'Zoom': {'x': 0, 'space': 'zoom'}})

    def test_idle_unsupported_groups_are_still_omitted(self):
        self.assertEqual(VelocitySpaces().build((0.5, 0, 0)), {'PanTilt': {'x': 0.5, 'y': 0}})
        self.assertEqual(VelocitySpaces().build((0, 0, 0.5)), {'Zoom': {'x': 0.5}})

    def test_worker_sends_scaled_groups_in_one_request(self):
        service = Mock()
        service.create_type.side_effect = lambda name: NS()
        selected = VelocitySpaces(('pt', (-2, 4), (-6, 8)), ('zoom', (-0.2, 0.6)))
        worker = PTZCommandWorker(service, Mock(), 'profile', 'source', velocity_spaces=selected)
        worker.submit(ControlState(pan=0.5, tilt=-0.5, zoom=0.5))
        worker.step()
        request = service.ContinuousMove.call_args.args[0]
        self.assertEqual(request.Velocity, {'PanTilt': {'x': 2, 'y': -3, 'space': 'pt'},
                                            'Zoom': {'x': 0.3, 'space': 'zoom'}})
        self.assertEqual(worker.motion, (0.5, -0.5, 0.5))
        service.ContinuousMove.assert_called_once()


class WireTraceTests(unittest.TestCase):
    def envelope(self):
        return ET.fromstring('''<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
          xmlns:p="http://www.onvif.org/ver20/ptz/wsdl" xmlns:t="http://www.onvif.org/ver10/schema">
          <s:Header><password>private-password</password></s:Header><s:Body><p:ContinuousMove>
          <p:ProfileToken>private-profile</p:ProfileToken><p:Velocity>
          <t:PanTilt x="0.5" y="-0.5" space="private-device-uri"/>
          <t:Zoom x="0.25" space="private-device-uri"/>
          </p:Velocity></p:ContinuousMove></s:Body></s:Envelope>''')

    def test_actual_xml_fields_are_recorded_without_private_data_or_modification(self):
        diagnostics = Mock()
        envelope = self.envelope()
        before = ET.tostring(envelope)
        headers = {'Authorization': 'private-auth'}
        result = PTZWireTrace(diagnostics).egress(envelope, headers, NS(name='ContinuousMove'), {})
        self.assertIs(result[0], envelope)
        self.assertIs(result[1], headers)
        self.assertEqual(ET.tostring(envelope), before)
        diagnostics.record.assert_called_once_with('soap_move', pan=0.5, tilt=-0.5, zoom=0.25,
                                                    pan_tilt_space=True, zoom_space=True)
        self.assertNotIn('private', str(diagnostics.record.call_args))

    def test_diagnostic_failure_does_not_prevent_sending_or_receiving(self):
        diagnostics = Mock()
        diagnostics.record.side_effect = OSError('disk full')
        plugin = PTZWireTrace(diagnostics)
        envelope = self.envelope()
        operation = NS(name='ContinuousMove')
        self.assertEqual(plugin.egress(envelope, {}, operation, {}), (envelope, {}))
        self.assertEqual(plugin.ingress(envelope, {}, operation), (envelope, {}))

    def test_non_movement_operations_are_not_logged(self):
        diagnostics = Mock()
        PTZWireTrace(diagnostics).egress(self.envelope(), {}, NS(name='GetProfiles'), {})
        diagnostics.record.assert_not_called()

    def test_plugin_is_added_without_replacing_existing_plugins(self):
        existing = object()
        plugins = [existing]
        service = NS(zeep_client=NS(plugins=plugins))
        self.assertTrue(install_wire_trace(service, Mock()))
        self.assertIs(plugins[0], existing)
        self.assertIsInstance(plugins[1], PTZWireTrace)
        self.assertFalse(install_wire_trace(NS(), Mock()))


if __name__ == '__main__':
    unittest.main()
