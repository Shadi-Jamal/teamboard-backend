"""API keys: per-tenant machine credentials with explicit scopes."""

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from teamboard.db.base import utcnow
from teamboard.db.models import ApiKey
from teamboard.domain import API_KEY_SCOPES, Permission, TenantContext
from teamboard.errors import (
    NotFoundError,
    PermissionDeniedError,
    QuotaExceededError,
    UnprocessableError,
)
from teamboard.pagination import Page, PageParams
from teamboard.repositories.api_keys import ApiKeyRepository
from teamboard.security.tokens import API_KEY_DISPLAY_LENGTH, hash_token, new_api_key
from teamboard.services.audit import AuditRecorder, snapshot


class ApiKeyService:
    def __init__(self, session: AsyncSession, ctx: TenantContext) -> None:
        self._session = session
        self._ctx = ctx
        self._api_keys = ApiKeyRepository(session, ctx.tenant_id)
        self._audit = AuditRecorder(session, ctx.tenant_id, ctx.actor)

    async def create(
        self, name: str, scopes: list[str], expires_at: datetime | None
    ) -> tuple[ApiKey, str]:
        """Create a key. Returns the row and the key itself, which is never stored
        and cannot be retrieved again."""
        granted = self._validate_scopes(scopes)
        if expires_at is not None and expires_at <= utcnow():
            raise UnprocessableError("expires_at must be in the future.", code="invalid_expiry")

        await self._api_keys.lock("api_keys")
        limit = self._ctx.limits.max_api_keys
        if await self._api_keys.count_active() >= limit:
            raise QuotaExceededError(
                f"This tenant's plan allows at most {limit} active API keys.",
                code="api_key_quota_exceeded",
            )

        raw_key = new_api_key()
        api_key = self._api_keys.add(
            ApiKey(
                id=uuid.uuid4(),
                name=name.strip(),
                prefix=raw_key[:API_KEY_DISPLAY_LENGTH],
                key_hash=hash_token(raw_key),
                scopes=sorted(scope.value for scope in granted),
                created_by_user_id=self._ctx.user_id,
                expires_at=expires_at,
            )
        )
        self._audit.record(
            "api_key.created",
            "api_key",
            api_key.id,
            snapshot({"name": api_key.name, "scopes": api_key.scopes}),
        )
        await self._session.commit()
        return api_key, raw_key

    async def list_keys(self, page: PageParams) -> Page[ApiKey]:
        return await self._api_keys.list_page(page)

    async def revoke(self, key_id: uuid.UUID) -> None:
        api_key = await self._api_keys.get(key_id)
        if api_key is None:
            raise NotFoundError("API key not found.")
        if api_key.revoked_at is None:
            api_key.revoked_at = utcnow()
            self._audit.record("api_key.revoked", "api_key", api_key.id)
            await self._session.commit()

    def _validate_scopes(self, scopes: list[str]) -> set[Permission]:
        granted: set[Permission] = set()
        for scope in scopes:
            try:
                permission = Permission(scope)
            except ValueError:
                permission = None
            if permission is None or permission not in API_KEY_SCOPES:
                allowed = ", ".join(sorted(s.value for s in API_KEY_SCOPES))
                raise UnprocessableError(
                    f"Unknown or non-grantable scope {scope!r}. Allowed scopes: {allowed}.",
                    code="invalid_scope",
                )
            granted.add(permission)
        # A key can never do more than the person who creates it.
        if not granted <= self._ctx.permissions:
            raise PermissionDeniedError(
                "You cannot grant scopes that you do not hold yourself.", code="insufficient_role"
            )
        return granted
