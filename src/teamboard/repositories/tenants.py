"""Global repository for the tenant registry and a user's own memberships.

These queries are keyed by tenant ID or by the authenticated user rather than by a
bound tenant, because they run before a tenant context exists (resolving
``X-Tenant-ID``, listing "my tenants").
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from teamboard.db.models import Membership, Tenant


class TenantRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, tenant: Tenant) -> Tenant:
        self._session.add(tenant)
        return tenant

    async def get(self, tenant_id: uuid.UUID) -> Tenant | None:
        return (
            await self._session.scalars(select(Tenant).where(Tenant.id == tenant_id))
        ).one_or_none()

    async def get_with_membership(
        self, tenant_id: uuid.UUID, user_id: uuid.UUID
    ) -> tuple[Tenant, Membership] | None:
        """The tenant and the user's membership in it, if the user is a member."""
        statement = (
            select(Tenant, Membership)
            .join(Membership, Membership.tenant_id == Tenant.id)
            .where(Tenant.id == tenant_id, Membership.user_id == user_id)
        )
        row = (await self._session.execute(statement)).one_or_none()
        return (row[0], row[1]) if row is not None else None

    async def list_for_user(self, user_id: uuid.UUID) -> list[tuple[Tenant, Membership]]:
        statement = (
            select(Tenant, Membership)
            .join(Membership, Membership.tenant_id == Tenant.id)
            .where(Membership.user_id == user_id)
            .order_by(Tenant.name, Tenant.id)
        )
        return [(row[0], row[1]) for row in (await self._session.execute(statement)).all()]
