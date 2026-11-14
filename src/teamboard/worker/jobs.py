"""Job definitions and worker construction.

Retry policy for exports
------------------------
An attempt that raises is recorded on the job row and retried with exponential
backoff (``export_retry_base_seconds * 2**(attempt - 1)``). After
``export_max_attempts`` attempts the job is marked ``failed`` for good. The job row
is always left in a truthful state - ``queued`` while a retry is pending, ``failed``
when none will follow - so API clients polling the export never see a job stuck in
``running`` because of an error.
"""

import logging
import uuid
from typing import Any

from arq import Retry
from arq.worker import Worker, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from teamboard.config import Settings
from teamboard.context import tenant_id_var
from teamboard.db.engine import assert_role_is_subject_to_rls, create_engine, create_sessionmaker
from teamboard.db.rls import set_rls_context
from teamboard.infra.queue import EXPORT_JOB
from teamboard.infra.redis import redis_settings
from teamboard.services.exports import ExportProcessor

logger = logging.getLogger(__name__)

# How long to wait for a job row that is not visible yet (the API enqueues just
# before it commits).
_MISSING_ROW_RETRY_SECONDS = 1.0


async def startup(ctx: dict[str, Any]) -> None:
    settings: Settings = ctx["settings"]
    engine = create_engine(settings, application_name="teamboard-worker")
    # The worker is subject to row-level security exactly like the API.
    await assert_role_is_subject_to_rls(engine)
    ctx["engine"] = engine
    ctx["sessionmaker"] = create_sessionmaker(engine)


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["engine"].dispose()


async def run_export(ctx: dict[str, Any], tenant_id: str, job_id: str) -> str:
    """Build one export. Returns a short outcome label for the worker log."""
    settings: Settings = ctx["settings"]
    sessionmaker: async_sessionmaker[AsyncSession] = ctx["sessionmaker"]
    attempt: int = ctx["job_try"]
    final_attempt = attempt >= settings.export_max_attempts
    tenant, job = uuid.UUID(tenant_id), uuid.UUID(job_id)

    tenant_id_var.set(tenant_id)
    async with sessionmaker() as session:
        # The job only names a tenant; the session is scoped to it before any query,
        # so the export can read that tenant's rows and nothing else.
        await set_rls_context(session, tenant_id=tenant)
        processor = ExportProcessor(session, tenant)
        try:
            found = await processor.process(job)
        except Exception as exc:
            await session.rollback()
            logger.warning(
                "export attempt failed",
                extra={"job_id": job_id, "attempt": attempt, "final": final_attempt},
                exc_info=True,
            )
            await processor.record_failure(job, f"{type(exc).__name__}: {exc}", final=final_attempt)
            if final_attempt:
                return "failed"
            delay = settings.export_retry_base_seconds * 2 ** (attempt - 1)
            raise Retry(defer=delay) from exc

    if found:
        return "succeeded"
    if final_attempt:
        logger.error("export job row never appeared, giving up", extra={"job_id": job_id})
        return "missing"
    raise Retry(defer=_MISSING_ROW_RETRY_SECONDS)


def create_worker(settings: Settings, **overrides: Any) -> Worker:
    """Build the arq worker. ``overrides`` are passed to ``arq.Worker`` (tests use
    ``burst=True`` to process the queue once and return)."""
    options: dict[str, Any] = {
        "redis_settings": redis_settings(settings),
        "on_startup": startup,
        "on_shutdown": shutdown,
        "ctx": {"settings": settings},
        "job_timeout": 300,
        "max_jobs": 10,
    }
    options.update(overrides)
    return Worker(
        functions=[func(run_export, name=EXPORT_JOB, max_tries=settings.export_max_attempts)],
        **options,
    )
