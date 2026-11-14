"""Application-layer tenant isolation, tested with row-level security switched off.

Every test here uses a *superuser* session, for which PostgreSQL skips all RLS
policies. Whatever isolation is observed is therefore the work of
``TenantScopedRepository`` alone. Together with ``test_rls.py`` (which proves the
database isolates tenants without the repositories) this shows the two layers are
independent: either one could fail and the other would still hold.
"""

import uuid
from collections.abc import AsyncIterator, Sequence

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from teamboard.db.base import TenantOwnedMixin, utcnow
from teamboard.db.engine import create_sessionmaker
from teamboard.db.models import Project, Task
from teamboard.filters import AuditFilters, TaskFilters
from teamboard.pagination import PageParams
from teamboard.repositories.api_keys import ApiKeyRepository
from teamboard.repositories.audit import AuditLogRepository
from teamboard.repositories.exports import ExportJobRepository
from teamboard.repositories.idempotency import IdempotencyRepository
from teamboard.repositories.invitations import InvitationRepository
from teamboard.repositories.memberships import MembershipRepository
from teamboard.repositories.projects import ProjectRepository
from teamboard.repositories.tasks import TaskRepository
from tests.support import TenantWorld

Worlds = tuple[TenantWorld, TenantWorld]
EVERYTHING = PageParams(limit=100)


