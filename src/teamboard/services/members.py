"""Membership management and the role rules behind it.

Rules
-----
* You cannot change or remove a member who outranks you (an admin cannot touch an
  owner) and cannot grant a role above your own (an admin cannot create owners).
* A tenant always keeps at least one owner.
* Anyone may leave a tenant themselves, subject to the rule above.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from teamboard.db.models import Membership, User
from teamboard.domain import Permission, Role, TenantContext
from teamboard.errors import ConflictError, NotFoundError, PermissionDeniedError
from teamboard.pagination import Page, PageParams
from teamboard.repositories.memberships import MembershipRepository
from teamboard.repositories.tasks import TaskRepository
from teamboard.repositories.users import UserRepository
from teamboard.services.audit import AuditRecorder, diff

# Advisory-lock name that serializes every change to a tenant's member list.
MEMBERS_LOCK = "members"


@dataclass(frozen=True, slots=True)
class Member:
    membership: Membership
    user: User


def _not_found() -> NotFoundError:
    return NotFoundError("Member not found.")


class MemberService:
    def __init__(self, session: AsyncSession, ctx: TenantContext) -> None:
        self._session = session
        self._ctx = ctx
        self._memberships = MembershipRepository(session, ctx.tenant_id)
        self._tasks = TaskRepository(session, ctx.tenant_id)
        self._users = UserRepository(session)
        self._audit = AuditRecorder(session, ctx.tenant_id, ctx.actor)

    async def list_members(self, page: PageParams) -> Page[Member]:
        memberships = await self._memberships.list_page(page)
        users = await self._users.get_many([m.user_id for m in memberships.items])
        return Page(
            items=[Member(m, users[m.user_id]) for m in memberships.items if m.user_id in users],
            next_cursor=memberships.next_cursor,
        )

    async def change_role(self, user_id: uuid.UUID, new_role: Role) -> Member:
        await self._memberships.lock(MEMBERS_LOCK)
        actor_role = await self._current_actor_role()
        membership = await self._memberships.get(user_id)
        if membership is None:
            raise _not_found()

        if membership.role.rank > actor_role.rank:
            raise PermissionDeniedError(
                "You cannot change the role of a member who outranks you.",
                code="insufficient_role",
            )
        if new_role.rank > actor_role.rank:
            raise PermissionDeniedError(
                "You cannot grant a role higher than your own.", code="insufficient_role"
            )

        if membership.role != new_role:
            if membership.role is Role.OWNER:
                await self._ensure_another_owner()
            changes = diff({"role": membership.role}, {"role": new_role})
            membership.role = new_role
            self._audit.record("member.role_changed", "member", user_id, changes)
            await self._session.commit()
        return await self._as_member(membership)

    async def remove(self, user_id: uuid.UUID) -> None:
        """Remove a member, or leave the tenant when ``user_id`` is the caller."""
        leaving = user_id == self._ctx.user_id
        if not leaving and not self._ctx.has(Permission.MEMBERS_MANAGE):
            raise PermissionDeniedError("You are not allowed to remove members.")

        await self._memberships.lock(MEMBERS_LOCK)
        membership = await self._memberships.get(user_id)
        if membership is None:
            raise _not_found()
        if not leaving and membership.role.rank > (await self._current_actor_role()).rank:
            raise PermissionDeniedError(
                "You cannot remove a member who outranks you.", code="insufficient_role"
            )
        if membership.role is Role.OWNER:
            await self._ensure_another_owner()

        # Tasks reference the membership (composite FK), so unassign them first.
        await self._tasks.unassign(user_id)
        await self._memberships.remove(user_id)
        self._audit.record(
            "member.left" if leaving else "member.removed",
            "member",
            user_id,
            diff({"role": membership.role}, {"role": None}),
        )
        await self._session.commit()

    async def _ensure_another_owner(self) -> None:
        # Safe against concurrent demotions: the caller holds MEMBERS_LOCK.
        if await self._memberships.count_owners() <= 1:
            raise ConflictError(
                "A tenant must keep at least one owner. Promote another member first.",
                code="last_owner",
            )

    async def _current_actor_role(self) -> Role:
        """The caller's role as of now.

        Read again under MEMBERS_LOCK instead of trusting the role resolved at the
        start of the request: a concurrent request may have just demoted the caller.
        """
        if self._ctx.user_id is None:
            raise PermissionDeniedError("Members can only be managed by a signed-in user.")
        actor = await self._memberships.get(self._ctx.user_id)
        if actor is None:
            raise PermissionDeniedError("You are no longer a member of this tenant.")
        return actor.role

    async def _as_member(self, membership: Membership) -> Member:
        user = await self._users.get(membership.user_id)
        if user is None:
            raise _not_found()
        return Member(membership, user)
