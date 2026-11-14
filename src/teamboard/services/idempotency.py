"""Idempotency keys for POST endpoints that create resources.

A client that does not know whether its request arrived (timeout, dropped
connection) can retry with the same ``Idempotency-Key`` and is guaranteed that the
operation runs at most once; the retry receives the original response.

The guarantee comes from PostgreSQL, not from application-level locking:

1. The key is inserted into ``idempotency_keys`` (primary key ``(tenant_id, key)``)
   in the *same transaction* as the operation.
2. The stored response is written before that transaction commits.

So the record and the operation's effects become visible atomically, and a
concurrent request with the same key blocks on the primary key until the first
one has committed or rolled back (see ``IdempotencyRepository.claim``).

Keys are scoped to a tenant: the same key used by two tenants never collides and
can never return another tenant's response.
"""

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from teamboard.config import Settings
from teamboard.db.base import utcnow
from teamboard.domain import TenantContext
from teamboard.errors import ConflictError, UnprocessableError
from teamboard.repositories.idempotency import IdempotencyRepository


@dataclass(frozen=True, slots=True)
class Outcome[S: BaseModel]:
    value: S
    # True if the operation was not executed and ``value`` is the stored response.
    replayed: bool


class IdempotencyService:
    def __init__(self, session: AsyncSession, ctx: TenantContext, settings: Settings) -> None:
        self._session = session
        self._ctx = ctx
        self._ttl = timedelta(seconds=settings.idempotency_ttl_seconds)
        self._repository = IdempotencyRepository(session, ctx.tenant_id)

    async def run[S: BaseModel](
        self,
        key: str | None,
        operation_name: str,
        parameters: dict[str, Any],
        operation: Callable[[], Awaitable[object]],
        schema: type[S],
        *,
        status_code: int = 201,
    ) -> Outcome[S]:
        """Run ``operation`` once per key and commit.

        ``operation`` must do its work on this service's session *without*
        committing; its result is converted to ``schema``, which is also what gets
        stored and replayed. Without a key the operation simply runs and commits.
        """
        if key is None:
            value = schema.model_validate(await operation())
            await self._session.commit()
            return Outcome(value=value, replayed=False)

        fingerprint = self._fingerprint(operation_name, parameters)
        if not await self._repository.claim(key, fingerprint, utcnow() + self._ttl):
            return await self._replay(key, fingerprint, schema)

        value = schema.model_validate(await operation())
        await self._repository.store_response(key, status_code, value.model_dump(mode="json"))
        await self._session.commit()
        return Outcome(value=value, replayed=False)

    async def _replay[S: BaseModel](
        self, key: str, fingerprint: str, schema: type[S]
    ) -> Outcome[S]:
        record = await self._repository.get(key)
        if record is None or record.response_body is None:
            # The record expired between the claim and this read; a retry will succeed.
            raise ConflictError(
                "A request with this Idempotency-Key is still being processed. Retry shortly.",
                code="idempotency_conflict",
            )
        if record.fingerprint != fingerprint:
            raise UnprocessableError(
                "This Idempotency-Key was already used for a different request.",
                code="idempotency_key_reused",
            )
        return Outcome(value=schema.model_validate(record.response_body), replayed=True)

    def _fingerprint(self, operation_name: str, parameters: dict[str, Any]) -> str:
        """Identifies "the same request": operation, caller and parameters."""
        canonical = json.dumps(
            {"operation": operation_name, "actor": str(self._ctx.actor), "parameters": parameters},
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()
