"""Authentication of requests and resolution of the tenant they may act on.

This is the single place where a ``TenantContext`` is created and where the
database session receives its tenant scope. The tenant is always derived from a
verified credential - a membership row for users, the key's own row for API keys -
never taken on trust from the request.
"""

import contextlib
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from teamboard.db.base import utcnow
from teamboard.db.rls import set_rls_context
from teamboard.domain import (
    ROLE_PERMISSIONS,
    Actor,
    ActorType,
    ApiKeyPrincipal,
    Permission,
    Principal,
    TenantContext,
    UserPrincipal,
)
from teamboard.errors import (
    AuthenticationError,
    BadRequestError,
    PermissionDeniedError,
    RateLimitedError,
)
from teamboard.infra.rate_limit import RateLimiter, RateLimitResult
from teamboard.repositories.api_keys import ApiKeyRepository
from teamboard.repositories.lookups import CredentialLookupRepository
from teamboard.repositories.tenants import TenantRepository
from teamboard.repositories.users import UserRepository
from teamboard.security.tokens import API_KEY_PREFIX, AccessTokenService, hash_token


def _tenant_access_denied() -> PermissionDeniedError:
    # Identical for "tenant does not exist" and "you are not a member": the response
    # must not reveal whether a tenant ID is in use.
    return PermissionDeniedError(
        "You do not have access to this tenant.", code="tenant_access_denied"
    )


class AccessService:
    def __init__(
        self,
        session: AsyncSession,
        access_tokens: AccessTokenService,
        rate_limiter: RateLimiter,
    ) -> None:
        self._session = session
        self._access_tokens = access_tokens
        self._rate_limiter = rate_limiter
        self._users = UserRepository(session)
        self._tenants = TenantRepository(session)
        self._lookups = CredentialLookupRepository(session)

    async def authenticate_access_token(self, token: str) -> UserPrincipal:
        user_id = self._access_tokens.verify(token)
        await set_rls_context(self._session, user_id=user_id)
        # Loaded on every request so that a disabled account loses access at once,
        # not only when its access token expires.
        user = await self._users.get(user_id)
        if user is None or not user.is_active:
            raise AuthenticationError(
                "The access token is invalid or expired.", code="invalid_token"
            )
        return UserPrincipal(user_id=user.id, email=user.email)

    async def authenticate_api_key(self, raw_key: str) -> ApiKeyPrincipal:
        invalid = AuthenticationError("The API key is invalid.", code="invalid_api_key")
        if not raw_key.startswith(API_KEY_PREFIX):
            raise invalid
        key_hash = hash_token(raw_key)
        await set_rls_context(self._session, token_hash=key_hash)
        key = await self._lookups.api_key_by_hash(key_hash)
        if key is None or key.revoked_at is not None:
            raise invalid
        if key.expires_at is not None and key.expires_at <= utcnow():
            raise invalid

        scopes = set()
        for scope in key.scopes:
            # A scope that no longer exists grants nothing.
            with contextlib.suppress(ValueError):
                scopes.add(Permission(scope))
        return ApiKeyPrincipal(api_key_id=key.id, tenant_id=key.tenant_id, scopes=frozenset(scopes))

    async def resolve_tenant(
        self, principal: Principal, requested_tenant_id: uuid.UUID | None
    ) -> TenantContext:
        """Authorize the principal for a tenant and scope the session to it."""
        if isinstance(principal, ApiKeyPrincipal):
            return await self._resolve_for_api_key(principal, requested_tenant_id)
        return await self._resolve_for_user(principal, requested_tenant_id)

    async def enforce_rate_limit(self, ctx: TenantContext) -> RateLimitResult:
        """Count this request against the tenant's per-minute budget."""
        result = await self._rate_limiter.consume(
            f"tenant:{ctx.tenant_id}", limit=ctx.limits.requests_per_minute, period_seconds=60
        )
        if not result.allowed:
            raise RateLimitedError(
                "The request rate limit for this tenant was exceeded.",
                retry_after=result.retry_after,
            )
        return result

    async def _resolve_for_user(
        self, principal: UserPrincipal, requested_tenant_id: uuid.UUID | None
    ) -> TenantContext:
        if requested_tenant_id is None:
            raise BadRequestError(
                "The X-Tenant-ID header is required for this endpoint.", code="tenant_required"
            )
        # Runs with only app.user_id set: RLS exposes just this user's memberships.
        found = await self._tenants.get_with_membership(requested_tenant_id, principal.user_id)
        if found is None:
            raise _tenant_access_denied()
        tenant, membership = found

        await set_rls_context(self._session, tenant_id=tenant.id)
        return TenantContext(
            tenant_id=tenant.id,
            plan=tenant.plan,
            actor=Actor(ActorType.USER, principal.user_id),
            permissions=ROLE_PERMISSIONS[membership.role],
            role=membership.role,
            user_id=principal.user_id,
        )

    async def _resolve_for_api_key(
        self, principal: ApiKeyPrincipal, requested_tenant_id: uuid.UUID | None
    ) -> TenantContext:
        # A key belongs to one tenant. X-Tenant-ID is optional, but if sent it must match.
        if requested_tenant_id is not None and requested_tenant_id != principal.tenant_id:
            raise _tenant_access_denied()

        await set_rls_context(self._session, tenant_id=principal.tenant_id)
        tenant = await self._tenants.get(principal.tenant_id)
        if tenant is None:
            raise _tenant_access_denied()

        api_keys = ApiKeyRepository(self._session, tenant.id)
        if await api_keys.touch(principal.api_key_id, utcnow()):
            # Committed on its own so that read-only requests also record key usage.
            await self._session.commit()

        return TenantContext(
            tenant_id=tenant.id,
            plan=tenant.plan,
            actor=Actor(ActorType.API_KEY, principal.api_key_id),
            permissions=principal.scopes,
        )
