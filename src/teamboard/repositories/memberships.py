import uuid

from teamboard.db.models import Membership
from teamboard.domain import Role
from teamboard.pagination import Page, PageParams
from teamboard.repositories.base import TenantScopedRepository


class MembershipRepository(TenantScopedRepository[Membership]):
    model = Membership

    async def get(self, user_id: uuid.UUID) -> Membership | None:
        # populate_existing: always return the committed state, even if this session
        # loaded the row earlier (membership rules are re-checked after taking a lock).
        return await self._one_or_none(
            self._select()
            .where(Membership.user_id == user_id)
            .execution_options(populate_existing=True)
        )

    async def list_page(self, page: PageParams) -> Page[Membership]:
        return await self._paginate(self._select(), page, Membership.created_at, Membership.user_id)

    async def count(self) -> int:
        return await self._scalar_count(self._count())

    async def count_owners(self) -> int:
        return await self._scalar_count(self._count().where(Membership.role == Role.OWNER))

    async def remove(self, user_id: uuid.UUID) -> None:
        await self._session.execute(self._delete().where(Membership.user_id == user_id))
