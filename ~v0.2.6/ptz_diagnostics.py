"""Small local command trace; never store credentials, camera addresses or SOAP."""
import json
import logging
from logging.handlers import RotatingFileHandler
import time
import math


class PTZDiagnostics:
    def __init__(self, path):
        self.started = time.monotonic()
        self.logger = logging.Logger('PTZ command trace', level=logging.INFO)
        self.handler = RotatingFileHandler(path, maxBytes=256 * 1024, backupCount=1,
                                          encoding='utf-8')
        self.handler.namer = lambda name: name + '.log'
        self.logger.addHandler(self.handler)

    def record(self, event, **values):
        self.logger.info(json.dumps({'seconds': round(time.monotonic() - self.started, 3),
                                    'event': event, **values}))

    def close(self):
        self.handler.close()


class PTZWireTrace:
    """Zeep plugin that inspects only numeric velocity fields after serialization."""
    def __init__(self, diagnostics):
        self.diagnostics = diagnostics

    def egress(self, envelope, http_headers, operation, binding_options):
        try:
            if operation.name == 'ContinuousMove':
                ns = 'http://www.onvif.org/ver20/ptz/wsdl'
                schema = 'http://www.onvif.org/ver10/schema'
                velocity = envelope.find(f'.//{{{ns}}}ContinuousMove/{{{ns}}}Velocity')
                pan_tilt = velocity.find(f'{{{schema}}}PanTilt') if velocity is not None else None
                zoom = velocity.find(f'{{{schema}}}Zoom') if velocity is not None else None

                def number(element, attribute):
                    if element is None or element.get(attribute) is None:
                        return None
                    value = float(element.get(attribute))
                    return value if math.isfinite(value) else None

                self.diagnostics.record(
                    'soap_move', pan=number(pan_tilt, 'x'), tilt=number(pan_tilt, 'y'),
                    zoom=number(zoom, 'x'),
                    pan_tilt_space=pan_tilt is not None and bool(pan_tilt.get('space')),
                    zoom_space=zoom is not None and bool(zoom.get('space')))
        except Exception:
            pass  # A trace failure must never change or prevent a camera request.
        return envelope, http_headers

    def ingress(self, envelope, http_headers, operation):
        return envelope, http_headers


def install_wire_trace(service, diagnostics):
    plugins = getattr(getattr(service, 'zeep_client', None), 'plugins', None)
    if diagnostics is None or not isinstance(plugins, list):
        return False
    plugins.append(PTZWireTrace(diagnostics))
    return True
