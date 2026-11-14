"""Global repository: user accounts are not owned by a tenant."""

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from teamboard.db.models import User


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, user: User) -> User:
        self._session.add(user)
        return user

    async def get(self, user_id: uuid.UUID) -> User | None:
        return (await self._session.scalars(select(User).where(User.id == user_id))).one_or_none()

    async def get_by_email(self, email: str) -> User | None:
        return (await self._session.scalars(select(User).where(User.email == email))).one_or_none()

    async def get_many(self, user_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, User]:
        if not user_ids:
            return {}
        users = await self._session.scalars(select(User).where(User.id.in_(user_ids)))
        return {user.id: user for user in users}
