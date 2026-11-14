"""Audit trail: who changed what, and when."""

import enum
import uuid
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from teamboard.context import request_id_var
from teamboard.db.models import AuditLog
from teamboard.domain import Actor, TenantContext
from teamboard.filters import AuditFilters
from teamboard.pagination import Page, PageParams
from teamboard.repositories.audit import AuditLogRepository


def _jsonable(value: Any) -> Any:
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, uuid.UUID | datetime | date):
        return str(value)
    return value


def diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    """Changed fields as ``{field: {"old": ..., "new": ...}}``."""
    return {
        field: {"old": _jsonable(before.get(field)), "new": _jsonable(new)}
        for field, new in after.items()
        if before.get(field) != new
    }


def snapshot(values: Mapping[str, Any]) -> dict[str, Any]:
    """Initial values of a newly created entity, in the same shape as ``diff``."""
    return {field: {"old": None, "new": _jsonable(value)} for field, value in values.items()}


class AuditRecorder:
    """Appends audit entries to the current transaction.

    Because the entry is written through the same session as the change itself,
    the two are committed (or rolled back) together: there is no change without a
    record and no record without a change.
    """

    def __init__(self, session: AsyncSession, tenant_id: uuid.UUID, actor: Actor) -> None:
        self._repository = AuditLogRepository(session, tenant_id)
        self._actor = actor

    def record(
        self,
        action: str,
        entity_type: str,
        entity_id: uuid.UUID,
        changes: dict[str, Any] | None = None,
    ) -> None:
        self._repository.add(
            AuditLog(
                actor_type=self._actor.type,
                actor_id=self._actor.id,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                changes=changes or {},
                request_id=request_id_var.get(),
            )
        )


class AuditService:
    def __init__(self, session: AsyncSession, ctx: TenantContext) -> None:
        self._repository = AuditLogRepository(session, ctx.tenant_id)

    async def list_entries(self, page: PageParams, filters: AuditFilters) -> Page[AuditLog]:
        return await self._repository.list_page(page, filters)
