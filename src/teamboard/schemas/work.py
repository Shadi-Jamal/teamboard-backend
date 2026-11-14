import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field

from teamboard.domain import ActorType, ExportStatus, ProjectStatus, TaskPriority, TaskStatus
from teamboard.schemas.common import Schema

# ---------------------------------------------------------------------- projects


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120, examples=["Website relaunch"])
    description: str = Field(default="", max_length=10_000)


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=10_000)
    status: ProjectStatus | None = None


class ProjectRead(Schema):
    id: uuid.UUID
    name: str
    description: str
    status: ProjectStatus
    created_at: datetime
    updated_at: datetime


# ------------------------------------------------------------------------- tasks


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200, examples=["Draft the landing page"])
    description: str = Field(default="", max_length=10_000)
    status: TaskStatus = TaskStatus.TODO
    priority: TaskPriority = TaskPriority.MEDIUM
    assignee_id: uuid.UUID | None = Field(
        default=None, description="User ID of a member of this tenant."
    )
    due_date: date | None = None


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10_000)
    status: TaskStatus | None = None
    priority: TaskPriority | None = None
    assignee_id: uuid.UUID | None = Field(default=None, description="`null` unassigns the task.")
    due_date: date | None = Field(default=None, description="`null` clears the due date.")


class TaskRead(Schema):
    id: uuid.UUID
    project_id: uuid.UUID
    title: str
    description: str
    status: TaskStatus
    priority: TaskPriority
    assignee_id: uuid.UUID | None
    due_date: date | None
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------- audit log


class AuditLogRead(Schema):
    id: uuid.UUID
    actor_type: ActorType
    actor_id: uuid.UUID | None
    action: str = Field(examples=["project.updated"])
    entity_type: str = Field(examples=["project"])
    entity_id: uuid.UUID
    changes: dict[str, Any] = Field(
        description="Changed fields as `{field: {old, new}}`.",
        examples=[{"name": {"old": "Old name", "new": "New name"}}],
    )
    request_id: str | None
    created_at: datetime


# ----------------------------------------------------------------------- exports


class ExportRead(Schema):
    id: uuid.UUID
    status: ExportStatus
    attempts: int
    last_error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class ExportedProject(ProjectRead):
    tasks: list[TaskRead]


class ExportDocument(BaseModel):
    export_id: uuid.UUID
    tenant_id: uuid.UUID
    generated_at: datetime
    projects: list[ExportedProject]
