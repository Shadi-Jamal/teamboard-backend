"""Job queue producer (Redis via arq). The consumer lives in ``teamboard.worker``."""

import asyncio
import uuid

from arq.connections import ArqRedis
from redis.exceptions import RedisError

from teamboard.errors import AppError

EXPORT_JOB = "run_export"
ENQUEUE_TIMEOUT_SECONDS = 2.0


class QueueUnavailableError(AppError):
    status = 503
    code = "queue_unavailable"
    title = "Service Unavailable"


class JobQueue:
    def __init__(self, redis: ArqRedis) -> None:
        self._redis = redis

    async def enqueue_export(self, tenant_id: uuid.UUID, job_id: uuid.UUID) -> None:
        """Queue an export for processing.

        The job carries only identifiers. ``_job_id`` makes the enqueue itself
        idempotent: arq ignores a second job with the same ID.
        """
        try:
            await asyncio.wait_for(
                self._redis.enqueue_job(
                    EXPORT_JOB, str(tenant_id), str(job_id), _job_id=f"export:{job_id}"
                ),
                timeout=ENQUEUE_TIMEOUT_SECONDS,
            )
        except (RedisError, TimeoutError, OSError) as exc:
            raise QueueUnavailableError(
                "The job queue is temporarily unavailable. Please retry."
            ) from exc
