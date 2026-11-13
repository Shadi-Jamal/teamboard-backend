"""Row-level security context for database sessions.

PostgreSQL policies (see the RLS migration) compare each row against a handful of
custom settings such as ``app.tenant_id``. This module is the only place that sets
them.

Why this cannot leak between requests through the connection pool
-----------------------------------------------------------------
The settings are written with ``set_config(name, value, is_local => true)``, the
function form of ``SET LOCAL``. PostgreSQL discards such a value when the
transaction ends, on COMMIT and ROLLBACK alike. A pooled connection therefore never
carries one request's tenant into the next: outside a transaction there is no tenant,
and with no tenant the policies match no rows (fail closed).

The context itself is remembered on the *session* (``session.info``) and re-applied
by an ``after_begin`` hook whenever that session starts another transaction, so a
service may commit mid-request without silently dropping its scope.
"""

import uuid
from typing import Final

from sqlalchemy import Connection, event, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, SessionTransaction

_INFO_KEY: Final = "rls_context"

# Context field -> PostgreSQL setting read by the policies.
_SETTINGS: Final[dict[str, str]] = {
    "tenant_id": "app.tenant_id",
    "user_id": "app.user_id",
    "token_hash": "app.token_hash",
    "login_email": "app.login_email",
}

# One statement sets all four settings; unset fields are written as '' which the
# policies treat as "no value". Names are constants, values are bind parameters.
_APPLY_SQL: Final = text(
    "SELECT "
    + ", ".join(f"set_config('{setting}', :{field}, true)" for field, setting in _SETTINGS.items())
)


class RlsSession(Session):
    """Session class whose transactions always start with the RLS context applied."""


def _params(session: Session) -> dict[str, str]:
    context: dict[str, str] = session.info.get(_INFO_KEY, {})
    return {field: context.get(field, "") for field in _SETTINGS}


@event.listens_for(RlsSession, "after_begin")
def _apply_context_on_begin(
    session: Session, transaction: SessionTransaction, connection: Connection
) -> None:
    connection.execute(_APPLY_SQL, _params(session))


async def set_rls_context(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    token_hash: str | None = None,
    login_email: str | None = None,
) -> None:
    """Add the given values to the session's RLS context and apply them now.

    Only the fields passed are changed; previously set fields are kept.
    """
    context: dict[str, str] = session.info.setdefault(_INFO_KEY, {})
    updates = {
        "tenant_id": str(tenant_id) if tenant_id is not None else None,
        "user_id": str(user_id) if user_id is not None else None,
        "token_hash": token_hash,
        "login_email": login_email,
    }
    context.update({field: value for field, value in updates.items() if value is not None})

    if session.in_transaction():
        await session.execute(_APPLY_SQL, _params(session.sync_session))
    else:
        # Starting the transaction fires the after_begin hook, which applies the context.
        await session.connection()
