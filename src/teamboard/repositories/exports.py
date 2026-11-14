import uuid

from sqlalchemy.orm import undefer

from teamboard.db.models import ExportJob
from teamboard.pagination import Page, PageParams
from teamboard.repositories.base import TenantScopedRepository


class ExportJobRepository(TenantScopedRepository[ExportJob]):
    model = ExportJob

    async def get(
        self, job_id: uuid.UUID, *, for_update: bool = False, with_result: bool = False
    ) -> ExportJob | None:
        statement = self._select().where(ExportJob.id == job_id)
        if with_result:
            statement = statement.options(undefer(ExportJob.result))
        if for_update:
            # populate_existing: refresh a row this session may have loaded before
            # it held the lock.
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return await self._one_or_none(statement)

    async def list_page(self, page: PageParams) -> Page[ExportJob]:
        return await self._paginate(self._select(), page, ExportJob.created_at, ExportJob.id)
