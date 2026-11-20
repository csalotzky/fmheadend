"""Bounded media diagnostics, independent of recovery decisions and native locks."""
import logging
import re
import threading
import time
from urllib.parse import urlsplit

log = logging.getLogger('fmheadendd.sources.events')


def redact(value):
    def url(match):
        try:
            p = urlsplit(match.group())
            return p.scheme + '://' + (p.hostname or '[redacted]') + p.path
        except ValueError:
            return '[redacted-url]'
    return re.sub(r'https?://[^\s<>]+', url, str(value)[:4096], flags=re.IGNORECASE).replace('\n', ' ').replace('\r', ' ')[:512]


class DiagnosticEvents:
    """Source-thread producer, existing reporting-thread consumer; never a callback."""
    def __init__(self, clock=time.monotonic):
        self.clock, self.lock, self.entries = clock, threading.Lock(), {}

    def error(self, category, message):
        now = self.clock()
        key = (category[:64], redact(message))
        with self.lock:
            if key not in self.entries:
                if len(self.entries) >= 7:
                    key = ('media_other', 'Additional media errors (aggregated)')
                    if key not in self.entries and len(self.entries) >= 8:
                        return  # Bounded even under changing/malicious error text.
                self.entries.setdefault(key, dict(count=0, first=now, last=now, due=now))
            row = self.entries[key]
            row['count'] = min(2**31-1, row['count']+1)
            row['last'] = now

    def poll(self):
        now = self.clock()
        with self.lock:
            for (category, message), row in self.entries.items():
                if row['count'] and now >= row['due']:
                    event = dict(category=category, message=message, count=row['count'],
                                 first=row['first'], last=row['last'])
                    row.update(count=0, first=now, due=now+300)
                    return event
        return None


def accept(parent, value):
    """Invalid diagnostics are discarded, never turned into worker failures."""
    if not isinstance(value, dict) or set(value) != {'category','message','count','first','last'}:
        return
    if (not isinstance(value['category'], str) or not re.fullmatch(r'[a-z0-9_]{1,64}', value['category'])
            or not isinstance(value['message'], str) or len(value['message']) > 512
            or type(value['count']) is not int or not 1 <= value['count'] < 2**31
            or any(type(value[k]) not in (int,float) for k in ('first','last'))):
        return
    now = time.monotonic()
    if not 0 <= value['first'] <= value['last'] <= now or now < getattr(parent, '_diagnostic_next', 0):
        return
    parent._diagnostic_next = now + 1
    if (getattr(parent, '_diagnostic_outage_since', None) is None
            and not (getattr(parent, '_has_pcm', False) and value['last'] <= parent._last_pcm)):
        parent._diagnostic_outage_since = value['first']
    log.warning('source=%s generation=%s category=%s reason=%s occurrences=%s span_s=%.3f',
                parent.source_id, parent.generation, value['category'], redact(value['message']),
                value['count'], value['last']-value['first'])


def audio_recovered(parent):
    since = getattr(parent, '_diagnostic_outage_since', None)
    if since is not None:
        parent._diagnostic_outage_since = None
        log.info('source=%s generation=%s category=audio_recovered outage_s=%.3f decoded PCM resumed',
                 parent.source_id, parent.generation, max(0, time.monotonic()-since))
