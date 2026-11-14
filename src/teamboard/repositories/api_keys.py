import uuid
from datetime import datetime, timedelta

from sqlalchemy import CursorResult, or_

from teamboard.db.models import ApiKey
from teamboard.pagination import Page, PageParams
from teamboard.repositories.base import TenantScopedRepository

# last_used_at is informational; refreshing it at most this often keeps API-key
# traffic from turning every read request into a write.
LAST_USED_GRANULARITY = timedelta(minutes=1)


class ApiKeyRepository(TenantScopedRepository[ApiKey]):
    model = ApiKey

    async def get(self, key_id: uuid.UUID) -> ApiKey | None:
        return await self._one_or_none(self._select().where(ApiKey.id == key_id))

    async def list_page(self, page: PageParams) -> Page[ApiKey]:
        return await self._paginate(self._select(), page, ApiKey.created_at, ApiKey.id)

    async def count_active(self) -> int:
        return await self._scalar_count(self._count().where(ApiKey.revoked_at.is_(None)))

    async def touch(self, key_id: uuid.UUID, now: datetime) -> bool:
        """Record usage; returns True if a row was actually updated."""
        result = await self._session.execute(
            self._update()
            .where(
                ApiKey.id == key_id,
                or_(
                    ApiKey.last_used_at.is_(None),
                    ApiKey.last_used_at < now - LAST_USED_GRANULARITY,
                ),
            )
            .values(last_used_at=now)
        )
        assert isinstance(result, CursorResult)  # noqa: S101 - narrows the type for mypy
        return result.rowcount > 0
