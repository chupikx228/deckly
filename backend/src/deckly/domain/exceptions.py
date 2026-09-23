class DomainError(Exception):
    pass


class NotFoundError(DomainError):
    pass


class ConflictError(DomainError):
    pass


class PolicyViolationError(DomainError):
    pass


class JobNotFoundError(NotFoundError):
    pass


class JobAlreadyTerminalError(ConflictError):
    pass


class TopicRejectedError(PolicyViolationError):
    pass
