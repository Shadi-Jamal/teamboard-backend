import uuid
from datetime import datetime

from pydantic import AwareDatetime, BaseModel, EmailStr, Field

from teamboard.domain import Plan, Role
from teamboard.schemas.common import RequestModel, Schema

SLUG_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{1,38}[a-z0-9])$"


class TenantCreate(RequestModel):
    name: str = Field(min_length=1, max_length=100, examples=["Acme Inc."])
    slug: str = Field(
        pattern=SLUG_PATTERN,
        description="Globally unique, URL-safe identifier (3-40 chars: a-z, 0-9, hyphen).",
        examples=["acme"],
    )


class TenantUpdate(RequestModel):
    name: str = Field(min_length=1, max_length=100)


class TenantRead(Schema):
    id: uuid.UUID
    name: str
    slug: str
    plan: Plan
    created_at: datetime
    updated_at: datetime


class TenantWithRole(TenantRead):
    role: Role = Field(description="The caller's role in this tenant.")


class PlanLimitsRead(Schema):
    requests_per_minute: int
    max_projects: int
    max_members: int
    max_api_keys: int
    exports_per_day: int


class UsageCounts(BaseModel):
    projects: int
    members: int
    api_keys: int
    exports_today: int


class UsageRead(BaseModel):
    plan: Plan
    limits: PlanLimitsRead
    usage: UsageCounts


# ----------------------------------------------------------------------- members


class MemberRead(BaseModel):
    user_id: uuid.UUID
    email: str
    full_name: str
    role: Role
    joined_at: datetime


class MemberUpdate(RequestModel):
    role: Role


# ------------------------------------------------------------------- invitations


class InvitationCreate(RequestModel):
    email: EmailStr = Field(max_length=320, examples=["grace@example.com"])
    role: Role = Field(default=Role.MEMBER, description="`admin`, `member` or `viewer`.")


class InvitationRead(Schema):
    id: uuid.UUID
    email: str
    role: Role
    invited_by_user_id: uuid.UUID | None
    expires_at: datetime
    accepted_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class InvitationCreated(InvitationRead):
    token: str = Field(
        description="The invitation secret. Returned only once - deliver it to the invitee."
    )


class InvitationAccept(RequestModel):
    token: str = Field(min_length=1, max_length=512)


class MembershipRead(Schema):
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    role: Role


# ---------------------------------------------------------------------- API keys


class ApiKeyCreate(RequestModel):
    name: str = Field(min_length=1, max_length=100, examples=["CI pipeline"])
    scopes: list[str] = Field(
        min_length=1,
        description="Permissions granted to the key, e.g. `projects:read`.",
        examples=[["projects:read", "tasks:read"]],
    )
    expires_at: AwareDatetime | None = Field(
        default=None, description="Optional expiry (timezone-aware). Omit for no expiry."
    )


class ApiKeyRead(Schema):
    id: uuid.UUID
    name: str
    prefix: str = Field(description="First characters of the key, for identification.")
    scopes: list[str]
    created_by_user_id: uuid.UUID | None
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None


class ApiKeyCreated(ApiKeyRead):
    key: str = Field(
        description="The API key. Returned only once; only a hash is stored. "
        "Send it as the `X-API-Key` header."
    )
