"""Global repository: credential lookups that happen before a tenant is known.

An API key or invitation token identifies its tenant, so it must be found *first*.
These are the only cross-tenant reads in the application, and they are point
lookups by the SHA-256 hash of a secret the caller presented. Row-level security
allows them through a dedicated policy that exposes exactly the row whose hash
matches ``app.token_hash`` - nothing can be listed or scanned.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from teamboard.db.models import ApiKey, Invitation


class CredentialLookupRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def api_key_by_hash(self, key_hash: str) -> ApiKey | None:
        return (
            await self._session.scalars(select(ApiKey).where(ApiKey.key_hash == key_hash))
        ).one_or_none()

    async def invitation_by_hash(self, token_hash: str) -> Invitation | None:
        return (
            await self._session.scalars(
                select(Invitation).where(Invitation.token_hash == token_hash)
            )
        ).one_or_none()
