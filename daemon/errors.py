class DomainError(Exception):
    def __init__(self, status: int, error: str, message: str, *, headers: dict[str, str] | None = None,
                 rf_outcome: str | None = None, intent_revision: int | None = None):
        super().__init__(message)
        self.status = status
        self.error = error
        self.message = message
        self.headers = headers
        self.rf_outcome = rf_outcome
        self.intent_revision = intent_revision

    def payload(self):
        data = {"error": self.error, "message": self.message}
        if self.rf_outcome is not None:
            data['rf_outcome'] = self.rf_outcome
        if self.intent_revision is not None:
            data['intent_revision'] = self.intent_revision
        return data


class DependencyUnavailable(Exception):
    """A mockable control or media dependency could not perform an operation."""


class SourceUnavailable(DependencyUnavailable):
    """An external media input/decoder is unavailable, not a system failure."""


class SourcePending(SourceUnavailable):
    """An owned source is still preparing; retry without destroying its worker."""


class SourceStartupFailed(SourceUnavailable):
    """A retained LOCAL startup ended; explicit user action is needed to retry."""