@pytest_asyncio.fixture
async def session(admin_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with create_sessionmaker(admin_engine)() as unrestricted:
        yield unrestricted
        await unrestricted.rollback()


async def test_precondition_this_session_really_bypasses_rls(
    worlds: Worlds, session: AsyncSession
) -> None:
    a, b = worlds
    tenants = {str(p.tenant_id) for p in (await session.scalars(select(Project))).all()}
    assert {a.tenant_id, b.tenant_id} <= tenants


async def test_get_by_id_cannot_reach_another_tenant(worlds: Worlds, session: AsyncSession) -> None:
    a, b = worlds
    tenant_a = uuid.UUID(a.tenant_id)

    assert await ProjectRepository(session, tenant_a).get(uuid.UUID(b.project["id"])) is None
    assert await TaskRepository(session, tenant_a).get(uuid.UUID(b.task["id"])) is None
    assert await MembershipRepository(session, tenant_a).get(uuid.UUID(b.member.id)) is None
    assert await ApiKeyRepository(session, tenant_a).get(uuid.UUID(b.api_key["id"])) is None
    assert await InvitationRepository(session, tenant_a).get(uuid.UUID(b.invitation["id"])) is None
    assert await ExportJobRepository(session, tenant_a).get(uuid.UUID(b.export["id"])) is None
    # ...including with the "show deleted" switch, which must not widen the tenant scope.
    assert (
        await ProjectRepository(session, tenant_a).get(
            uuid.UUID(b.deleted_project["id"]), include_deleted=True
        )
        is None
    )

    # Sanity: the same IDs resolve for their own tenant.
    tenant_b = uuid.UUID(b.tenant_id)
    assert await ProjectRepository(session, tenant_b).get(uuid.UUID(b.project["id"])) is not None
    assert await TaskRepository(session, tenant_b).get(uuid.UUID(b.task["id"])) is not None


async def test_lists_and_counts_contain_only_the_bound_tenant(
    worlds: Worlds, session: AsyncSession
) -> None:
    a, b = worlds
    tenant_a = uuid.UUID(a.tenant_id)

    listings: list[Sequence[TenantOwnedMixin]] = [
        (await ProjectRepository(session, tenant_a).list_page(EVERYTHING)).items,
        (await TaskRepository(session, tenant_a).list_page(EVERYTHING, TaskFilters())).items,
        (await MembershipRepository(session, tenant_a).list_page(EVERYTHING)).items,
        (await ApiKeyRepository(session, tenant_a).list_page(EVERYTHING)).items,
        (await InvitationRepository(session, tenant_a).list_page(EVERYTHING)).items,
        (await ExportJobRepository(session, tenant_a).list_page(EVERYTHING)).items,
        (await AuditLogRepository(session, tenant_a).list_page(EVERYTHING, AuditFilters())).items,
        await ProjectRepository(session, tenant_a).list_live(),
        await TaskRepository(session, tenant_a).list_live(),
    ]
    for rows in listings:
        assert rows, "expected the tenant to have data"
        assert {row.tenant_id for row in rows} == {tenant_a}

    assert await ProjectRepository(session, tenant_a).count_live() == 1
    assert await MembershipRepository(session, tenant_a).count() == 4
    assert await MembershipRepository(session, tenant_a).count_owners() == 1
    assert await ApiKeyRepository(session, tenant_a).count_active() == 1
    # A filter value from another tenant matches nothing.
    foreign_project = TaskFilters(project_id=uuid.UUID(b.project["id"]))
    assert (
        await TaskRepository(session, tenant_a).list_page(EVERYTHING, foreign_project)
    ).items == []


async def test_add_always_stamps_the_bound_tenant(worlds: Worlds, session: AsyncSession) -> None:
    """A caller cannot smuggle a row into another tenant by setting tenant_id."""
    a, b = worlds
    repository = ProjectRepository(session, uuid.UUID(a.tenant_id))

    project = Project(id=uuid.uuid4(), name="smuggled", tenant_id=uuid.UUID(b.tenant_id))
    repository.add(project)
    await repository.flush()

    assert project.tenant_id == uuid.UUID(a.tenant_id)


async def test_bulk_writes_cannot_touch_another_tenant(
    worlds: Worlds, session: AsyncSession
) -> None:
    a, b = worlds
    tenant_a = uuid.UUID(a.tenant_id)
    b_project, b_member = uuid.UUID(b.project["id"]), uuid.UUID(b.member.id)

    tasks = TaskRepository(session, tenant_a)
    await tasks.unassign(b_member)
    await tasks.soft_delete_for_project(b_project, utcnow())
    await MembershipRepository(session, tenant_a).remove(b_member)
    await InvitationRepository(session, tenant_a).revoke_pending_for_email(
        b.invitation["email"], utcnow()
    )
    touched = await ApiKeyRepository(session, tenant_a).touch(uuid.UUID(b.api_key["id"]), utcnow())
    await session.flush()

    assert touched is False
    b_task = (
        await session.scalars(
            select(Task)
            .where(Task.id == uuid.UUID(b.task["id"]))
            .execution_options(populate_existing=True)
        )
    ).one()
    assert b_task.assignee_id == b_member
    assert b_task.deleted_at is None
    tenant_b = uuid.UUID(b.tenant_id)
    assert await MembershipRepository(session, tenant_b).get(b_member) is not None
    b_invitation = await InvitationRepository(session, tenant_b).get(uuid.UUID(b.invitation["id"]))
    assert b_invitation is not None
    assert b_invitation.revoked_at is None


async def test_idempotency_keys_are_namespaced_per_tenant(
    worlds: Worlds, session: AsyncSession
) -> None:
    a, b = worlds
    key, expires = f"shared-{uuid.uuid4()}", utcnow().replace(year=2999)
    repo_a = IdempotencyRepository(session, uuid.UUID(a.tenant_id))
    repo_b = IdempotencyRepository(session, uuid.UUID(b.tenant_id))

    assert await repo_a.claim(key, "fingerprint-a", expires) is True
    await repo_a.store_response(key, 201, {"owner": "a"})
    # The same key is free for tenant B and never resolves to A's record.
    assert await repo_b.get(key) is None
    assert await repo_b.claim(key, "fingerprint-b", expires) is True
    assert await repo_a.claim(key, "fingerprint-a", expires) is False

    record_a = await repo_a.get(key)
    record_b = await repo_b.get(key)
    assert record_a is not None
    assert record_b is not None
    assert record_a.response_body == {"owner": "a"}
    assert record_b.response_body is None
