class DomainError(Exception):
    def __init__(self, status: int, error: str, message: str):
        super().__init__(message)
        self.status = status
        self.error = error
        self.message = message


class DependencyUnavailable(Exception):
    """A mockable control or media dependency could not perform an operation."""
