from fastapi import Request

from deckly.application.generations import CancelGeneration, CreateGeneration, GetGeneration
from deckly.application.health import CheckHealth
from deckly.application.regeneration import RegenerateNote
from deckly.transport.error_handlers import ProblemResponder


def wired[T](request: Request, name: str, kind: type[T]) -> T:
    wired_object: object = getattr(request.app.state, name, None)
    if not isinstance(wired_object, kind):
        message = f"{name} is not wired into the application state"
        raise TypeError(message)
    return wired_object


def create_generation_use_case(request: Request) -> CreateGeneration:
    return wired(request, "create_generation", CreateGeneration)


def get_generation_use_case(request: Request) -> GetGeneration:
    return wired(request, "get_generation", GetGeneration)


def cancel_generation_use_case(request: Request) -> CancelGeneration:
    return wired(request, "cancel_generation", CancelGeneration)


def regenerate_note_use_case(request: Request) -> RegenerateNote:
    return wired(request, "regenerate_note", RegenerateNote)


def check_health_use_case(request: Request) -> CheckHealth:
    return wired(request, "check_health", CheckHealth)


def problem_responder(request: Request) -> ProblemResponder:
    return wired(request, "problem_responder", ProblemResponder)
