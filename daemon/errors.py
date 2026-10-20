class DomainError(Exception):
    def __init__(self, status: int, error: str, message: str):
        super().__init__(message)
        self.status = status
        self.error = error
        self.message = message


class DependencyUnavailable(Exception):
    """A mockable control or media dependency could not perform an operation."""


class SourceUnavailable(DependencyUnavailable):
    """An external media input/decoder is unavailable, not a system failure."""


class SourcePending(SourceUnavailable):
    """An owned source is still preparing; retry without destroying its worker."""


class SourceStartupFailed(SourceUnavailable):
    """A retained LOCAL startup ended; explicit user action is needed to retry."""
