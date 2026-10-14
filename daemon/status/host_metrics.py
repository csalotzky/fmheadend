"""Demand-driven Linux host metrics; no background monitoring."""
import sys
import threading
import time
from pathlib import Path


class HostMetrics:
    CACHE_SECONDS = 5.0
    BASELINE_MAX_AGE_SECONDS = 60.0
    CPU_SENSOR_TYPES = frozenset({
        'cpu-thermal', 'rpi-thermal', 'bcm2835_thermal', 'cpu_thermal',
        'x86_pkg_temp', 'cpu-package', 'soc-thermal', 'soc_thermal',
    })

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._lock = threading.Lock()
        self._observed_at = None
        self._previous = None
        self._values = (None, None)
        self._sensor_checked = False
        self._sensor = None

    def read(self):
        # Concurrent readers reuse the cache rather than queue refresh work.
        if not self._lock.acquire(blocking=False):
            return self._values
        try:
            now = self.clock()
            age = None if self._observed_at is None else now - self._observed_at
            if age is not None and age < self.CACHE_SECONDS:
                return self._values
            cpu = None
            current = self._cpu_counters()
            if (current is not None and self._previous is not None
                    and age is not None and age <= self.BASELINE_MAX_AGE_SECONDS):
                total = current[0] - self._previous[0]
                idle = current[1] - self._previous[1]
                if total > 0 and idle >= 0:
                    cpu = max(0.0, min(100.0, 100.0 * (total - idle) / total))
            self._previous = current
            self._values = (cpu, self._temperature())
            self._observed_at = now
            return self._values
        finally:
            self._lock.release()

    @staticmethod
    def _cpu_counters():
        if sys.platform != 'linux':
            return None
        try:
            with open('/proc/stat', encoding='ascii') as stream:
                fields = stream.readline().split()
            if not fields or fields[0] != 'cpu' or len(fields) < 5:
                return None
            # guest/guest_nice are already included in user/nice. Exclude them.
            counters = [int(value) for value in fields[1:9]]
            if any(value < 0 for value in counters):
                return None
            idle = counters[3] + (counters[4] if len(counters) > 4 else 0)
            return sum(counters), idle
        except (OSError, ValueError):
            return None

    def _temperature(self):
        if sys.platform != 'linux':
            return None
        if not self._sensor_checked:
            self._sensor_checked = True
            try:
                for zone in sorted(Path('/sys/class/thermal').glob('thermal_zone*')):
                    try:
                        sensor_type = (zone / 'type').read_text(encoding='ascii').strip().lower()
                    except (OSError, ValueError):
                        continue
                    if sensor_type in self.CPU_SENSOR_TYPES:
                        self._sensor = zone / 'temp'
                        break
            except OSError:
                pass
        if self._sensor is None:
            return None
        try:
            return int(self._sensor.read_text(encoding='ascii').strip()) / 1000.0
        except (OSError, ValueError):
            return None
