import uuid
from datetime import datetime

from teamboard.db.models import Invitation
from teamboard.pagination import Page, PageParams
from teamboard.repositories.base import TenantScopedRepository


class InvitationRepository(TenantScopedRepository[Invitation]):
    model = Invitation

    async def get(self, invitation_id: uuid.UUID, *, for_update: bool = False) -> Invitation | None:
        statement = self._select().where(Invitation.id == invitation_id)
        if for_update:
            # populate_existing: refresh a row this session may have loaded before
            # it held the lock.
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return await self._one_or_none(statement)

    async def list_page(self, page: PageParams) -> Page[Invitation]:
        return await self._paginate(self._select(), page, Invitation.created_at, Invitation.id)

    async def revoke_pending_for_email(self, email: str, at: datetime) -> None:
        """Close any open invitation for this address so a fresh one can replace it."""
        await self._session.execute(
            self._update()
            .where(
                Invitation.email == email,
                Invitation.accepted_at.is_(None),
                Invitation.revoked_at.is_(None),
            )
            .values(revoked_at=at)
        )
