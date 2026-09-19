"""Map normalized keyboard speeds into the camera's advertised ONVIF spaces."""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class VelocitySpaces:
    pan_tilt: object = None  # (URI, (x min, max), (y min, max))
    zoom: object = None  # (URI, (min, max))

    @classmethod
    def from_options(cls, configuration, options):
        spaces = getattr(options, 'Spaces', None)

        def bounds(value):
            try:
                low, high = float(value.Min), float(value.Max)
                if math.isfinite(low) and math.isfinite(high) and low < 0 < high:
                    return low, high
            except (AttributeError, TypeError, ValueError):
                pass
            return None

        def select(attribute, default_attribute, group, two_axes=False):
            advertised = getattr(spaces, attribute, None)
            if not isinstance(advertised, (list, tuple)):
                return None
            valid = []
            for entry in advertised:
                uri = getattr(entry, 'URI', None)
                x = bounds(getattr(entry, 'XRange', None))
                y = bounds(getattr(entry, 'YRange', None)) if two_axes else None
                if isinstance(uri, str) and uri and x and (y or not two_axes):
                    valid.append((uri, x, y) if two_axes else (uri, x))
            preferred = getattr(configuration, default_attribute, None)
            generic = f'http://www.onvif.org/ver10/tptz/{group}Spaces/VelocityGenericSpace'
            for uri in (preferred, generic):
                for entry in valid:
                    if entry[0] == uri:
                        return entry
            return valid[0] if valid else None

        return cls(
            select('ContinuousPanTiltVelocitySpace', 'DefaultContinuousPanTiltVelocitySpace',
                   'PanTilt', True),
            select('ContinuousZoomVelocitySpace', 'DefaultContinuousZoomVelocitySpace', 'Zoom'))

    @staticmethod
    def _scale(value, limits):
        value = max(-1.0, min(1.0, value))
        return value * limits[1] if value >= 0 else -value * limits[0]

    def build(self, motion, stop_pan_tilt=False, stop_zoom=False):
        pan, tilt, zoom = motion
        velocity = {}
        if pan or tilt or stop_pan_tilt:
            vector = {'x': pan, 'y': tilt}
            if self.pan_tilt:
                uri, x, y = self.pan_tilt
                vector = {'x': self._scale(pan, x), 'y': self._scale(tilt, y), 'space': uri}
            velocity['PanTilt'] = vector
        if zoom or stop_zoom:
            vector = {'x': zoom}
            if self.zoom:
                uri, limits = self.zoom
                vector = {'x': self._scale(zoom, limits), 'space': uri}
            velocity['Zoom'] = vector
        return velocity

    def summary(self):
        # Do not log device-specific URIs: they could contain camera addresses.
        return {'pan_tilt_ranges': self.pan_tilt[1:] if self.pan_tilt else None,
                'zoom_range': self.zoom[1] if self.zoom else None}
