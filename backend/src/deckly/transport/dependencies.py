from fastapi import Request

from deckly.application.documents import FetchArticle, FetchRecording, GetDocument, UploadDocument
from deckly.application.generations import CancelGeneration, CreateGeneration, GetGeneration
from deckly.application.health import CheckHealth
from deckly.application.regeneration import RegenerateNote
from deckly.transport.error_handlers import ProblemResponder

CREATE_GENERATION_STATE = "create_generation"
GET_GENERATION_STATE = "get_generation"
CANCEL_GENERATION_STATE = "cancel_generation"
REGENERATE_NOTE_STATE = "regenerate_note"
CHECK_HEALTH_STATE = "check_health"
UPLOAD_DOCUMENT_STATE = "upload_document"
UPLOAD_RECORDING_STATE = "upload_recording"
FETCH_RECORDING_STATE = "fetch_recording"
FETCH_ARTICLE_STATE = "fetch_article"
GET_DOCUMENT_STATE = "get_document"


def wired[T](request: Request, name: str, kind: type[T]) -> T:
    wired_object: object = getattr(request.app.state, name, None)
    if not isinstance(wired_object, kind):
        message = f"{name} is not wired into the application state"
        raise TypeError(message)
    return wired_object


def create_generation_use_case(request: Request) -> CreateGeneration:
    return wired(request, CREATE_GENERATION_STATE, CreateGeneration)


def get_generation_use_case(request: Request) -> GetGeneration:
    return wired(request, GET_GENERATION_STATE, GetGeneration)


def cancel_generation_use_case(request: Request) -> CancelGeneration:
    return wired(request, CANCEL_GENERATION_STATE, CancelGeneration)


def regenerate_note_use_case(request: Request) -> RegenerateNote:
    return wired(request, REGENERATE_NOTE_STATE, RegenerateNote)


def check_health_use_case(request: Request) -> CheckHealth:
    return wired(request, CHECK_HEALTH_STATE, CheckHealth)


def upload_document_use_case(request: Request) -> UploadDocument:
    return wired(request, UPLOAD_DOCUMENT_STATE, UploadDocument)


def upload_recording_use_case(request: Request) -> UploadDocument:
    return wired(request, UPLOAD_RECORDING_STATE, UploadDocument)


def fetch_recording_use_case(request: Request) -> FetchRecording:
    return wired(request, FETCH_RECORDING_STATE, FetchRecording)


def fetch_article_use_case(request: Request) -> FetchArticle:
    return wired(request, FETCH_ARTICLE_STATE, FetchArticle)


def get_document_use_case(request: Request) -> GetDocument:
    return wired(request, GET_DOCUMENT_STATE, GetDocument)


def problem_responder(request: Request) -> ProblemResponder:
    return wired(request, "problem_responder", ProblemResponder)
