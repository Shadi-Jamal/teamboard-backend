"""Dependency checks behind the readiness probe."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Literal

from arq.connections import ArqRedis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

CheckResult = Literal["ok", "failed"]

_CHECK_TIMEOUT_SECONDS = 2.0


class HealthService:
    def __init__(self, engine: AsyncEngine, redis: ArqRedis) -> None:
        self._engine = engine
        self._redis = redis

    async def check_dependencies(self) -> dict[str, CheckResult]:
        checks: dict[str, Callable[[], Awaitable[None]]] = {
            "database": self._check_database,
            "redis": self._check_redis,
        }
        results: dict[str, CheckResult] = {}
        for name, check in checks.items():
            try:
                await asyncio.wait_for(check(), timeout=_CHECK_TIMEOUT_SECONDS)
                results[name] = "ok"
            except Exception:
                logger.warning("readiness check failed", extra={"check": name}, exc_info=True)
                results[name] = "failed"
        return results

    async def _check_database(self) -> None:
        async with self._engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def _check_redis(self) -> None:
        await self._redis.ping()
