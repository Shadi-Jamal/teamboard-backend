"""Filter criteria for list endpoints. Plain data, shared by all layers."""

import uuid
from dataclasses import dataclass
from datetime import date

from teamboard.domain import TaskPriority, TaskStatus


@dataclass(frozen=True, slots=True)
class TaskFilters:
    project_id: uuid.UUID | None = None
    status: TaskStatus | None = None
    priority: TaskPriority | None = None
    assignee_id: uuid.UUID | None = None
    due_before: date | None = None
    due_after: date | None = None
    search: str | None = None


@dataclass(frozen=True, slots=True)
class AuditFilters:
    entity_type: str | None = None
    entity_id: uuid.UUID | None = None
    actor_id: uuid.UUID | None = None
    action: str | None = None
