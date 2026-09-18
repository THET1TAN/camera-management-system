"""Display metrics from counters; never used to decide stream health."""
from collections import deque


class BitrateAverage:
    """Legacy five-reading average, with readings at least one second apart."""
    def __init__(self):
        self.samples = deque(maxlen=5)
        self.baseline = None
        self.last = None
        self.missing_since = None
        self.value = 0.

    def reset(self, now=None, received=None):
        self.samples.clear()
        self.baseline = self.last = (now, received) if received is not None else None
        self.value = 0.

    def observe(self, now, received):
        if received is None or received < 0:
            if self.missing_since is None:
                self.missing_since = now
            if now - self.missing_since >= 3:
                self.reset()
            return self.value
        self.missing_since = None
        if self.last is None or now <= self.last[0] or received < self.last[1]:
            # A first sample or reset is a baseline, never a traffic burst.
            self.reset(now, received)
            return self.value
        self.last = (now, received)
        elapsed = now - self.baseline[0]
        if elapsed >= 1.:
            self.samples.append((received - self.baseline[1]) * 8 / elapsed / 1_000_000)
            self.baseline = (now, received)
            self.value = max(0., min(sum(self.samples) / len(self.samples), 100.))
        return self.value
