"""Test infrastructure.

The suite runs against real PostgreSQL 16 and Redis 7 containers (testcontainers);
nothing is mocked at the database boundary. The schema is created by running the
actual Alembic migrations as the owner role, and the application connects as the
restricted ``teamboard_app`` role - the same split as in production - so row-level
security is fully in force during every test.

Tests do not clean the database between runs: every test creates its own users and
tenants, and tenant isolation is what keeps them apart.
"""

import os
import subprocess
import sys

# testcontainers' "Ryuk" reaper container intermittently fails to report its port
# on Docker Desktop, which aborts the whole session. The fixtures below stop their
# containers themselves (context managers), so the reaper is only a safety net for
# a killed test process. Export TESTCONTAINERS_RYUK_DISABLED=false to re-enable it.
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

from teamboard.api.deps import AppResources
from teamboard.config import Settings
from teamboard.main import create_app
from tests.support import AdminDb, ApiDriver, TenantWorld, build_tenant_world

ROOT = Path(__file__).resolve().parent.parent

OWNER_ROLE = "teamboard_owner"
OWNER_PASSWORD = "owner-test-password"
APP_ROLE = "teamboard_app"
APP_PASSWORD = "app-test-password"
DATABASE = "teamboard"


@dataclass(frozen=True)
class DatabaseUrls:
    admin: str  # schema owner; a superuser inside the test container (bypasses RLS)
    app: str  # restricted runtime role (subject to RLS)


def run_alembic(admin_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the Alembic CLI the same way a deployment would."""
    return subprocess.run(  # noqa: S603 - fixed command, test-only
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env={**os.environ, "MIGRATION_DATABASE_URL": admin_url, "APP_DB_ROLE": APP_ROLE},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(scope="session")
def database_urls() -> Iterator[DatabaseUrls]:
    with PostgresContainer(
        "postgres:16-alpine",
        username=OWNER_ROLE,
        password=OWNER_PASSWORD,
        dbname=DATABASE,
        driver="asyncpg",
    ) as postgres:
        exit_code, output = postgres.exec(
            [
                "psql",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                OWNER_ROLE,
                "-d",
                DATABASE,
                "-c",
                f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{APP_PASSWORD}' "
                "NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS",
            ]
        )
        assert exit_code == 0, output

        admin_url = postgres.get_connection_url()
        host, port = postgres.get_container_host_ip(), postgres.get_exposed_port(5432)
        app_url = f"postgresql+asyncpg://{APP_ROLE}:{APP_PASSWORD}@{host}:{port}/{DATABASE}"

        migration = run_alembic(admin_url, "upgrade", "head")
        assert migration.returncode == 0, migration.stderr
        yield DatabaseUrls(admin=admin_url, app=app_url)


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    with RedisContainer("redis:7-alpine") as redis:
        yield f"redis://{redis.get_container_host_ip()}:{redis.get_exposed_port(6379)}/0"


@pytest.fixture(scope="session")
def settings(database_urls: DatabaseUrls, redis_url: str) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        log_level="WARNING",
        database_url=database_urls.app,
        redis_url=redis_url,
        jwt_secret="test-only-jwt-secret-with-enough-length",
        # Cheapest valid Argon2 parameters: the suite hashes hundreds of passwords.
        argon2_time_cost=1,
        argon2_memory_cost_kib=8,
        argon2_parallelism=1,
        # All test requests share one client address; the dedicated rate-limit tests
        # lower this again.
        auth_rate_limit_per_minute=1_000_000,
        export_retry_base_seconds=0.02,
        export_max_attempts=3,
    )


@pytest_asyncio.fixture(scope="session")
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture(scope="session")
def resources(app: FastAPI) -> AppResources:
    resources: AppResources = app.state.resources
    return resources


@pytest_asyncio.fixture(scope="session")
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """In-process HTTP client: full ASGI stack, no network."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


@pytest_asyncio.fixture(scope="session")
async def admin_engine(database_urls: DatabaseUrls) -> AsyncIterator[AsyncEngine]:
    """Superuser connection for test setup and for inspecting data across tenants."""
    engine = create_async_engine(database_urls.admin)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(scope="session")
async def app_engine(database_urls: DatabaseUrls) -> AsyncIterator[AsyncEngine]:
    """A separate engine logged in as the restricted application role, for tests
    that talk to PostgreSQL directly to exercise row-level security."""
    engine = create_async_engine(database_urls.app)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _clean_redis(resources: AppResources) -> None:
    """Rate limits, counters and queues start empty for every test."""
    await resources.redis.flushdb()


@pytest.fixture
def api(client: httpx.AsyncClient, admin_engine: AsyncEngine) -> ApiDriver:
    return ApiDriver(client, admin_engine)


@pytest_asyncio.fixture(scope="module")
async def worlds(
    client: httpx.AsyncClient, admin_engine: AsyncEngine, resources: AppResources
) -> tuple[TenantWorld, TenantWorld]:
    """Two unrelated, fully populated tenants ("A" and "B") for isolation tests."""
    await resources.redis.flushdb()
    driver = ApiDriver(client, admin_engine)
    return await build_tenant_world(driver), await build_tenant_world(driver)


@pytest.fixture
def db(admin_engine: AsyncEngine) -> AdminDb:
    return AdminDb(admin_engine)
