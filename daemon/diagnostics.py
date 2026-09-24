"""Presentation-only error scope and bounded, nonpersistent backend logs."""
from collections import deque
from datetime import datetime, timezone
import logging

from .rp2354.protocol import GLOBAL_COMMANDS


def error_scope(error):
    # Preserve origin metadata, never infer scope from human-readable text.
    if (getattr(error, 'command', None) in GLOBAL_COMMANDS or
            getattr(error, 'stage', None) in ('open', 'backoff')):
        return 'GLOBAL'
    return 'TX'


class RecentLogs(logging.Handler):
    """Existing fmheadendd logging only; does not change logger levels/sinks."""
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.entries = deque(maxlen=100)

    def emit(self, record):
        self.entries.append({
            'timestamp': datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            'severity': record.levelname,
            'logger': record.name,
            'message': self.format(record)[:4096],
        })

    def snapshot(self):
        self.acquire()
        try:
            return [dict(entry) for entry in reversed(self.entries)]
        finally:
            self.release()

    def __enter__(self):
        logging.getLogger('fmheadendd').addHandler(self)
        return self

    def __exit__(self, *args):
        logging.getLogger('fmheadendd').removeHandler(self)
        self.close()
