import asyncio
import logging
from functools import partial
from http import HTTPStatus
from typing import Annotated, Self
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import UploadFile
from starlette.requests import ClientDisconnect
from starlette.types import Message, Receive

from deckly.application.documents import FetchArticle, FetchRecording, GetDocument, UploadDocument
from deckly.application.ports import DocumentUpload, Requester
from deckly.domain.article_url import MAX_ARTICLE_URL_LENGTH
from deckly.domain.document import Document, DocumentFormat
from deckly.domain.exceptions import DocumentNotFoundError, DocumentTooLargeError, InvalidDocumentError
from deckly.domain.recording_url import MAX_RECORDING_URL_LENGTH
from deckly.transport.body import ResponseBody, UtcDateTime, is_none
from deckly.transport.dependencies import (
    fetch_article_use_case,
    fetch_recording_use_case,
    get_document_use_case,
    upload_document_use_case,
    upload_recording_use_case,
)
from deckly.transport.generations import CLIENT_ID_HEADER, HYPHENATED_UUID, CanonicalUuid, address_of

logger = logging.getLogger(__name__)

DOCUMENT_FIELD = "file"
MULTIPART_OVERHEAD_BYTES = 16 * 1024
CONTENT_LENGTH_HEADER = "content-length"
REQUEST_MESSAGE = "http.request"
MESSAGE_TYPE = "type"
MESSAGE_BODY = "body"
MAX_UPLOAD_FILES = 1
MAX_UPLOAD_FIELDS = 0


class DocumentBody(ResponseBody):
    document_id: UUID = Field(alias="documentId")
    filename: str
    size_bytes: int = Field(alias="sizeBytes")
    content_type: DocumentFormat = Field(alias="contentType")
    uploaded_at: UtcDateTime = Field(alias="uploadedAt")
    duration_seconds: float | None = Field(default=None, alias="durationSeconds", exclude_if=is_none)
    source_url: str | None = Field(default=None, alias="sourceUrl", exclude_if=is_none)

    @classmethod
    def from_document(cls, document: Document) -> Self:
        return cls(
            document_id=document.document_id,
            filename=document.filename,
            size_bytes=document.size_bytes,
            content_type=document.document_format,
            uploaded_at=document.uploaded_at,
            duration_seconds=document.duration_seconds,
            source_url=document.source_url,
        )


class RecordingUrlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str = Field(min_length=1, max_length=MAX_RECORDING_URL_LENGTH)


class ArticleUrlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str = Field(min_length=1, max_length=MAX_ARTICLE_URL_LENGTH)


def body_limit(max_bytes: int) -> int:
    return max_bytes + MULTIPART_OVERHEAD_BYTES


def too_large(limit: int) -> DocumentTooLargeError:
    return DocumentTooLargeError(f"the request body is over {limit} bytes")


def reject_declared_oversize(request: Request, limit: int) -> None:
    declared = request.headers.get(CONTENT_LENGTH_HEADER)
    if declared is not None and declared.isascii() and declared.isdigit() and int(declared) > limit:
        raise too_large(limit)


def capped(receive: Receive, limit: int) -> Receive:
    received = 0

    async def capped_receive() -> Message:
        nonlocal received
        message = await receive()
        if message[MESSAGE_TYPE] == REQUEST_MESSAGE:
            received += len(message.get(MESSAGE_BODY, b""))
            if received > limit:
                raise too_large(limit)
        return message

    return capped_receive


async def read_upload(request: Request, limit: int, timeout_seconds: float) -> DocumentUpload:
    capped_request = Request(request.scope, receive=capped(request.receive, limit))
    try:
        async with asyncio.timeout(timeout_seconds):
            async with capped_request.form(max_files=MAX_UPLOAD_FILES, max_fields=MAX_UPLOAD_FIELDS) as form:
                upload = form.get(DOCUMENT_FIELD)
                if not isinstance(upload, UploadFile):
                    message = f"the request must carry exactly one file in the {DOCUMENT_FIELD!r} field"
                    raise InvalidDocumentError(message)
                return DocumentUpload(filename=upload.filename or "", content=await upload.read())
    except ClientDisconnect as error:
        message = "the client disconnected before the upload finished"
        raise InvalidDocumentError(message) from error
    except TimeoutError as error:
        logger.warning("upload_read_timed_out", extra={"timeout_seconds": timeout_seconds})
        message = f"the upload was not received within {timeout_seconds} seconds"
        raise InvalidDocumentError(message) from error


def parse_document_id(value: str) -> UUID:
    if HYPHENATED_UUID.fullmatch(value) is None:
        message = f"{value!r} is not a document id"
        raise DocumentNotFoundError(message)
    return UUID(value)


router = APIRouter()


async def receive_upload(
    request: Request, client_id: UUID, address: str, upload: UploadDocument
) -> DocumentBody:
    limit = body_limit(upload.max_bytes)
    reject_declared_oversize(request, limit)
    requester = Requester(client_id=client_id, address=address)
    return DocumentBody.from_document(
        await upload(requester, partial(read_upload, request, limit, upload.read_timeout_seconds))
    )


@router.post("/documents", status_code=HTTPStatus.CREATED)
async def upload_document(
    request: Request,
    client_id: Annotated[CanonicalUuid, Header(alias=CLIENT_ID_HEADER)],
    address: Annotated[str, Depends(address_of)],
    upload: Annotated[UploadDocument, Depends(upload_document_use_case)],
) -> DocumentBody:
    return await receive_upload(request, client_id, address, upload)


@router.post("/recordings", status_code=HTTPStatus.CREATED)
async def upload_recording(
    request: Request,
    client_id: Annotated[CanonicalUuid, Header(alias=CLIENT_ID_HEADER)],
    address: Annotated[str, Depends(address_of)],
    upload: Annotated[UploadDocument, Depends(upload_recording_use_case)],
) -> DocumentBody:
    return await receive_upload(request, client_id, address, upload)


@router.post("/recordings/from-url", status_code=HTTPStatus.CREATED)
async def fetch_recording(
    body: RecordingUrlRequest,
    client_id: Annotated[CanonicalUuid, Header(alias=CLIENT_ID_HEADER)],
    address: Annotated[str, Depends(address_of)],
    fetch: Annotated[FetchRecording, Depends(fetch_recording_use_case)],
) -> DocumentBody:
    requester = Requester(client_id=client_id, address=address)
    return DocumentBody.from_document(await fetch(requester, body.url))


@router.post("/documents/from-url", status_code=HTTPStatus.CREATED)
async def fetch_article(
    body: ArticleUrlRequest,
    client_id: Annotated[CanonicalUuid, Header(alias=CLIENT_ID_HEADER)],
    address: Annotated[str, Depends(address_of)],
    fetch: Annotated[FetchArticle, Depends(fetch_article_use_case)],
) -> DocumentBody:
    requester = Requester(client_id=client_id, address=address)
    return DocumentBody.from_document(await fetch(requester, body.url))


@router.get("/documents/{document_id}")
async def get_document(
    document_id: str,
    client_id: Annotated[CanonicalUuid, Header(alias=CLIENT_ID_HEADER)],
    get: Annotated[GetDocument, Depends(get_document_use_case)],
) -> DocumentBody:
    return DocumentBody.from_document(await get(parse_document_id(document_id), client_id))
