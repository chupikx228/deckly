import hashlib
import json
import logging
from dataclasses import asdict, dataclass

from redis.asyncio import Redis

from deckly.application.exceptions import UpstreamUnavailableError
from deckly.domain.deck import GenerationResult
from deckly.domain.generation import (
    DocumentGenerationFingerprint,
    Fingerprint,
    GenerationFingerprint,
    RecordingGenerationFingerprint,
)
from deckly.infrastructure.observability.metrics import CacheLookup, Metrics
from deckly.infrastructure.rate_limit import bounded_redis
from deckly.infrastructure.stored_result import dump_result, load_result

logger = logging.getLogger(__name__)

KEY_PREFIX = "deckly:generation-result:v2"
DOCUMENT_KEY_PREFIX = "deckly:generation-result:document:v1"
RECORDING_KEY_PREFIX = "deckly:generation-result:recording:v1"


@dataclass(frozen=True, slots=True)
class ResultCacheLimits:
    ttl_seconds: int
    command_timeout_seconds: float


def key_prefix(fingerprint: Fingerprint) -> str:
    match fingerprint:
        case GenerationFingerprint():
            return KEY_PREFIX
        case DocumentGenerationFingerprint():
            return DOCUMENT_KEY_PREFIX
        case RecordingGenerationFingerprint():
            return RECORDING_KEY_PREFIX


def cache_key(fingerprint: Fingerprint) -> str:
    canonical = json.dumps(asdict(fingerprint), sort_keys=True, separators=(",", ":"), default=str)
    return f"{key_prefix(fingerprint)}:{hashlib.sha256(canonical.encode()).hexdigest()}"


def parse_payload(payload: object) -> GenerationResult:
    if not isinstance(payload, bytes):
        message = f"redis answered GET with {type(payload).__name__}"
        raise TypeError(message)
    return load_result(json.loads(payload))


class RedisResultCache:
    def __init__(self, redis: Redis, limits: ResultCacheLimits, metrics: Metrics) -> None:
        self._redis = redis
        self._limits = limits
        self._metrics = metrics

    async def get(self, fingerprint: Fingerprint) -> GenerationResult | None:
        key = cache_key(fingerprint)
        try:
            async with bounded_redis(
                "generation_result_cache_unavailable", self._limits.command_timeout_seconds
            ):
                payload = await self._redis.get(key)
        except UpstreamUnavailableError:
            self._looked_up(CacheLookup.ERROR)
            raise
        if payload is None:
            self._looked_up(CacheLookup.MISS)
            return None
        try:
            result = parse_payload(payload)
        except (ValueError, TypeError):
            logger.warning("generation_result_cache_corrupt", extra={"key": key})
            self._looked_up(CacheLookup.CORRUPT)
            return None
        self._looked_up(CacheLookup.HIT)
        return result

    async def put(self, fingerprint: Fingerprint, result: GenerationResult) -> None:
        key = cache_key(fingerprint)
        document = json.dumps(dump_result(result), separators=(",", ":"))
        async with bounded_redis("generation_result_cache_unavailable", self._limits.command_timeout_seconds):
            await self._redis.set(key, document, ex=self._limits.ttl_seconds)

    def _looked_up(self, lookup: CacheLookup) -> None:
        self._metrics.cache_lookups.labels(result=lookup).inc()
        logger.info("generation_cache_checked", extra={"result": lookup})
