import uuid
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import or_

from teamboard.db.models import Task
from teamboard.domain import TaskPriority, TaskStatus
from teamboard.pagination import Page, PageParams
from teamboard.repositories.base import TenantScopedRepository


@dataclass(frozen=True, slots=True)
class TaskFilters:
    project_id: uuid.UUID | None = None
    status: TaskStatus | None = None
    priority: TaskPriority | None = None
    assignee_id: uuid.UUID | None = None
    due_before: date | None = None
    due_after: date | None = None
    search: str | None = None


class TaskRepository(TenantScopedRepository[Task]):
    model = Task

    async def get(self, task_id: uuid.UUID, *, include_deleted: bool = False) -> Task | None:
        statement = self._select().where(Task.id == task_id)
        if not include_deleted:
            statement = statement.where(Task.deleted_at.is_(None))
        return await self._one_or_none(statement)

    async def list_page(self, page: PageParams, filters: TaskFilters) -> Page[Task]:
        statement = self._select().where(Task.deleted_at.is_(None))
        if filters.project_id is not None:
            statement = statement.where(Task.project_id == filters.project_id)
        if filters.status is not None:
            statement = statement.where(Task.status == filters.status)
        if filters.priority is not None:
            statement = statement.where(Task.priority == filters.priority)
        if filters.assignee_id is not None:
            statement = statement.where(Task.assignee_id == filters.assignee_id)
        if filters.due_before is not None:
            statement = statement.where(Task.due_date <= filters.due_before)
        if filters.due_after is not None:
            statement = statement.where(Task.due_date >= filters.due_after)
        if filters.search:
            statement = statement.where(
                or_(
                    Task.title.icontains(filters.search, autoescape=True),
                    Task.description.icontains(filters.search, autoescape=True),
                )
            )
        return await self._paginate(statement, page, Task.created_at, Task.id)

    async def list_live(self) -> list[Task]:
        statement = (
            self._select().where(Task.deleted_at.is_(None)).order_by(Task.created_at, Task.id)
        )
        return list((await self._session.scalars(statement)).all())

    async def soft_delete_for_project(self, project_id: uuid.UUID, at: datetime) -> None:
        """Delete a project's live tasks together with it, stamped with the same time."""
        await self._session.execute(
            self._update()
            .where(Task.project_id == project_id, Task.deleted_at.is_(None))
            .values(deleted_at=at)
        )

    async def restore_for_project(self, project_id: uuid.UUID, deleted_at: datetime) -> None:
        """Bring back exactly the tasks that were deleted with the project.

        Tasks deleted individually beforehand carry a different timestamp and stay
        deleted.
        """
        await self._session.execute(
            self._update()
            .where(Task.project_id == project_id, Task.deleted_at == deleted_at)
            .values(deleted_at=None)
        )

    async def unassign(self, user_id: uuid.UUID) -> None:
        """Clear the assignee on every task assigned to a departing member."""
        await self._session.execute(
            self._update().where(Task.assignee_id == user_id).values(assignee_id=None)
        )
