"""The Alembic migrations, exercised through the real CLI against PostgreSQL."""

import os
import subprocess
import sys
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from teamboard.db.base import Base
from tests.conftest import APP_ROLE, ROOT, DatabaseUrls, run_alembic


@pytest_asyncio.fixture
async def scratch_database(
    database_urls: DatabaseUrls, admin_engine: AsyncEngine
) -> AsyncIterator[str]:
    """An empty database next to the main test database, dropped afterwards."""
    name = "migration_scratch"
    autocommit = admin_engine.execution_options(isolation_level="AUTOCOMMIT")
    async with autocommit.connect() as connection:
        await connection.execute(text(f"DROP DATABASE IF EXISTS {name}"))
        await connection.execute(text(f"CREATE DATABASE {name}"))
    yield make_url(database_urls.admin).set(database=name).render_as_string(hide_password=False)
    async with autocommit.connect() as connection:
        await connection.execute(text(f"DROP DATABASE {name} WITH (FORCE)"))


async def _inventory(url: str) -> dict[str, list[str]]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:

            async def names(sql: str) -> list[str]:
                return sorted((await connection.execute(text(sql))).scalars().all())

            return {
                "tables": await names(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                    "AND tablename <> 'alembic_version'"
                ),
                "policies": await names(
                    "SELECT tablename || '.' || policyname FROM pg_policies "
                    "WHERE schemaname = 'public'"
                ),
                "functions": await names(
                    "SELECT proname FROM pg_proc WHERE pronamespace = 'public'::regnamespace"
                ),
                "grants": await names(
                    "SELECT DISTINCT table_name FROM information_schema.role_table_grants "
                    f"WHERE grantee = '{APP_ROLE}'"
                ),
            }
    finally:
        await engine.dispose()


def test_models_and_migrations_are_in_sync(database_urls: DatabaseUrls) -> None:
    """`alembic check` fails if a model change has no migration."""
    result = run_alembic(database_urls.admin, "check")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "No new upgrade operations detected" in result.stdout + result.stderr


def test_there_is_a_single_migration_head(database_urls: DatabaseUrls) -> None:
    result = run_alembic(database_urls.admin, "heads")

    assert result.stdout.strip().splitlines() == ["0002 (head)"]


async def test_upgrade_downgrade_upgrade_round_trip(scratch_database: str) -> None:
    upgraded = run_alembic(scratch_database, "upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stderr
    after_upgrade = await _inventory(scratch_database)

    downgraded = run_alembic(scratch_database, "downgrade", "base")
    assert downgraded.returncode == 0, downgraded.stderr
    after_downgrade = await _inventory(scratch_database)

    again = run_alembic(scratch_database, "upgrade", "head")
    assert again.returncode == 0, again.stderr

    assert after_upgrade["tables"] == sorted(Base.metadata.tables)
    assert after_upgrade["grants"] == sorted(Base.metadata.tables)
    assert len(after_upgrade["policies"]) == 22
    assert after_upgrade["functions"] == [
        "app_login_email",
        "app_tenant_id",
        "app_token_hash",
        "app_user_id",
    ]
    # The downgrade removes everything it created - nothing is left half-secured.
    assert after_downgrade == {"tables": [], "policies": [], "functions": [], "grants": []}
    assert await _inventory(scratch_database) == after_upgrade


def test_each_revision_can_be_applied_on_its_own(scratch_database: str) -> None:
    for target in ("0001", "0002"):
        result = run_alembic(scratch_database, "upgrade", target)
        assert result.returncode == 0, result.stderr


def test_upgrade_fails_clearly_when_the_application_role_is_missing(
    scratch_database: str,
) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env={**os.environ, "MIGRATION_DATABASE_URL": scratch_database, "APP_DB_ROLE": "ghost_role"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert 'Role "ghost_role" does not exist' in result.stderr


@pytest.mark.parametrize("role", ["bad;drop table users", "Mixed_Case", "1starts_with_digit", ""])
def test_application_role_name_is_validated_before_it_reaches_sql(
    scratch_database: str, role: str
) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env={**os.environ, "MIGRATION_DATABASE_URL": scratch_database, "APP_DB_ROLE": role},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "APP_DB_ROLE is not a valid role name" in result.stderr


def test_migrations_require_an_explicit_database_url() -> None:
    environment = {k: v for k, v in os.environ.items() if k != "MIGRATION_DATABASE_URL"}

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "MIGRATION_DATABASE_URL is not set" in result.stderr


def test_migration_sql_can_be_generated_for_review(database_urls: DatabaseUrls) -> None:
    """Offline mode: a DBA can read the exact SQL before it is applied."""
    result = run_alembic(database_urls.admin, "upgrade", "head", "--sql")

    assert result.returncode == 0, result.stderr
    assert "CREATE TABLE tenants" in result.stdout
    assert "ALTER TABLE projects FORCE ROW LEVEL SECURITY" in result.stdout
    assert "CREATE POLICY tenant_isolation ON tasks" in result.stdout
    assert f"GRANT SELECT, INSERT ON audit_logs TO {APP_ROLE}" in result.stdout
