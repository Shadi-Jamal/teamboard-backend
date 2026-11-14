"""Global repository: refresh tokens belong to a user, not to a tenant."""

import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from teamboard.db.models import RefreshToken


class RefreshTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, token: RefreshToken) -> RefreshToken:
        self._session.add(token)
        return token

    async def get_by_hash_for_update(self, token_hash: str) -> RefreshToken | None:
        """Load a token and lock its row.

        The lock is what makes rotation safe under concurrency: of several requests
        presenting the same token, exactly one proceeds at a time, and every later
        one observes the first one's ``used_at``.
        """
        statement = (
            select(RefreshToken).where(RefreshToken.token_hash == token_hash).with_for_update()
        )
        return (await self._session.scalars(statement)).one_or_none()

    async def revoke_family(self, family_id: uuid.UUID, at: datetime) -> None:
        await self._session.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=at)
        )
