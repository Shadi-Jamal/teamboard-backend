"""Asynchronous data export.

``ExportService`` is the API side: it records a job and queues it.
``ExportProcessor`` is the worker side: it builds the export document.

The job row in PostgreSQL is the source of truth for status; Redis only transports
"please work on job X of tenant Y". Delivery is at-least-once, so the processor is
idempotent: running it again for a finished job does nothing.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from teamboard.db.base import utcnow
from teamboard.db.models import ExportJob
from teamboard.domain import Actor, ActorType, ExportStatus, TenantContext
from teamboard.errors import ConflictError, NotFoundError, RateLimitedError
from teamboard.infra.queue import JobQueue
from teamboard.infra.rate_limit import UsageCounter
from teamboard.pagination import Page, PageParams
from teamboard.repositories.exports import ExportJobRepository
from teamboard.repositories.projects import ProjectRepository
from teamboard.repositories.tasks import TaskRepository
from teamboard.schemas.work import ExportDocument, ExportedProject, ExportRead, TaskRead
from teamboard.services.audit import AuditRecorder
from teamboard.services.idempotency import IdempotencyService, Outcome
from teamboard.services.tenants import exports_usage_key

_MAX_ERROR_LENGTH = 2000
# The daily counter key contains the date, so its TTL only has to outlive the day.
_COUNTER_TTL_SECONDS = 2 * 24 * 3600


def _not_found() -> NotFoundError:
    return NotFoundError("Export not found.")


def _seconds_until_utc_midnight(now: datetime) -> int:
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((tomorrow - now).total_seconds()) + 1


class ExportService:
    def __init__(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        idempotency: IdempotencyService,
        queue: JobQueue,
        counter: UsageCounter,
    ) -> None:
        self._session = session
        self._ctx = ctx
        self._idempotency = idempotency
        self._queue = queue
        self._counter = counter
        self._jobs = ExportJobRepository(session, ctx.tenant_id)
        self._audit = AuditRecorder(session, ctx.tenant_id, ctx.actor)

    async def create(self, idempotency_key: str | None) -> Outcome[ExportRead]:
        """Request an export of the tenant's projects and tasks."""
        usage_key = exports_usage_key(self._ctx.tenant_id)
        counted = False

        async def operation() -> ExportJob:
            nonlocal counted
            quota = await self._counter.increment(
                usage_key,
                limit=self._ctx.limits.exports_per_day,
                window_seconds=_COUNTER_TTL_SECONDS,
            )
            if not quota.allowed:
                raise RateLimitedError(
                    "The daily export quota of this tenant's plan is used up.",
                    code="export_quota_exceeded",
                    retry_after=_seconds_until_utc_midnight(datetime.now(UTC)),
                )
            counted = True

            job = self._jobs.add(ExportJob(id=uuid.uuid4()))
            self._audit.record("export.requested", "export", job.id)
            await self._jobs.flush()
            # Enqueue *before* commit. If queueing fails the request fails and the
            # row is rolled back, so a committed job is never left without a queue
            # entry. The reverse - a queue entry whose row is not committed yet, or
            # never - is handled by the worker, which retries and then gives up.
            await self._queue.enqueue_export(self._ctx.tenant_id, job.id)
            return job

        try:
            return await self._idempotency.run(
                idempotency_key, "export.create", {}, operation, ExportRead, status_code=202
            )
        except BaseException:
            if counted:
                await self._counter.refund(usage_key)
            raise

    async def list_exports(self, page: PageParams) -> Page[ExportJob]:
        return await self._jobs.list_page(page)

    async def get(self, job_id: uuid.UUID) -> ExportJob:
        job = await self._jobs.get(job_id)
        if job is None:
            raise _not_found()
        return job

    async def download(self, job_id: uuid.UUID) -> dict[str, Any]:
        job = await self._jobs.get(job_id, with_result=True)
        if job is None:
            raise _not_found()
        if job.status is not ExportStatus.SUCCEEDED or job.result is None:
            raise ConflictError(
                f"The export is not available (status: {job.status.value}).",
                code="export_not_ready",
            )
        return job.result


async def build_export_document(
    session: AsyncSession, tenant_id: uuid.UUID, job_id: uuid.UUID
) -> dict[str, Any]:
    """Collect the tenant's live projects and tasks into one JSON document."""
    projects = await ProjectRepository(session, tenant_id).list_live()
    tasks_by_project: dict[uuid.UUID, list[TaskRead]] = {}
    for task in await TaskRepository(session, tenant_id).list_live():
        tasks_by_project.setdefault(task.project_id, []).append(TaskRead.model_validate(task))

    document = ExportDocument(
        export_id=job_id,
        tenant_id=tenant_id,
        generated_at=utcnow(),
        projects=[
            ExportedProject(
                id=project.id,
                name=project.name,
                description=project.description,
                status=project.status,
                created_at=project.created_at,
                updated_at=project.updated_at,
                tasks=tasks_by_project.get(project.id, []),
            )
            for project in projects
        ],
    )
    return document.model_dump(mode="json")


class ExportProcessor:
    """Runs one export job inside the worker. The session must already be scoped
    to the job's tenant."""

    def __init__(self, session: AsyncSession, tenant_id: uuid.UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._jobs = ExportJobRepository(session, tenant_id)
        self._audit = AuditRecorder(session, tenant_id, Actor(ActorType.SYSTEM, None))

    async def process(self, job_id: uuid.UUID) -> bool:
        """Build the export. Returns False if the job row does not exist (yet)."""
        job = await self._jobs.get(job_id, for_update=True)
        if job is None:
            return False
        if job.status in (ExportStatus.SUCCEEDED, ExportStatus.FAILED):
            return True  # duplicate delivery of a finished job

        job.status = ExportStatus.RUNNING
        job.attempts += 1
        job.started_at = job.started_at or utcnow()
        await self._session.commit()

        document = await build_export_document(self._session, self._tenant_id, job_id)

        job = await self._jobs.get(job_id, for_update=True)
        if job is None:
            return False
        if job.status in (ExportStatus.SUCCEEDED, ExportStatus.FAILED):
            return True  # another delivery of the same job finished meanwhile
        job.status = ExportStatus.SUCCEEDED
        job.result = document
        job.last_error = None
        job.finished_at = utcnow()
        self._audit.record(
            "export.completed", "export", job_id, {"projects": len(document["projects"])}
        )
        await self._session.commit()
        return True

    async def record_failure(self, job_id: uuid.UUID, error: str, *, final: bool) -> None:
        """Persist a failed attempt; ``final`` means no further retry will happen."""
        job = await self._jobs.get(job_id, for_update=True)
        if job is None or job.status is ExportStatus.SUCCEEDED:
            return
        job.last_error = error[:_MAX_ERROR_LENGTH]
        if final:
            job.status = ExportStatus.FAILED
            job.finished_at = utcnow()
            self._audit.record("export.failed", "export", job_id, {"error": job.last_error})
        else:
            job.status = ExportStatus.QUEUED
        await self._session.commit()
