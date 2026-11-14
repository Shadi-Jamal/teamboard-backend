"""Redis connection used for rate limiting, quotas and the job queue."""

import json
from typing import Any

from arq.connections import ArqRedis, RedisSettings, create_pool

from teamboard.config import Settings


def redis_settings(settings: Settings) -> RedisSettings:
    return RedisSettings.from_dsn(settings.redis_url.get_secret_value())


def serialize_job(data: dict[str, Any]) -> bytes:
    """Queue payloads are JSON, not arq's default pickle.

    Unpickling executes code, so with pickle anyone able to write to Redis could
    run arbitrary code inside the worker. Jobs here carry only identifiers, which
    JSON represents fine. ``default=repr`` keeps job *results* serializable even
    when a job ends with an exception object.
    """
    return json.dumps(data, default=repr).encode()


def deserialize_job(data: bytes) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(data)
    return payload


async def create_redis(settings: Settings) -> ArqRedis:
    """Create the shared connection pool (an arq pool is a regular Redis client
    that can also enqueue jobs)."""
    return await create_pool(
        redis_settings(settings),
        job_serializer=serialize_job,
        job_deserializer=deserialize_job,
    )
