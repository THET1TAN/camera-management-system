"""Opt-in HTTP phase timings; delegate every ONVIF command without modification."""
import threading
import time


class TimedPTZTransport:
    def __init__(self, wrapped, diagnostics, clock=time.monotonic):
        self._wrapped = wrapped
        self._diagnostics = diagnostics
        self._clock = clock
        self._pending = threading.local()
        self._hooks = wrapped.session.hooks['response']
        self._hook = self._capture_headers
        self._hooks.append(self._hook)

    def __getattr__(self, name):
        return getattr(self._wrapped, name)

    def __setattr__(self, name, value):
        if name.startswith('_'):
            object.__setattr__(self, name, value)
        else:
            setattr(self._wrapped, name, value)

    def detach(self):
        if self._hook in self._hooks:
            self._hooks.remove(self._hook)

    def _capture_headers(self, response, *args, **kwargs):
        try:
            pending = getattr(self._pending, 'value', None)
            if pending is not None:
                pending['headers_at'] = self._clock()
                pending['response_id'] = id(response)
        except Exception:
            pass
        return response

    @staticmethod
    def _operation(envelope):
        body = envelope.find('{http://www.w3.org/2003/05/soap-envelope}Body')
        if body is not None and len(body):
            for name in ('Stop', 'ContinuousMove'):
                if body[0].tag == '{http://www.onvif.org/ver20/ptz/wsdl}' + name:
                    return name
        return None

    def post_xml(self, address, envelope, headers):
        # Never copy connection details, headers, profile tokens or XML into logs.
        pending = None
        try:
            operation = self._operation(envelope)
            if operation is not None:
                pending = {'operation': operation, 'started_at': self._clock()}
        except Exception:
            pass
        previous = getattr(self._pending, 'value', None)
        self._pending.value = pending
        response = None
        error_type = None
        try:
            response = self._wrapped.post_xml(address, envelope, headers)
            return response
        except Exception as error:
            error_type = type(error).__name__
            raise
        finally:
            self._pending.value = previous
            if pending is not None:
                try:
                    completed_at = self._clock()
                    headers_at = pending.get('headers_at')
                    if response is not None and pending.get('response_id') != id(response):
                        headers_at = None  # A hook replaced the response; do not misattribute timing.
                    status = getattr(response, 'status_code', None)
                    self._diagnostics.record(
                        'http_timing', operation=pending['operation'],
                        transport_seconds=round(completed_at - pending['started_at'], 6),
                        headers_seconds=(round(headers_at - pending['started_at'], 6)
                                         if headers_at is not None else None),
                        after_headers_seconds=(round(completed_at - headers_at, 6)
                                               if headers_at is not None else None),
                        status_code=status if type(status) is int else None,
                        error_type=error_type)
                except Exception:
                    pass  # Instrumentation cannot prevent Stop, alter its result or mask an error.


def install_http_timing(service, diagnostics, clock=time.monotonic):
    if diagnostics is None:
        return False
    proxy = None
    try:
        client = service.zeep_client
        wrapped = client.transport
        if isinstance(wrapped, TimedPTZTransport):
            return True
        hooks = wrapped.session.hooks
        if not isinstance(hooks, dict) or not isinstance(hooks.get('response'), list):
            return False
        proxy = TimedPTZTransport(wrapped, diagnostics, clock)
        client.transport = proxy
        return True
    except Exception:
        if proxy is not None:
            proxy.detach()
        return False
