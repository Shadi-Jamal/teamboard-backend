"""Tenant lifecycle, settings and usage."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from teamboard.db.errors import violated_constraint
from teamboard.db.models import Membership, Tenant
from teamboard.db.rls import set_rls_context
from teamboard.domain import (
    Actor,
    ActorType,
    Plan,
    PlanLimits,
    Role,
    TenantContext,
    UserPrincipal,
)
from teamboard.errors import ConflictError, NotFoundError
from teamboard.infra.rate_limit import UsageCounter
from teamboard.repositories.api_keys import ApiKeyRepository
from teamboard.repositories.memberships import MembershipRepository
from teamboard.repositories.projects import ProjectRepository
from teamboard.repositories.tenants import TenantRepository
from teamboard.services.audit import AuditRecorder, diff, snapshot


def exports_usage_key(tenant_id: uuid.UUID, now: datetime | None = None) -> str:
    """Redis counter for a tenant's exports on the current UTC day."""
    day = (now or datetime.now(UTC)).strftime("%Y%m%d")
    return f"exports:{tenant_id}:{day}"


@dataclass(frozen=True, slots=True)
class TenantMembership:
    """A tenant together with the role a particular user holds in it."""

    tenant: Tenant
    role: Role


@dataclass(frozen=True, slots=True)
class Usage:
    plan: Plan
    limits: PlanLimits
    projects: int
    members: int
    api_keys: int
    exports_today: int


class TenantService:
    """Operations a user performs outside of any tenant: create one, list their own."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._tenants = TenantRepository(session)

    async def create(self, principal: UserPrincipal, name: str, slug: str) -> TenantMembership:
        """Create a tenant with the caller as its first owner."""
        tenant = Tenant(id=uuid.uuid4(), name=name.strip(), slug=slug, plan=Plan.FREE)
        # The ID is generated here so the session can be scoped to the new tenant
        # before its rows are inserted (RLS only admits rows of the current tenant).
        await set_rls_context(self._session, tenant_id=tenant.id)
        self._tenants.add(tenant)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            if violated_constraint(exc) == "uq_tenants_slug":
                raise ConflictError("This slug is already taken.", code="slug_taken") from exc
            raise

        MembershipRepository(self._session, tenant.id).add(
            Membership(user_id=principal.user_id, role=Role.OWNER)
        )
        AuditRecorder(self._session, tenant.id, Actor(ActorType.USER, principal.user_id)).record(
            "tenant.created", "tenant", tenant.id, snapshot({"name": tenant.name, "slug": slug})
        )
        await self._session.commit()
        return TenantMembership(tenant, Role.OWNER)

    async def list_for_user(self, principal: UserPrincipal) -> list[TenantMembership]:
        return [
            TenantMembership(tenant, membership.role)
            for tenant, membership in await self._tenants.list_for_user(principal.user_id)
        ]


class TenantSettingsService:
    """Operations on the current tenant."""

    def __init__(self, session: AsyncSession, ctx: TenantContext, counter: UsageCounter) -> None:
        self._session = session
        self._ctx = ctx
        self._counter = counter
        self._tenants = TenantRepository(session)
        self._audit = AuditRecorder(session, ctx.tenant_id, ctx.actor)

    async def get(self) -> Tenant:
        tenant = await self._tenants.get(self._ctx.tenant_id)
        if tenant is None:
            raise NotFoundError("Tenant not found.")
        return tenant

    async def rename(self, name: str) -> Tenant:
        tenant = await self.get()
        changes = diff({"name": tenant.name}, {"name": name.strip()})
        if changes:
            tenant.name = name.strip()
            self._audit.record("tenant.updated", "tenant", tenant.id, changes)
            await self._session.commit()
        return tenant

    async def usage(self) -> Usage:
        tenant_id = self._ctx.tenant_id
        return Usage(
            plan=self._ctx.plan,
            limits=self._ctx.limits,
            projects=await ProjectRepository(self._session, tenant_id).count_live(),
            members=await MembershipRepository(self._session, tenant_id).count(),
            api_keys=await ApiKeyRepository(self._session, tenant_id).count_active(),
            exports_today=await self._counter.current(exports_usage_key(tenant_id)),
        )
