"""Invitations to join a tenant.

An admin creates an invitation for an email address and receives a one-time token
to hand to the invitee. The invitee - signed in with an account for that address -
accepts it and becomes a member. Only the token's hash is stored.
"""

import uuid
from datetime import timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from teamboard.config import Settings
from teamboard.db.base import utcnow
from teamboard.db.errors import violated_constraint
from teamboard.db.models import Invitation, Membership
from teamboard.db.rls import set_rls_context
from teamboard.domain import PLAN_LIMITS, Actor, ActorType, Role, TenantContext, UserPrincipal
from teamboard.errors import (
    ConflictError,
    GoneError,
    NotFoundError,
    PermissionDeniedError,
    QuotaExceededError,
    UnprocessableError,
)
from teamboard.pagination import Page, PageParams
from teamboard.repositories.invitations import InvitationRepository
from teamboard.repositories.lookups import CredentialLookupRepository
from teamboard.repositories.memberships import MembershipRepository
from teamboard.repositories.tenants import TenantRepository
from teamboard.repositories.users import UserRepository
from teamboard.security.tokens import hash_token, new_invitation_token
from teamboard.services.audit import AuditRecorder, snapshot
from teamboard.services.auth import normalize_email
from teamboard.services.members import MEMBERS_LOCK


def _seats_exhausted(limit: int) -> QuotaExceededError:
    return QuotaExceededError(
        f"This tenant's plan allows at most {limit} members.", code="member_quota_exceeded"
    )


class InvitationService:
    """Invitation management inside a tenant (requires ``invitations:manage``)."""

    def __init__(self, session: AsyncSession, ctx: TenantContext, settings: Settings) -> None:
        self._session = session
        self._ctx = ctx
        self._ttl = timedelta(seconds=settings.invitation_ttl_seconds)
        self._invitations = InvitationRepository(session, ctx.tenant_id)
        self._memberships = MembershipRepository(session, ctx.tenant_id)
        self._users = UserRepository(session)
        self._audit = AuditRecorder(session, ctx.tenant_id, ctx.actor)

    async def create(self, email: str, role: Role) -> tuple[Invitation, str]:
        """Invite ``email``. Returns the invitation and its token (shown only once)."""
        email = normalize_email(email)
        if role is Role.OWNER:
            raise UnprocessableError(
                "Ownership cannot be granted by invitation. Invite the user, then "
                "change their role.",
                code="invalid_invitation_role",
            )
        if self._ctx.role is None or role.rank > self._ctx.role.rank:
            raise PermissionDeniedError(
                "You cannot invite someone to a role higher than your own.",
                code="insufficient_role",
            )

        # RLS shows a user row only if that user is already a member of this tenant,
        # so this lookup cannot be used to probe for accounts of other tenants.
        existing_user = await self._users.get_by_email(email)
        if existing_user is not None and await self._memberships.get(existing_user.id):
            raise ConflictError("This user is already a member.", code="already_member")
        if await self._memberships.count() >= self._ctx.limits.max_members:
            raise _seats_exhausted(self._ctx.limits.max_members)

        now = utcnow()
        # A new invitation replaces an open one for the same address.
        await self._invitations.revoke_pending_for_email(email, now)
        token = new_invitation_token()
        invitation = self._invitations.add(
            Invitation(
                id=uuid.uuid4(),
                email=email,
                role=role,
                token_hash=hash_token(token),
                invited_by_user_id=self._ctx.user_id,
                expires_at=now + self._ttl,
            )
        )
        self._audit.record(
            "invitation.created",
            "invitation",
            invitation.id,
            snapshot({"email": email, "role": role}),
        )
        try:
            await self._invitations.flush()
        except IntegrityError as exc:
            # Two admins invited the same address at the same moment.
            if violated_constraint(exc) == "uq_invitations_pending_email":
                raise ConflictError(
                    "An invitation for this email was just created.", code="invitation_conflict"
                ) from exc
            raise
        await self._session.commit()
        return invitation, token

    async def list_invitations(self, page: PageParams) -> Page[Invitation]:
        return await self._invitations.list_page(page)

    async def revoke(self, invitation_id: uuid.UUID) -> None:
        invitation = await self._invitations.get(invitation_id, for_update=True)
        if invitation is None:
            raise NotFoundError("Invitation not found.")
        if invitation.accepted_at is not None:
            raise ConflictError(
                "This invitation was already accepted.", code="invitation_already_accepted"
            )
        if invitation.revoked_at is None:
            invitation.revoked_at = utcnow()
            self._audit.record("invitation.revoked", "invitation", invitation.id)
            await self._session.commit()


class InvitationAcceptanceService:
    """Accepting an invitation. Runs for a signed-in user who is not yet a member,
    so there is no tenant context: the tenant is established by the token."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._lookups = CredentialLookupRepository(session)
        self._tenants = TenantRepository(session)

    async def accept(self, principal: UserPrincipal, raw_token: str) -> Membership:
        token_hash = hash_token(raw_token)
        await set_rls_context(self._session, token_hash=token_hash)
        found = await self._lookups.invitation_by_hash(token_hash)
        if found is None:
            raise NotFoundError("Invitation not found.", code="invitation_not_found")
        # The token alone is not enough: it must be redeemed by the invited address.
        if found.email != principal.email:
            raise PermissionDeniedError(
                "This invitation was issued for a different email address.",
                code="invitation_email_mismatch",
            )

        tenant_id = found.tenant_id
        await set_rls_context(self._session, tenant_id=tenant_id)
        invitations = InvitationRepository(self._session, tenant_id)
        memberships = MembershipRepository(self._session, tenant_id)

        await memberships.lock(MEMBERS_LOCK)
        # Re-read under lock: a concurrent request may have accepted or revoked it.
        invitation = await invitations.get(found.id, for_update=True)
        now = utcnow()
        if invitation is None:
            raise NotFoundError("Invitation not found.", code="invitation_not_found")
        if invitation.accepted_at is not None:
            raise ConflictError(
                "This invitation was already accepted.", code="invitation_already_accepted"
            )
        if invitation.revoked_at is not None:
            raise GoneError("This invitation was revoked.", code="invitation_revoked")
        if invitation.expires_at <= now:
            raise GoneError("This invitation has expired.", code="invitation_expired")
        if await memberships.get(principal.user_id) is not None:
            raise ConflictError("You are already a member of this tenant.", code="already_member")

        tenant = await self._tenants.get(tenant_id)
        if tenant is None:
            raise NotFoundError("Invitation not found.", code="invitation_not_found")
        seats = PLAN_LIMITS[tenant.plan].max_members
        if await memberships.count() >= seats:
            raise _seats_exhausted(seats)

        membership = memberships.add(Membership(user_id=principal.user_id, role=invitation.role))
        invitation.accepted_at = now
        AuditRecorder(self._session, tenant_id, Actor(ActorType.USER, principal.user_id)).record(
            "member.joined",
            "member",
            principal.user_id,
            snapshot({"role": invitation.role, "invitation_id": invitation.id}),
        )
        await self._session.commit()
        return membership
