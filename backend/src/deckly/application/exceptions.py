class ApplicationError(Exception):
    pass


class IdempotencyKeyConflictError(ApplicationError):
    pass


class RetryableError(ApplicationError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(retry_after_seconds)
        self.retry_after_seconds = retry_after_seconds


class RateLimitedError(RetryableError):
    pass


class UpstreamUnavailableError(RetryableError):
    pass
