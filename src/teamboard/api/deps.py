"""FastAPI dependencies: wiring of sessions, authentication, authorization and services.

Request flow for a tenant-scoped endpoint::

    get_session            one AsyncSession (= one connection) per request
      -> get_principal     verify the JWT or API key                  (401)
      -> get_tenant_context   check membership, scope the session to
                              the tenant (RLS), apply the rate limit  (403/429)
      -> require(permission)  check the role / key scopes             (403)
      -> get_*_service     build the service for the authorized tenant
"""

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from arq.connections import ArqRedis
from fastapi import Depends, Header, Query, Request
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from teamboard.config import Settings
from teamboard.context import actor_var, tenant_id_var
from teamboard.domain import ApiKeyPrincipal, Permission, Principal, TenantContext, UserPrincipal
from teamboard.errors import (
    AuthenticationError,
    BadRequestError,
    PermissionDeniedError,
    RateLimitedError,
)
from teamboard.infra.queue import JobQueue
from teamboard.infra.rate_limit import RateLimiter, RateLimitResult, UsageCounter
from teamboard.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Cursor, PageParams
from teamboard.security.passwords import PasswordService
from teamboard.security.tokens import AccessTokenService
from teamboard.services.access import AccessService
from teamboard.services.api_keys import ApiKeyService
from teamboard.services.audit import AuditService
from teamboard.services.auth import AuthService
from teamboard.services.exports import ExportService
from teamboard.services.idempotency import IdempotencyService
from teamboard.services.invitations import InvitationAcceptanceService, InvitationService
from teamboard.services.members import MemberService
from teamboard.services.projects import ProjectService
from teamboard.services.tasks import TaskService
from teamboard.services.tenants import TenantService, TenantSettingsService


@dataclass(slots=True)
class AppResources:
    """Process-wide singletons, created in the application lifespan."""

    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    redis: ArqRedis
    passwords: PasswordService
    access_tokens: AccessTokenService
    rate_limiter: RateLimiter
    counter: UsageCounter
    queue: JobQueue


def get_resources(request: Request) -> AppResources:
    resources: AppResources = request.app.state.resources
    return resources


Resources = Annotated[AppResources, Depends(get_resources)]


async def get_session(resources: Resources) -> AsyncIterator[AsyncSession]:
    """One session per request.

    Nothing is committed here: services commit explicitly. Whatever is still open
    when the request ends - including after an error - is rolled back on close.
    """
    async with resources.sessionmaker() as session:
        yield session


# scope="function": the session is closed (and its connection returned to the pool)
# as soon as the endpoint has produced its response, before the response is sent.
DbSession = Annotated[AsyncSession, Depends(get_session, scope="function")]


# ------------------------------------------------------------------ authentication

_bearer = HTTPBearer(
    auto_error=False,
    scheme_name="AccessToken",
    description="JWT access token of a user, obtained from `POST /v1/auth/login`.",
)
_api_key = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
    scheme_name="ApiKey",
    description="API key of a tenant, created with `POST /v1/api-keys`.",
)


def get_access_service(session: DbSession, resources: Resources) -> AccessService:
    return AccessService(session, resources.access_tokens, resources.rate_limiter)


Access = Annotated[AccessService, Depends(get_access_service)]


async def get_principal(
    access: Access,
    bearer: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    api_key: Annotated[str | None, Depends(_api_key)],
) -> Principal:
    """Authenticate the request with a user access token or an API key."""
    principal: Principal
    if bearer is not None and api_key is not None:
        raise BadRequestError(
            "Send either an Authorization header or an X-API-Key header, not both.",
            code="ambiguous_credentials",
        )
    if api_key is not None:
        principal = await access.authenticate_api_key(api_key)
        actor_var.set(f"api_key:{principal.api_key_id}")
    elif bearer is not None:
        principal = await access.authenticate_access_token(bearer.credentials)
        actor_var.set(f"user:{principal.user_id}")
    else:
        raise AuthenticationError("Authentication is required.", code="not_authenticated")
    return principal


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]


async def get_current_user(principal: CurrentPrincipal) -> UserPrincipal:
    """For endpoints that act on a user account rather than on a tenant."""
    if isinstance(principal, ApiKeyPrincipal):
        raise PermissionDeniedError(
            "This endpoint requires a user access token, not an API key.",
            code="user_token_required",
        )
    return principal


CurrentUser = Annotated[UserPrincipal, Depends(get_current_user)]


# ------------------------------------------------------------------- authorization


async def get_tenant_context(
    request: Request,
    principal: CurrentPrincipal,
    access: Access,
    tenant_id: Annotated[
        uuid.UUID | None,
        Header(
            alias="X-Tenant-ID",
            description=(
                "The tenant to act on. Required with a user access token; optional with "
                "an API key (which is already bound to its tenant)."
            ),
        ),
    ] = None,
) -> TenantContext:
    """Authorize the caller for a tenant and scope the database session to it."""
    ctx = await access.resolve_tenant(principal, tenant_id)
    tenant_id_var.set(str(ctx.tenant_id))
    try:
        request.state.rate_limit = await access.enforce_rate_limit(ctx)
    except RateLimitedError:
        request.state.rate_limit = RateLimitResult(
            allowed=False, limit=ctx.limits.requests_per_minute, remaining=0, retry_after=0
        )
        raise
    return ctx


