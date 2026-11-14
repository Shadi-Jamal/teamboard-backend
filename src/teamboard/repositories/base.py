"""Base class for repositories of tenant-owned tables."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Delete,
    Select,
    Update,
    delete,
    func,
    literal,
    select,
    text,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import Insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from teamboard.db.base import TenantOwnedMixin
from teamboard.pagination import Cursor, Page, PageParams


class TenantScopedRepository[M: TenantOwnedMixin]:
    """Application-layer tenant isolation.

    A repository instance is bound to one tenant for its whole life. Subclasses
    build every statement from ``_select`` / ``_count`` / ``_update`` / ``_delete``
    / ``_insert``, each of which already carries ``tenant_id = <bound tenant>``,
    and persist new rows through ``add``, which stamps the tenant. There is no code
    path in a subclass that can produce a statement without the tenant predicate.

    Row-level security enforces the same rule again inside PostgreSQL; either layer
    alone is sufficient, and the test-suite verifies each one independently.
    """

    model: type[M]

    def __init__(self, session: AsyncSession, tenant_id: uuid.UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id

    # -- statement builders -------------------------------------------------------

    def _select(self) -> Select[tuple[M]]:
        return select(self.model).where(self.model.tenant_id == self._tenant_id)

    def _count(self) -> Select[tuple[int]]:
        return (
            select(func.count())
            .select_from(self.model)
            .where(self.model.tenant_id == self._tenant_id)
        )

    def _update(self) -> Update:
        return update(self.model).where(self.model.tenant_id == self._tenant_id)

    def _delete(self) -> Delete:
        return delete(self.model).where(self.model.tenant_id == self._tenant_id)

    def _insert(self, **values: Any) -> Insert:
        return pg_insert(self.model).values(tenant_id=self._tenant_id, **values)

    # -- persistence --------------------------------------------------------------

    def add(self, entity: M) -> M:
        """Stage a new row. The tenant is always the repository's, never the caller's."""
        entity.tenant_id = self._tenant_id
        self._session.add(entity)
        return entity

    async def flush(self) -> None:
        await self._session.flush()

    # -- helpers ------------------------------------------------------------------

    async def _one_or_none(self, statement: Select[tuple[M]]) -> M | None:
        return (await self._session.scalars(statement)).one_or_none()

    async def _scalar_count(self, statement: Select[tuple[int]]) -> int:
        return (await self._session.scalars(statement)).one()

    async def _paginate(
        self,
        statement: Select[tuple[M]],
        page: PageParams,
        created_at: InstrumentedAttribute[datetime],
        row_id: InstrumentedAttribute[uuid.UUID],
    ) -> Page[M]:
        """Keyset pagination, newest first, on ``(created_at, row_id)``."""
        if page.cursor is not None:
            position = tuple_(
                literal(page.cursor.created_at, created_at.type),
                literal(page.cursor.id, row_id.type),
            )
            statement = statement.where(tuple_(created_at, row_id) < position)
        statement = statement.order_by(created_at.desc(), row_id.desc()).limit(page.limit + 1)

        rows = list((await self._session.scalars(statement)).all())
        items = rows[: page.limit]
        next_cursor = None
        if len(rows) > page.limit:
            last = items[-1]
            next_cursor = Cursor(
                created_at=getattr(last, created_at.key), id=getattr(last, row_id.key)
            ).encode()
        return Page(items=items, next_cursor=next_cursor)

    async def lock(self, purpose: str) -> None:
        """Serialize a check-then-write section for this tenant.

        Takes a transaction-scoped advisory lock keyed on (tenant, purpose); it is
        released automatically at COMMIT/ROLLBACK. Used where an invariant spans
        several rows (plan quotas, "at least one owner") and a row lock cannot help.
        """
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"{self._tenant_id}:{purpose}"},
        )
