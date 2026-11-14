"""Redis connection used for rate limiting, quotas and the job queue."""

from arq.connections import ArqRedis, RedisSettings, create_pool

from teamboard.config import Settings


def redis_settings(settings: Settings) -> RedisSettings:
    return RedisSettings.from_dsn(settings.redis_url)


async def create_redis(settings: Settings) -> ArqRedis:
    """Create the shared connection pool (an arq pool is a regular Redis client
    that can also enqueue jobs)."""
    return await create_pool(redis_settings(settings))
