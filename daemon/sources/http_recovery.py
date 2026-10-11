"""Small handoff between the existing HTTP retry worker and control owner.

No networking, database access, or extra playback loop lives here. All PCM
notifications originate on the Source worker after validated PCM is drained.
"""
from threading import Lock

PERSISTENT_ATTEMPTS = 3


class HttpUrlRecovery:
    def __init__(self, uri):
        self.lock = Lock()
        self.uri = uri
        self.attempt_uri = uri
        self.eligible = False
        self.trial = None
        self.attempt_token = None

    def observe(self):
        with self.lock:
            return self.eligible, dict(self.trial) if self.trial else None

    def offer(self, token, uri):
        with self.lock:
            if not self.eligible or self.trial is not None:
                return False
            self.trial = dict(token=token, uri=uri, state='queued')
            return True

    def begin_attempt(self):
        with self.lock:
            self.attempt_token = None
            if self.trial and self.trial['state'] == 'queued':
                self.attempt_token = self.trial['token']
                self.trial['state'] = 'trying'
                self.attempt_uri = self.trial['uri']
            else:
                self.attempt_uri = self.uri
            return self.attempt_uri

    def cancelled(self):
        with self.lock:
            if self.trial is None and self.attempt_uri != self.uri:
                return True  # A commit raced the start of an old-URL retry.
            return self.attempt_token is not None and (
                self.trial is None or self.trial['token'] is not self.attempt_token)

    def pcm(self):
        with self.lock:
            self.eligible = False
            if self.trial:
                if self.trial['token'] is self.attempt_token:
                    self.trial['state'] = 'pcm'
                elif self.trial['state'] == 'queued':
                    self.trial['state'] = 'obsolete'  # Original URL recovered itself.

    def failed(self):
        with self.lock:
            if self.trial and self.trial['token'] is self.attempt_token:
                self.trial['state'] = 'failed'
            self.attempt_token = None

    def retry(self, backoff_attempt):
        with self.lock:
            self.eligible = backoff_attempt >= PERSISTENT_ATTEMPTS

    def accept(self, token):
        with self.lock:
            if self.trial and self.trial['token'] is token:
                # PCM was observed before the atomic write. If it failed again
                # meanwhile, ordinary retries now correctly use the persisted URI.
                self.uri = self.trial['uri']
                self.trial = None
                self.attempt_token = None
                self.eligible = False

    def discard(self, token):
        with self.lock:
            if self.trial and self.trial['token'] is token:
                self.trial = None
                # Keep attempt_token until the worker has torn down that attempt.
                # Its next loop observes cancellation and resumes the saved URI.
