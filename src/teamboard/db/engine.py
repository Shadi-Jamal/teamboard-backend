"""Engine and session factory construction."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from teamboard.config import Settings
from teamboard.db.rls import RlsSession


class UnsafeDatabaseRoleError(RuntimeError):
    """The configured database role is exempt from row-level security."""


def create_engine(settings: Settings, *, application_name: str = "teamboard-api") -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        connect_args={
            "server_settings": {
                "application_name": application_name,
                "statement_timeout": str(settings.db_statement_timeout_ms),
            }
        },
    )


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: services commit before the response is serialized and
    # the loaded objects must stay readable without another (context-less) query.
    return async_sessionmaker(engine, sync_session_class=RlsSession, expire_on_commit=False)


async def assert_role_is_subject_to_rls(engine: AsyncEngine) -> None:
    """Refuse to run as a role that bypasses row-level security.

    Superusers and BYPASSRLS roles ignore every policy, which would silently remove
    the database half of tenant isolation. Failing at startup turns a dangerous
    misconfiguration (e.g. pointing DATABASE_URL at the migration role) into an
    obvious one.
    """
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles "
                    "WHERE rolname = current_user"
                )
            )
        ).one()
    if row.rolsuper or row.rolbypassrls:
        raise UnsafeDatabaseRoleError(
            f"Database role {row.rolname!r} bypasses row-level security "
            "(superuser or BYPASSRLS). Configure DATABASE_URL with the restricted "
            "application role instead."
        )
