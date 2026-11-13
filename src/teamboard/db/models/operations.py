"""Operational tables: audit trail, idempotency records and export jobs."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, Index, PrimaryKeyConstraint, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from teamboard.db.base import Base, TenantOwnedMixin, enum_check, enum_column, utcnow
from teamboard.domain import ActorType, ExportStatus


class AuditLog(TenantOwnedMixin, Base):
    """Append-only record of who changed what and when.

    The application role is only granted SELECT and INSERT on this table.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        CheckConstraint(enum_check("actor_type", ActorType), name="actor_type_valid"),
        Index("ix_audit_logs_tenant_created", "tenant_id", "created_at", "id"),
        Index("ix_audit_logs_tenant_entity", "tenant_id", "entity_type", "entity_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    actor_type: Mapped[ActorType] = mapped_column(enum_column(ActorType))
    actor_id: Mapped[uuid.UUID | None]
    action: Mapped[str] = mapped_column(String(100))
    entity_type: Mapped[str] = mapped_column(String(50))
    entity_id: Mapped[uuid.UUID]
    changes: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    request_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())


class IdempotencyKey(TenantOwnedMixin, Base):
    """The stored outcome of a POST made with an ``Idempotency-Key`` header.

    Written in the same transaction as the operation it guards, so the record exists
    if and only if the operation was committed.
    """

    __tablename__ = "idempotency_keys"
    __table_args__ = (PrimaryKeyConstraint("tenant_id", "key"),)

    key: Mapped[str] = mapped_column(String(255))
    fingerprint: Mapped[str] = mapped_column(String(64))
    status_code: Mapped[int | None]
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
    expires_at: Mapped[datetime]


class ExportJob(TenantOwnedMixin, Base):
    __tablename__ = "export_jobs"
    __table_args__ = (
        CheckConstraint(enum_check("status", ExportStatus), name="status_valid"),
        Index("ix_export_jobs_tenant_created", "tenant_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    status: Mapped[ExportStatus] = mapped_column(
        enum_column(ExportStatus), default=ExportStatus.QUEUED, server_default="queued"
    )
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    # The export document. Deferred: listings must not drag it along.
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, deferred=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(default=None)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)
