"""Tenants and everything that controls access to them."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, PrimaryKeyConstraint, String, func, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from teamboard.db.base import (
    Base,
    TenantOwnedMixin,
    TimestampMixin,
    enum_check,
    enum_column,
    utcnow,
)
from teamboard.domain import Plan, Role


class Tenant(TimestampMixin, Base):
    __tablename__ = "tenants"
    __table_args__ = (CheckConstraint(enum_check("plan", Plan), name="plan_valid"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100))
    slug: Mapped[str] = mapped_column(String(40), unique=True)
    plan: Mapped[Plan] = mapped_column(enum_column(Plan), default=Plan.FREE, server_default="free")


class Membership(TimestampMixin, TenantOwnedMixin, Base):
    __tablename__ = "memberships"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "user_id"),
        CheckConstraint(enum_check("role", Role), name="role_valid"),
        Index("ix_memberships_user_id", "user_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[Role] = mapped_column(enum_column(Role))


class Invitation(TenantOwnedMixin, Base):
    __tablename__ = "invitations"
    __table_args__ = (
        CheckConstraint(enum_check("role", Role), name="role_valid"),
        CheckConstraint("email = lower(email)", name="email_lowercase"),
        # At most one open invitation per address and tenant.
        Index(
            "uq_invitations_pending_email",
            "tenant_id",
            "email",
            unique=True,
            postgresql_where=text("accepted_at IS NULL AND revoked_at IS NULL"),
        ),
        Index("ix_invitations_tenant_created", "tenant_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320))
    role: Mapped[Role] = mapped_column(enum_column(Role))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    invited_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    expires_at: Mapped[datetime]
    accepted_at: Mapped[datetime | None] = mapped_column(default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())


class ApiKey(TenantOwnedMixin, Base):
    """A machine credential bound to one tenant. Only the SHA-256 hash is stored."""

    __tablename__ = "api_keys"
    __table_args__ = (Index("ix_api_keys_tenant_created", "tenant_id", "created_at", "id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100))
    prefix: Mapped[str] = mapped_column(String(16))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(50)))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    expires_at: Mapped[datetime | None] = mapped_column(default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(default=None)
    last_used_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
