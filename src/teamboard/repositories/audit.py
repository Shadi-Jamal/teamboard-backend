from teamboard.db.models import AuditLog
from teamboard.filters import AuditFilters
from teamboard.pagination import Page, PageParams
from teamboard.repositories.base import TenantScopedRepository


class AuditLogRepository(TenantScopedRepository[AuditLog]):
    model = AuditLog

    async def list_page(self, page: PageParams, filters: AuditFilters) -> Page[AuditLog]:
        statement = self._select()
        if filters.entity_type is not None:
            statement = statement.where(AuditLog.entity_type == filters.entity_type)
        if filters.entity_id is not None:
            statement = statement.where(AuditLog.entity_id == filters.entity_id)
        if filters.actor_id is not None:
            statement = statement.where(AuditLog.actor_id == filters.actor_id)
        if filters.action is not None:
            statement = statement.where(AuditLog.action == filters.action)
        return await self._paginate(statement, page, AuditLog.created_at, AuditLog.id)
