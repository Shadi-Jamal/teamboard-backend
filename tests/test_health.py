import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import create_async_engine

from teamboard.api.deps import AppResources
from teamboard.config import Settings
from teamboard.db.engine import UnsafeDatabaseRoleError, assert_role_is_subject_to_rls
from teamboard.main import create_app
from tests.conftest import DatabaseUrls


async def test_liveness(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_reports_dependencies(client: httpx.AsyncClient) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"database": "ok", "redis": "ok"}}


async def test_readiness_fails_when_database_is_unreachable(
    client: httpx.AsyncClient, resources: AppResources, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = create_async_engine("postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none")
    monkeypatch.setattr(resources, "engine", broken)

    response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "checks": {"database": "failed", "redis": "ok"},
    }
    await broken.dispose()


async def test_app_role_passes_the_rls_safety_check(resources: AppResources) -> None:
    await assert_role_is_subject_to_rls(resources.engine)


async def test_startup_refuses_a_role_that_bypasses_rls(
    settings: Settings, database_urls: DatabaseUrls
) -> None:
    """Pointing the app at the owner/superuser role must fail loudly at startup,
    because that role ignores every row-level security policy."""
    unsafe = settings.model_copy(update={"database_url": SecretStr(database_urls.admin)})
    application = create_app(unsafe)

    with pytest.raises(UnsafeDatabaseRoleError, match="bypasses row-level security"):
        async with application.router.lifespan_context(application):
            pytest.fail("the application must not start")
