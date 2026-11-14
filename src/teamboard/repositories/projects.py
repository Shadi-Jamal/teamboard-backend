import uuid

from teamboard.db.models import Project
from teamboard.domain import ProjectStatus
from teamboard.pagination import Page, PageParams
from teamboard.repositories.base import TenantScopedRepository


class ProjectRepository(TenantScopedRepository[Project]):
    model = Project

    async def get(self, project_id: uuid.UUID, *, include_deleted: bool = False) -> Project | None:
        statement = self._select().where(Project.id == project_id)
        if not include_deleted:
            statement = statement.where(Project.deleted_at.is_(None))
        return await self._one_or_none(statement)

    async def list_page(
        self,
        page: PageParams,
        *,
        status: ProjectStatus | None = None,
        search: str | None = None,
    ) -> Page[Project]:
        statement = self._select().where(Project.deleted_at.is_(None))
        if status is not None:
            statement = statement.where(Project.status == status)
        if search:
            statement = statement.where(Project.name.icontains(search, autoescape=True))
        return await self._paginate(statement, page, Project.created_at, Project.id)

    async def list_live(self) -> list[Project]:
        statement = (
            self._select()
            .where(Project.deleted_at.is_(None))
            .order_by(Project.created_at, Project.id)
        )
        return list((await self._session.scalars(statement)).all())

    async def count_live(self) -> int:
        return await self._scalar_count(self._count().where(Project.deleted_at.is_(None)))