TenantCtx = Annotated[TenantContext, Depends(get_tenant_context)]


def require(permission: Permission) -> Callable[[TenantContext], Awaitable[TenantContext]]:
    """Dependency factory: the caller must hold ``permission`` in the current tenant."""

    async def check(ctx: TenantCtx) -> TenantContext:
        if not ctx.has(permission):
            raise PermissionDeniedError(
                f"This action requires the '{permission.value}' permission.",
                code="permission_denied",
            )
        return ctx

    return check


async def auth_rate_limit(request: Request, resources: Resources) -> None:
    """Throttle unauthenticated auth endpoints per client address."""
    client_ip = request.client.host if request.client else "unknown"
    result = await resources.rate_limiter.consume(
        f"auth:{client_ip}", limit=resources.settings.auth_rate_limit_per_minute
    )
    if not result.allowed:
        raise RateLimitedError(
            "Too many authentication requests. Slow down.", retry_after=result.retry_after
        )


# ---------------------------------------------------------------- request parsing


def get_page_params(
    limit: Annotated[
        int, Query(ge=1, le=MAX_PAGE_SIZE, description="Maximum number of items to return.")
    ] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[
        str | None,
        Query(description="`next_cursor` of the previous page. Omit for the first page."),
    ] = None,
) -> PageParams:
    return PageParams(limit=limit, cursor=Cursor.decode(cursor) if cursor else None)


Pagination = Annotated[PageParams, Depends(get_page_params)]

IdempotencyKeyHeader = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=255,
        description=(
            "Optional unique key (e.g. a UUID) that makes this request safe to retry: "
            "a repeated request with the same key returns the original response "
            "(with `Idempotent-Replayed: true`) instead of creating a second resource. "
            "Keys are remembered for 24 hours."
        ),
    ),
]


# ------------------------------------------------------------------------ services


def get_auth_service(session: DbSession, resources: Resources) -> AuthService:
    return AuthService(
        session,
        resources.settings,
        resources.passwords,
        resources.access_tokens,
        resources.counter,
    )


def get_tenant_service(session: DbSession) -> TenantService:
    return TenantService(session)


def get_invitation_acceptance_service(session: DbSession) -> InvitationAcceptanceService:
    return InvitationAcceptanceService(session)


def get_tenant_settings_service(
    session: DbSession, ctx: TenantCtx, resources: Resources
) -> TenantSettingsService:
    return TenantSettingsService(session, ctx, resources.counter)


def get_member_service(session: DbSession, ctx: TenantCtx) -> MemberService:
    return MemberService(session, ctx)


def get_invitation_service(
    session: DbSession, ctx: TenantCtx, resources: Resources
) -> InvitationService:
    return InvitationService(session, ctx, resources.settings)


def get_api_key_service(session: DbSession, ctx: TenantCtx) -> ApiKeyService:
    return ApiKeyService(session, ctx)


def get_idempotency_service(
    session: DbSession, ctx: TenantCtx, resources: Resources
) -> IdempotencyService:
    return IdempotencyService(session, ctx, resources.settings)


Idempotency = Annotated[IdempotencyService, Depends(get_idempotency_service)]


def get_project_service(
    session: DbSession, ctx: TenantCtx, idempotency: Idempotency
) -> ProjectService:
    return ProjectService(session, ctx, idempotency)


def get_task_service(session: DbSession, ctx: TenantCtx, idempotency: Idempotency) -> TaskService:
    return TaskService(session, ctx, idempotency)


def get_audit_service(session: DbSession, ctx: TenantCtx) -> AuditService:
    return AuditService(session, ctx)


def get_export_service(
    session: DbSession, ctx: TenantCtx, idempotency: Idempotency, resources: Resources
) -> ExportService:
    return ExportService(session, ctx, idempotency, resources.queue, resources.counter)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
TenantServiceDep = Annotated[TenantService, Depends(get_tenant_service)]
InvitationAcceptanceDep = Annotated[
    InvitationAcceptanceService, Depends(get_invitation_acceptance_service)
]
TenantSettingsDep = Annotated[TenantSettingsService, Depends(get_tenant_settings_service)]
MemberServiceDep = Annotated[MemberService, Depends(get_member_service)]
InvitationServiceDep = Annotated[InvitationService, Depends(get_invitation_service)]
ApiKeyServiceDep = Annotated[ApiKeyService, Depends(get_api_key_service)]
ProjectServiceDep = Annotated[ProjectService, Depends(get_project_service)]
TaskServiceDep = Annotated[TaskService, Depends(get_task_service)]
AuditServiceDep = Annotated[AuditService, Depends(get_audit_service)]
ExportServiceDep = Annotated[ExportService, Depends(get_export_service)]
