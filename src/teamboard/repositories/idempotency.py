from datetime import datetime
from typing import Any

from sqlalchemy import func

from teamboard.db.models import IdempotencyKey
from teamboard.repositories.base import TenantScopedRepository


class IdempotencyRepository(TenantScopedRepository[IdempotencyKey]):
    model = IdempotencyKey

    async def claim(self, key: str, fingerprint: str, expires_at: datetime) -> bool:
        """Try to take ownership of ``key`` for the current transaction.

        Returns True if this transaction now owns the key and must perform the
        operation; False if a live record already exists (replay).

        The primary key is ``(tenant_id, key)``. If another transaction has inserted
        the same key and not yet finished, this INSERT *blocks* on its row until
        that transaction commits (we then see its record and return False) or rolls
        back (we then insert and return True). This is what makes two simultaneous
        requests with one key execute the operation exactly once.

        An expired record is taken over in place.
        """
        insert = self._insert(
            key=key, fingerprint=fingerprint, expires_at=expires_at, created_at=func.now()
        )
        statement = insert.on_conflict_do_update(
            index_elements=[IdempotencyKey.tenant_id, IdempotencyKey.key],
            set_={
                "fingerprint": insert.excluded.fingerprint,
                "expires_at": insert.excluded.expires_at,
                "created_at": func.now(),
                "status_code": None,
                "response_body": None,
            },
            where=IdempotencyKey.expires_at < func.now(),
        ).returning(IdempotencyKey.key)
        return (await self._session.execute(statement)).first() is not None

    async def get(self, key: str) -> IdempotencyKey | None:
        return await self._one_or_none(
            self._select()
            .where(IdempotencyKey.key == key)
            .execution_options(populate_existing=True)
        )

    async def store_response(self, key: str, status_code: int, body: dict[str, Any]) -> None:
        await self._session.execute(
            self._update()
            .where(IdempotencyKey.key == key)
            .values(status_code=status_code, response_body=body)
        )
