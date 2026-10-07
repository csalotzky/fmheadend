"""Raw meter specifications and worker-only native access. No audio processing."""
import math
import time
from collections import deque

from ..models.dsp import IMPLEMENTATIONS


DYNAMICS = tuple(f'{prefix}-{side}' for prefix in ('ilm', 'slm', 'elm', 'rlm', 'olm') for side in ('l', 'r'))
METERS = {
    'LSP_MULTIBAND_GATE': ('ilm-l', 'ilm-r', 'olm-l', 'olm-r'),
    'LSP_MULTIBAND_EXPANDER': ('ilm-l', 'ilm-r', 'olm-l', 'olm-r'),
    'LSP_COMPRESSOR': DYNAMICS + ('rl',),
    'LSP_AGC': ('g-g', 'g-in-s', 'g-in-l', 'g-out-s', 'g-out-l'),
    'LSP_EQ': ('iml', 'imr', 'sml', 'smr'),
    'LSP_MULTIBAND': ('ilm-l', 'ilm-r', 'olm-l', 'olm-r'),
    'LSP_LIMITER': ('ilm-l', 'ilm-r', 'sclm-l', 'sclm-r', 'grlm-l', 'grlm-r', 'olm-l', 'olm-r'),
    'FM_HEADEND_LIMITER': ('detector-peak-dbfs', 'gain-reduction-db'),
    'FM_HEADEND_CLIPPER': ('detector-peak-dbfs', 'clip-count', 'max-clipping-db', 'buffer-max-clipping-db'),
}
AGC_ENABLE = ('e-g', 'e-in-s', 'e-in-l', 'e-out-s', 'e-out-l')


class StageMeters:
    """Owned and used exclusively by the Source worker for one native generation."""

    def __init__(self, element, implementation, settings):
        self.element = element
        self.implementation = implementation
        self.factory = IMPLEMENTATIONS[implementation][1]
        self.properties = METERS[implementation]
        if implementation in ('LSP_MULTIBAND', 'LSP_MULTIBAND_GATE', 'LSP_MULTIBAND_EXPANDER'):
            self.properties += tuple(prop for i in range(len(settings['crossovers_hz']) + 1)
                                     for prop in (f'elm-{i}l', f'elm-{i}r', f'rlm-{i}l', f'rlm-{i}r', f'rl-{i}'))
            if implementation == 'LSP_MULTIBAND_GATE':
                self.properties = tuple(prop for prop in self.properties if not prop.startswith('rl-'))
        self.original = {}
        self.errors = {}
        self.enabled = False
        self._clipping_maxima = deque()

    def enable(self, enabled):
        if enabled == self.enabled:
            return
        self.enabled = enabled
        if self.implementation != 'LSP_AGC':
            return
        for prop in AGC_ENABLE:
            try:
                if enabled:
                    self.original[prop] = self.element.get_property(prop)
                    self.element.set_property(prop, True)
                elif prop in self.original:
                    self.element.set_property(prop, self.original[prop])
                self.errors.pop(prop, None)
            except Exception:
                self.errors[prop] = 'meter-enable failed' if enabled else 'meter-restore failed'

    def collect(self):
        values, errors = {}, dict(self.errors)
        for prop in self.properties:
            try:
                value = self.element.get_property(prop)
                if not isinstance(value, (int, float, bool)) or (isinstance(value, float) and not math.isfinite(value)):
                    values[prop] = None
                    errors[prop] = 'non-finite or non-numeric'
                else:
                    values[prop] = value
            except Exception:
                values[prop] = None
                errors[prop] = 'unavailable'
        if self.implementation == 'FM_HEADEND_CLIPPER':
            now = time.monotonic_ns()
            cutoff = now - 10_000_000_000
            while self._clipping_maxima and self._clipping_maxima[0][0] < cutoff:
                self._clipping_maxima.popleft()
            clipping = values.get('buffer-max-clipping-db')
            if clipping is not None:
                # Monotonic maximum deque: newer equal/larger samples supersede
                # older ones. Keep timestamps so peaks expire, rather than decay.
                while self._clipping_maxima and self._clipping_maxima[-1][1] <= clipping:
                    self._clipping_maxima.pop()
                self._clipping_maxima.append((now, clipping))
                values['max-clipping-db-10s'] = self._clipping_maxima[0][1]
        result = dict(implementation=self.implementation, factory=self.factory, values=values)
        if errors:
            result['errors'] = errors
        return result
