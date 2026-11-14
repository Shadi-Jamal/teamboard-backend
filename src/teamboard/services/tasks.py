"""Tasks: CRUD with filters, soft delete and audit trail."""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from teamboard.db.base import utcnow
from teamboard.db.models import Task
from teamboard.domain import ProjectStatus, TenantContext
from teamboard.errors import ConflictError, NotFoundError, UnprocessableError
from teamboard.filters import TaskFilters
from teamboard.pagination import Page, PageParams
from teamboard.repositories.memberships import MembershipRepository
from teamboard.repositories.projects import ProjectRepository
from teamboard.repositories.tasks import TaskRepository
from teamboard.schemas.work import TaskCreate, TaskRead, TaskUpdate
from teamboard.services.audit import AuditRecorder, diff, snapshot
from teamboard.services.idempotency import IdempotencyService, Outcome

# Fields a client may explicitly set to null (to clear them) in a PATCH.
_NULLABLE_FIELDS = frozenset({"assignee_id", "due_date"})


def _not_found() -> NotFoundError:
    return NotFoundError("Task not found.")


class TaskService:
    def __init__(
        self, session: AsyncSession, ctx: TenantContext, idempotency: IdempotencyService
    ) -> None:
        self._session = session
        self._ctx = ctx
        self._idempotency = idempotency
        self._tasks = TaskRepository(session, ctx.tenant_id)
        self._projects = ProjectRepository(session, ctx.tenant_id)
        self._memberships = MembershipRepository(session, ctx.tenant_id)
        self._audit = AuditRecorder(session, ctx.tenant_id, ctx.actor)

    async def create(
        self, project_id: uuid.UUID, data: TaskCreate, idempotency_key: str | None
    ) -> Outcome[TaskRead]:
        return await self._idempotency.run(
            idempotency_key,
            "task.create",
            {"project_id": str(project_id), **data.model_dump(mode="json")},
            lambda: self._create(project_id, data),
            TaskRead,
        )

    async def _create(self, project_id: uuid.UUID, data: TaskCreate) -> Task:
        project = await self._projects.get(project_id)
        if project is None:
            raise NotFoundError("Project not found.")
        if project.status is ProjectStatus.ARCHIVED:
            raise ConflictError(
                "Tasks cannot be added to an archived project.", code="project_archived"
            )
        await self._ensure_assignable(data.assignee_id)

        task = self._tasks.add(
            Task(
                id=uuid.uuid4(),
                project_id=project.id,
                title=data.title.strip(),
                description=data.description,
                status=data.status,
                priority=data.priority,
                assignee_id=data.assignee_id,
                due_date=data.due_date,
            )
        )
        self._audit.record(
            "task.created",
            "task",
            task.id,
            snapshot(
                {
                    "project_id": task.project_id,
                    "title": task.title,
                    "status": task.status,
                    "priority": task.priority,
                    "assignee_id": task.assignee_id,
                    "due_date": task.due_date,
                }
            ),
        )
        await self._tasks.flush()
        return task

    async def list_tasks(self, page: PageParams, filters: TaskFilters) -> Page[Task]:
        return await self._tasks.list_page(page, filters)

    async def get(self, task_id: uuid.UUID) -> Task:
        task = await self._tasks.get(task_id)
        if task is None:
            raise _not_found()
        return task

    async def update(self, task_id: uuid.UUID, data: TaskUpdate) -> Task:
        task = await self.get(task_id)
        requested: dict[str, Any] = {
            field: value
            for field, value in data.model_dump(exclude_unset=True).items()
            if value is not None or field in _NULLABLE_FIELDS
        }
        if "title" in requested:
            requested["title"] = requested["title"].strip()
        changes = diff({field: getattr(task, field) for field in requested}, requested)
        if not changes:
            return task

        if "assignee_id" in changes:
            await self._ensure_assignable(requested["assignee_id"])
        for field in changes:
            setattr(task, field, requested[field])
        self._audit.record("task.updated", "task", task.id, changes)
        await self._session.commit()
        return task

    async def delete(self, task_id: uuid.UUID) -> None:
        task = await self.get(task_id)
        task.deleted_at = utcnow()
        self._audit.record("task.deleted", "task", task.id)
        await self._session.commit()

    async def restore(self, task_id: uuid.UUID) -> Task:
        task = await self._tasks.get(task_id, include_deleted=True)
        if task is None:
            raise _not_found()
        if task.deleted_at is None:
            return task
        if await self._projects.get(task.project_id) is None:
            raise ConflictError(
                "The task's project is deleted. Restore the project first.",
                code="project_deleted",
            )
        task.deleted_at = None
        self._audit.record("task.restored", "task", task.id)
        await self._session.commit()
        return task

    async def _ensure_assignable(self, assignee_id: uuid.UUID | None) -> None:
        if assignee_id is None:
            return
        # Scoped to this tenant, so a user of another tenant looks exactly like a
        # user that does not exist.
        if await self._memberships.get(assignee_id) is None:
            raise UnprocessableError(
                "The assignee must be a member of this tenant.", code="invalid_assignee"
            )
