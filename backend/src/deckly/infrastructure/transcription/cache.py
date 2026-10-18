import logging
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from redis.asyncio import Redis

from deckly.application.exceptions import UpstreamUnavailableError
from deckly.infrastructure.observability.metrics import CacheLookup, Metrics
from deckly.infrastructure.rate_limit import bounded_redis
from deckly.infrastructure.transcription.client import Transcript, TranscriptSegment

logger = logging.getLogger(__name__)

KEY_PREFIX = "deckly:transcript:v1"


@dataclass(frozen=True, slots=True)
class TranscriptCacheLimits:
    ttl_seconds: int
    command_timeout_seconds: float


class TranscriptCache(Protocol):
    async def get(self, document_id: UUID) -> Transcript | None: ...

    async def put(self, document_id: UUID, transcript: Transcript) -> None: ...


class StoredSegment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    start_ms: int
    end_ms: int
    text: str


class StoredTranscript(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    language: str | None
    segments: tuple[StoredSegment, ...]


def transcript_key(document_id: UUID) -> str:
    return f"{KEY_PREFIX}:{document_id}"


def dump_transcript(transcript: Transcript) -> str:
    return StoredTranscript(
        language=transcript.language,
        segments=tuple(
            StoredSegment(start_ms=segment.start_ms, end_ms=segment.end_ms, text=segment.text)
            for segment in transcript.segments
        ),
    ).model_dump_json()


def parse_payload(payload: object) -> Transcript:
    if not isinstance(payload, bytes):
        message = f"redis answered GET with {type(payload).__name__}"
        raise TypeError(message)
    stored = StoredTranscript.model_validate_json(payload)
    return Transcript(
        segments=tuple(
            TranscriptSegment(start_ms=segment.start_ms, end_ms=segment.end_ms, text=segment.text)
            for segment in stored.segments
        ),
        language=stored.language,
    )


class RedisTranscriptCache:
    def __init__(self, redis: Redis, limits: TranscriptCacheLimits, metrics: Metrics) -> None:
        self._redis = redis
        self._limits = limits
        self._metrics = metrics

    async def get(self, document_id: UUID) -> Transcript | None:
        key = transcript_key(document_id)
        try:
            async with bounded_redis("transcript_cache_unavailable", self._limits.command_timeout_seconds):
                payload = await self._redis.get(key)
        except UpstreamUnavailableError:
            self._looked_up(CacheLookup.ERROR)
            raise
        if payload is None:
            self._looked_up(CacheLookup.MISS)
            return None
        try:
            transcript = parse_payload(payload)
        except (ValueError, TypeError):
            logger.warning("transcript_cache_corrupt", extra={"key": key})
            self._looked_up(CacheLookup.CORRUPT)
            return None
        self._looked_up(CacheLookup.HIT)
        return transcript

    async def put(self, document_id: UUID, transcript: Transcript) -> None:
        async with bounded_redis("transcript_cache_unavailable", self._limits.command_timeout_seconds):
            await self._redis.set(
                transcript_key(document_id), dump_transcript(transcript), ex=self._limits.ttl_seconds
            )

    def _looked_up(self, lookup: CacheLookup) -> None:
        self._metrics.transcript_cache_lookups.labels(result=lookup).inc()
        logger.info("transcript_cache_checked", extra={"result": lookup})
