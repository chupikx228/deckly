import hashlib
import json
import logging
from dataclasses import asdict, dataclass

from redis.asyncio import Redis

from deckly.domain.deck import GenerationResult
from deckly.domain.generation import GenerationFingerprint, GenerationRequest
from deckly.infrastructure.rate_limit import bounded_redis
from deckly.infrastructure.stored_result import dump_result, load_result

logger = logging.getLogger(__name__)

KEY_PREFIX = "deckly:generation-result:v1"


@dataclass(frozen=True, slots=True)
class ResultCacheLimits:
    ttl_seconds: int
    command_timeout_seconds: float


def cache_key(fingerprint: GenerationFingerprint) -> str:
    canonical = json.dumps(asdict(fingerprint), sort_keys=True, separators=(",", ":"))
    return f"{KEY_PREFIX}:{hashlib.sha256(canonical.encode()).hexdigest()}"


def parse_payload(payload: object) -> GenerationResult:
    if not isinstance(payload, bytes):
        message = f"redis answered GET with {type(payload).__name__}"
        raise TypeError(message)
    return load_result(json.loads(payload))


class RedisResultCache:
    def __init__(self, redis: Redis, limits: ResultCacheLimits) -> None:
        self._redis = redis
        self._limits = limits

    async def get(self, request: GenerationRequest) -> GenerationResult | None:
        key = cache_key(request.fingerprint())
        async with bounded_redis("generation_result_cache_unavailable", self._limits.command_timeout_seconds):
            payload = await self._redis.get(key)
        if payload is None:
            return None
        try:
            return parse_payload(payload)
        except (ValueError, TypeError):
            logger.warning("generation_result_cache_corrupt", extra={"key": key})
            return None

    async def put(self, request: GenerationRequest, result: GenerationResult) -> None:
        key = cache_key(request.fingerprint())
        document = json.dumps(dump_result(result), separators=(",", ":"))
        async with bounded_redis("generation_result_cache_unavailable", self._limits.command_timeout_seconds):
            await self._redis.set(key, document, ex=self._limits.ttl_seconds)
