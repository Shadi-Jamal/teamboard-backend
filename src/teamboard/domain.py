"""Core domain vocabulary: roles, permissions, plans and request principals.

This module has no I/O. Authorization is permission-based: human members get the
permission set of their role, API keys get exactly the scopes they were created with.
"""

import uuid
from dataclasses import dataclass
from enum import StrEnum


class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"

    @property
    def rank(self) -> int:
        return _ROLE_RANK[self]


_ROLE_RANK: dict[Role, int] = {Role.VIEWER: 1, Role.MEMBER: 2, Role.ADMIN: 3, Role.OWNER: 4}


class Permission(StrEnum):
    TENANT_READ = "tenant:read"
    TENANT_WRITE = "tenant:write"
    MEMBERS_READ = "members:read"
    MEMBERS_MANAGE = "members:manage"
    INVITATIONS_MANAGE = "invitations:manage"
    API_KEYS_MANAGE = "api_keys:manage"
    PROJECTS_READ = "projects:read"
    PROJECTS_WRITE = "projects:write"
    PROJECTS_DELETE = "projects:delete"
    TASKS_READ = "tasks:read"
    TASKS_WRITE = "tasks:write"
    AUDIT_READ = "audit:read"
    EXPORTS_MANAGE = "exports:manage"


_VIEWER = frozenset(
    {
        Permission.TENANT_READ,
        Permission.MEMBERS_READ,
        Permission.PROJECTS_READ,
        Permission.TASKS_READ,
    }
)
_MEMBER = _VIEWER | {Permission.PROJECTS_WRITE, Permission.TASKS_WRITE}
_ADMIN = _MEMBER | {
    Permission.TENANT_WRITE,
    Permission.MEMBERS_MANAGE,
    Permission.INVITATIONS_MANAGE,
    Permission.API_KEYS_MANAGE,
    Permission.PROJECTS_DELETE,
    Permission.AUDIT_READ,
    Permission.EXPORTS_MANAGE,
}

# Owners hold the same permissions as admins; what sets them apart is rank: only an
# owner can grant or revoke the owner role, and a tenant always keeps one owner.
ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: _VIEWER,
    Role.MEMBER: frozenset(_MEMBER),
    Role.ADMIN: frozenset(_ADMIN),
    Role.OWNER: frozenset(_ADMIN),
}

# Scopes an API key may hold. Administrative permissions (members, invitations,
# API keys, tenant settings) are deliberately reserved for interactive users.
API_KEY_SCOPES: frozenset[Permission] = frozenset(
    {
        Permission.TENANT_READ,
        Permission.MEMBERS_READ,
        Permission.PROJECTS_READ,
        Permission.PROJECTS_WRITE,
        Permission.PROJECTS_DELETE,
        Permission.TASKS_READ,
        Permission.TASKS_WRITE,
        Permission.AUDIT_READ,
        Permission.EXPORTS_MANAGE,
    }
)


class Plan(StrEnum):
    FREE = "free"
    PRO = "pro"
    ENTERPRISE = "enterprise"


@dataclass(frozen=True, slots=True)
class PlanLimits:
    requests_per_minute: int
    max_projects: int
    max_members: int
    max_api_keys: int
    exports_per_day: int


PLAN_LIMITS: dict[Plan, PlanLimits] = {
    Plan.FREE: PlanLimits(
        requests_per_minute=120, max_projects=5, max_members=5, max_api_keys=2, exports_per_day=5
    ),
    Plan.PRO: PlanLimits(
        requests_per_minute=1200,
        max_projects=200,
        max_members=50,
        max_api_keys=20,
        exports_per_day=100,
    ),
    Plan.ENTERPRISE: PlanLimits(
        requests_per_minute=6000,
        max_projects=5000,
        max_members=1000,
        max_api_keys=100,
        exports_per_day=1000,
    ),
}


class ActorType(StrEnum):
    USER = "user"
    API_KEY = "api_key"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class Actor:
    """Who performed an action - recorded in the audit log."""

    type: ActorType
    id: uuid.UUID | None

    def __str__(self) -> str:
        return f"{self.type.value}:{self.id}" if self.id else self.type.value


@dataclass(frozen=True, slots=True)
class UserPrincipal:
    """An authenticated human user (JWT access token). Not yet bound to a tenant."""

    user_id: uuid.UUID
    email: str


@dataclass(frozen=True, slots=True)
class ApiKeyPrincipal:
    """An authenticated API key. Always bound to exactly one tenant."""

    api_key_id: uuid.UUID
    tenant_id: uuid.UUID
    scopes: frozenset[Permission]


Principal = UserPrincipal | ApiKeyPrincipal


@dataclass(frozen=True, slots=True)
class TenantContext:
    """The authorized tenant scope of a request. Everything tenant-owned requires one."""

    tenant_id: uuid.UUID
    plan: Plan
    actor: Actor
    permissions: frozenset[Permission]
    role: Role | None = None
    user_id: uuid.UUID | None = None

    @property
    def limits(self) -> PlanLimits:
        return PLAN_LIMITS[self.plan]

    def has(self, permission: Permission) -> bool:
        return permission in self.permissions


class ProjectStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class TaskStatus(StrEnum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"


class TaskPriority(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class ExportStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
