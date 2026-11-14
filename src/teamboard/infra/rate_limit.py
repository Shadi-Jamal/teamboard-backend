"""Redis-backed rate limiting and usage counters.

Both primitives are single Lua scripts, so check-and-update is atomic across any
number of API instances.

Failure policy: if Redis is unavailable *or slow* these helpers **fail open** (the
request is allowed and a warning is logged). Losing throttling for a while is
preferable to a Redis outage taking down every API call; readiness still reports
Redis as down. Every call is bounded by ``REDIS_TIMEOUT_SECONDS`` so that a Redis
that accepts connections but does not answer cannot stall requests either.
"""

import asyncio
import logging
import math
from collections.abc import Awaitable
from dataclasses import dataclass

from arq.connections import ArqRedis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

REDIS_TIMEOUT_SECONDS = 1.0

# Everything that means "Redis did not answer (in time)".
_UNAVAILABLE = (RedisError, TimeoutError, OSError)


async def _bounded[T](operation: Awaitable[T]) -> T:
    return await asyncio.wait_for(operation, timeout=REDIS_TIMEOUT_SECONDS)


# Token bucket. A bucket holds up to `capacity` tokens and refills continuously at
# `capacity / period` tokens per second; each request takes one token. Time comes
# from the Redis server so that application hosts need not have synchronised clocks.
_BUCKET_SCRIPT = """
local capacity = tonumber(ARGV[1])
local refill_per_second = capacity / tonumber(ARGV[2])
local time = redis.call('TIME')
local now = tonumber(time[1]) + tonumber(time[2]) / 1000000

local state = redis.call('HMGET', KEYS[1], 'tokens', 'updated_at')
local tokens = tonumber(state[1])
local updated_at = tonumber(state[2])
if tokens == nil then
    tokens = capacity
    updated_at = now
end
tokens = math.min(capacity, tokens + math.max(0, now - updated_at) * refill_per_second)

local allowed = 0
local retry_after = 0
if tokens >= 1 then
    tokens = tokens - 1
    allowed = 1
else
    retry_after = (1 - tokens) / refill_per_second
end

redis.call('HSET', KEYS[1], 'tokens', tokens, 'updated_at', now)
redis.call('EXPIRE', KEYS[1], math.ceil(tonumber(ARGV[2])) + 1)
return {allowed, math.floor(tokens), tostring(retry_after)}
"""

# Counter with a ceiling: increments only while below `limit`. The TTL is set when
# the counter is created, which makes it a fixed window.
_COUNTER_SCRIPT = """
local limit = tonumber(ARGV[1])
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if current >= limit then
    return {0, current}
end
current = redis.call('INCR', KEYS[1])
if current == 1 then
    redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
end
return {1, current}
"""


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int  # seconds until a request will be allowed again (0 if allowed)


@dataclass(frozen=True, slots=True)
class CounterResult:
    allowed: bool
    used: int


class RateLimiter:
    def __init__(self, redis: ArqRedis) -> None:
        self._script = redis.register_script(_BUCKET_SCRIPT)

    async def consume(self, key: str, *, limit: int, period_seconds: int = 60) -> RateLimitResult:
        """Take one token from the bucket ``key`` allowing ``limit`` requests per period."""
        try:
            allowed, remaining, retry_after = await _bounded(
                self._script(keys=[f"ratelimit:{key}"], args=[limit, period_seconds])
            )
        except _UNAVAILABLE:
            logger.warning("rate limiter unavailable, allowing request", exc_info=True)
            return RateLimitResult(allowed=True, limit=limit, remaining=limit, retry_after=0)
        return RateLimitResult(
            allowed=bool(allowed),
            limit=limit,
            remaining=int(remaining),
            retry_after=math.ceil(float(retry_after)),
        )


class UsageCounter:
    """Fixed-window counters, e.g. "exports today" or "failed logins for this email"."""

    def __init__(self, redis: ArqRedis) -> None:
        self._redis = redis
        self._script = redis.register_script(_COUNTER_SCRIPT)

    async def increment(self, key: str, *, limit: int, window_seconds: int) -> CounterResult:
        """Count one use unless ``limit`` has been reached within the window."""
        try:
            allowed, used = await _bounded(
                self._script(keys=[f"usage:{key}"], args=[limit, window_seconds])
            )
        except _UNAVAILABLE:
            logger.warning("usage counter unavailable, allowing request", exc_info=True)
            return CounterResult(allowed=True, used=0)
        return CounterResult(allowed=bool(allowed), used=int(used))

    async def refund(self, key: str) -> None:
        """Give back one use (the counted operation did not happen after all)."""
        try:
            await _bounded(self._redis.decr(f"usage:{key}"))
        except _UNAVAILABLE:
            logger.warning("usage counter unavailable, refund skipped", exc_info=True)

    async def current(self, key: str) -> int:
        try:
            value = await _bounded(self._redis.get(f"usage:{key}"))
        except _UNAVAILABLE:
            logger.warning("usage counter unavailable", exc_info=True)
            return 0
        return max(0, int(value)) if value is not None else 0

    async def ttl(self, key: str) -> int:
        """Seconds until the window resets (at least 1)."""
        try:
            value = int(await _bounded(self._redis.ttl(f"usage:{key}")))
        except _UNAVAILABLE:
            return 1
        return max(1, value)

    async def reset(self, key: str) -> None:
        try:
            await _bounded(self._redis.delete(f"usage:{key}"))
        except _UNAVAILABLE:
            logger.warning("usage counter unavailable, reset skipped", exc_info=True)
