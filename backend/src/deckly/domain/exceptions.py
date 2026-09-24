class DomainError(Exception):
    pass


class NotFoundError(DomainError):
    pass


class ConflictError(DomainError):
    pass


class PolicyViolationError(DomainError):
    pass


class InvariantViolationError(DomainError):
    pass


class JobNotFoundError(NotFoundError):
    pass


class InvalidJobTransitionError(ConflictError):
    pass


class JobAlreadyTerminalError(InvalidJobTransitionError):
    pass


class TopicRejectedError(PolicyViolationError):
    pass


class InvalidJobIdError(InvariantViolationError):
    pass


class InvalidTimestampError(InvariantViolationError):
    pass


class InvalidProgressError(InvariantViolationError):
    pass


class ProgressRegressionError(InvariantViolationError):
    pass


class StageRegressionError(InvariantViolationError):
    pass


class InvalidNoteError(InvariantViolationError):
    pass


class InvalidClozeError(InvalidNoteError):
    pass


class DistractorMatchesAnswerError(InvalidNoteError):
    pass


class UnsupportedNoteTypeError(InvariantViolationError):
    pass


class InvalidMediaError(InvariantViolationError):
    pass


class InvalidSourceError(InvariantViolationError):
    pass


class InvalidDeckError(InvariantViolationError):
    pass


class DuplicateClientIdError(InvariantViolationError):
    pass
