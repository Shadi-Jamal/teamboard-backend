"""The product domain: projects and their tasks."""

import uuid
from datetime import date

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from teamboard.db.base import (
    Base,
    SoftDeleteMixin,
    TenantOwnedMixin,
    TimestampMixin,
    enum_check,
    enum_column,
)
from teamboard.domain import ProjectStatus, TaskPriority, TaskStatus


class Project(TimestampMixin, SoftDeleteMixin, TenantOwnedMixin, Base):
    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint(enum_check("status", ProjectStatus), name="status_valid"),
        # Target of the composite foreign key from tasks (see Task).
        UniqueConstraint("tenant_id", "id"),
        Index("ix_projects_tenant_created", "tenant_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    status: Mapped[ProjectStatus] = mapped_column(
        enum_column(ProjectStatus), default=ProjectStatus.ACTIVE, server_default="active"
    )


# Names are unique per tenant among live projects - never across tenants, so a name
# collision cannot reveal anything about another tenant's data.
Index(
    "uq_projects_tenant_name_live",
    Project.tenant_id,
    func.lower(Project.name),
    unique=True,
    postgresql_where=text("deleted_at IS NULL"),
)


class Task(TimestampMixin, SoftDeleteMixin, TenantOwnedMixin, Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(enum_check("status", TaskStatus), name="status_valid"),
        CheckConstraint(enum_check("priority", TaskPriority), name="priority_valid"),
        # Composite foreign keys include tenant_id, so the database itself rejects a
        # task that points at another tenant's project or at a non-member assignee.
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "assignee_id"],
            ["memberships.tenant_id", "memberships.user_id"],
        ),
        Index("ix_tasks_tenant_created", "tenant_id", "created_at", "id"),
        Index("ix_tasks_tenant_project", "tenant_id", "project_id"),
        Index("ix_tasks_tenant_assignee", "tenant_id", "assignee_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID]
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    status: Mapped[TaskStatus] = mapped_column(
        enum_column(TaskStatus), default=TaskStatus.TODO, server_default="todo"
    )
    priority: Mapped[TaskPriority] = mapped_column(
        enum_column(TaskPriority), default=TaskPriority.MEDIUM, server_default="medium"
    )
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    due_date: Mapped[date | None] = mapped_column(default=None)
