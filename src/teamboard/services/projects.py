"""Projects: CRUD with soft delete, plan quota and audit trail."""

import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from teamboard.db.base import utcnow
from teamboard.db.errors import violated_constraint
from teamboard.db.models import Project
from teamboard.domain import ProjectStatus, TenantContext
from teamboard.errors import ConflictError, NotFoundError, QuotaExceededError
from teamboard.pagination import Page, PageParams
from teamboard.repositories.projects import ProjectRepository
from teamboard.repositories.tasks import TaskRepository
from teamboard.schemas.work import ProjectCreate, ProjectRead, ProjectUpdate
from teamboard.services.audit import AuditRecorder, diff, snapshot
from teamboard.services.idempotency import IdempotencyService, Outcome

_NAME_CONSTRAINT = "uq_projects_tenant_name_live"
_PROJECTS_LOCK = "projects"


def _not_found() -> NotFoundError:
    return NotFoundError("Project not found.")


def _name_taken() -> ConflictError:
    return ConflictError("A project with this name already exists.", code="project_name_taken")


class ProjectService:
    def __init__(
        self, session: AsyncSession, ctx: TenantContext, idempotency: IdempotencyService
    ) -> None:
        self._session = session
        self._ctx = ctx
        self._idempotency = idempotency
        self._projects = ProjectRepository(session, ctx.tenant_id)
        self._tasks = TaskRepository(session, ctx.tenant_id)
        self._audit = AuditRecorder(session, ctx.tenant_id, ctx.actor)

    async def create(
        self, data: ProjectCreate, idempotency_key: str | None
    ) -> Outcome[ProjectRead]:
        return await self._idempotency.run(
            idempotency_key,
            "project.create",
            data.model_dump(mode="json"),
            lambda: self._create(data),
            ProjectRead,
        )

    async def _create(self, data: ProjectCreate) -> Project:
        await self._ensure_capacity()
        project = self._projects.add(
            Project(id=uuid.uuid4(), name=data.name.strip(), description=data.description)
        )
        self._audit.record(
            "project.created",
            "project",
            project.id,
            snapshot({"name": project.name, "description": project.description}),
        )
        await self._flush()
        return project

    async def list_projects(
        self, page: PageParams, *, status: ProjectStatus | None, search: str | None
    ) -> Page[Project]:
        return await self._projects.list_page(page, status=status, search=search)

    async def get(self, project_id: uuid.UUID) -> Project:
        project = await self._projects.get(project_id)
        if project is None:
            raise _not_found()
        return project

    async def update(self, project_id: uuid.UUID, data: ProjectUpdate) -> Project:
        project = await self.get(project_id)
        requested: dict[str, Any] = data.model_dump(exclude_unset=True, exclude_none=True)
        if "name" in requested:
            requested["name"] = requested["name"].strip()
        changes = diff({field: getattr(project, field) for field in requested}, requested)
        if not changes:
            return project

        for field in changes:
            setattr(project, field, requested[field])
        self._audit.record("project.updated", "project", project.id, changes)
        await self._flush()
        await self._session.commit()
        return project

    async def delete(self, project_id: uuid.UUID) -> None:
        """Soft-delete a project together with its tasks."""
        project = await self.get(project_id)
        now = utcnow()
        project.deleted_at = now
        await self._tasks.soft_delete_for_project(project.id, now)
        self._audit.record("project.deleted", "project", project.id)
        await self._session.commit()

    async def restore(self, project_id: uuid.UUID) -> Project:
        project = await self._projects.get(project_id, include_deleted=True)
        if project is None:
            raise _not_found()
        if project.deleted_at is None:
            return project

        # A restored project counts against the plan again.
        await self._ensure_capacity()
        deleted_at = project.deleted_at
        project.deleted_at = None
        await self._tasks.restore_for_project(project.id, deleted_at)
        self._audit.record("project.restored", "project", project.id)
        await self._flush()
        await self._session.commit()
        return project

    async def _ensure_capacity(self) -> None:
        # The advisory lock makes count-then-insert atomic per tenant; without it
        # two concurrent requests could both see "one slot left".
        await self._projects.lock(_PROJECTS_LOCK)
        limit = self._ctx.limits.max_projects
        if await self._projects.count_live() >= limit:
            raise QuotaExceededError(
                f"This tenant's plan allows at most {limit} projects.",
                code="project_quota_exceeded",
            )

    async def _flush(self) -> None:
        try:
            await self._projects.flush()
        except IntegrityError as exc:
            if violated_constraint(exc) == _NAME_CONSTRAINT:
                raise _name_taken() from exc
            raise
