"""Python-only Radio Browser handoff across the bounded control channel."""
from collections import deque
from threading import Lock
from uuid import uuid4
from .audio_ipc import ProtocolError


class RemoteRadio:
    def __init__(self):
        self.lock = Lock()
        self.revision = 0
        self.eligible = False
        self.trial = None
        self.token = self.wire = None
        self.commands = deque()

    def observe(self):
        with self.lock:
            return self.eligible, dict(self.trial) if self.trial else None

    def offer(self, token, uri):
        with self.lock:
            if not self.eligible or self.trial:
                return False
            self.token, self.wire = token, uuid4().hex
            self.trial = dict(token=token, uri=uri, state='queued')
            self._command('offer', uri)
            return True

    def _command(self, action, uri=None):
        if len(self.commands) >= 8:
            raise ProtocolError('Radio handoff queue full')
        self.revision += 1
        self.commands.append(dict(revision=self.revision, action=action, token=self.wire, uri=uri))

    def accept(self, token):
        self._finish(token, 'accept')

    def discard(self, token):
        self._finish(token, 'discard')

    def _finish(self, token, action):
        with self.lock:
            if token is self.token:
                self._command(action)
                self.token = self.wire = self.trial = None
                self.eligible = False

    def drain(self):
        with self.lock:
            result = list(self.commands)
            self.commands.clear()
            return result

    def update(self, value):
        if (set(value) != {'revision', 'eligible', 'trial'} or type(value['revision']) is not int
                or type(value['eligible']) is not bool):
            raise ProtocolError('Invalid Radio Browser observation')
        trial = value['trial']
        if trial is not None and (not isinstance(trial, dict) or set(trial) != {'token','uri','state'}
                or trial['state'] not in ('queued','trying','pcm','failed','obsolete')
                or not isinstance(trial['uri'],str) or len(trial['uri']) > 8192):
            raise ProtocolError('Invalid Radio Browser trial')
        with self.lock:
            if value['revision'] != self.revision:
                return
            self.eligible = value['eligible']
            if trial is not None and trial['token'] == self.wire and self.token is not None:
                self.trial = dict(trial, token=self.token)
            elif trial is None and self.trial is not None:
                self.trial = dict(self.trial, state='failed')


def radio_command(pipeline, value):
    from ..radio_browser import http_url
    if (set(value) != {'revision','action','token','uri'} or type(value['revision']) is not int
            or value['revision'] != getattr(pipeline, '_radio_revision', 0) + 1
            or not isinstance(value['token'], str) or len(value['token']) != 32
            or value['action'] not in ('offer','accept','discard') or pipeline.radio_url is None):
        raise ProtocolError('Invalid radio command')
    radio = pipeline.radio_url
    if value['action'] == 'offer':
        if not http_url(value['uri']):
            raise ProtocolError('Invalid radio URL')
        pipeline._radio_token = value['token']
        radio.offer(pipeline._radio_token, value['uri'])
    else:
        # Preserve HttpUrlRecovery's identity comparison after JSON decoding.
        token = getattr(pipeline, '_radio_token', None)
        if token == value['token']:
            getattr(radio, value['action'])(token)
    pipeline._radio_revision = value['revision']
