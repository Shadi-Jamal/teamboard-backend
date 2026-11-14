"""Isolation in depth, demonstrated end to end over HTTP.

Tenant isolation is enforced twice: by the repositories (application layer) and by
row-level security (database layer). These tests remove one layer at a time and
show the other one still holds - and then remove both, to show that the leak
becomes real. That last test is the control experiment for the whole isolation
suite: it proves the 404s elsewhere are produced by the two layers, and that the
tests are able to see a leak when there is one.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import httpx
import pytest
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncEngine

from teamboard import main as main_module
from teamboard.config import Settings
from teamboard.db.base import TenantOwnedMixin
from teamboard.main import create_app
from teamboard.repositories.base import TenantScopedRepository
from tests.conftest import DatabaseUrls
from tests.support import TenantWorld, assert_problem

Worlds = tuple[TenantWorld, TenantWorld]
ClientFactory = Callable[[], AbstractAsyncContextManager[httpx.AsyncClient]]


@pytest.fixture
def without_rls(
    settings: Settings, database_urls: DatabaseUrls, monkeypatch: pytest.MonkeyPatch
) -> ClientFactory:
    """An application instance connected as a superuser, for which PostgreSQL
    skips every row-level security policy. (The startup check that normally
    forbids exactly this is disabled for the experiment.)"""

    async def skip_role_check(engine: AsyncEngine) -> None:
        return None

    @asynccontextmanager
    async def client() -> AsyncIterator[httpx.AsyncClient]:
        monkeypatch.setattr(main_module, "assert_role_is_subject_to_rls", skip_role_check)
        application = create_app(settings.model_copy(update={"database_url": database_urls.admin}))
        async with (
            application.router.lifespan_context(application),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application), base_url="http://testserver"
            ) as http,
        ):
            yield http

    return client


@pytest.fixture
def without_repository_scoping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate the classic bug: every repository query "forgets" its tenant filter."""

    def unscoped_select[M: TenantOwnedMixin](self: TenantScopedRepository[M]) -> Select[tuple[M]]:
        return select(self.model)

    monkeypatch.setattr(TenantScopedRepository, "_select", unscoped_select)


async def _attack(http: httpx.AsyncClient, a: TenantWorld, b: TenantWorld) -> list[httpx.Response]:
    """Tenant A reads tenant B's project and task by ID, and lists its own projects."""
    return [
        await http.get(f"/v1/projects/{b.project['id']}", headers=a.ws.headers()),
        await http.get(f"/v1/tasks/{b.task['id']}", headers=a.ws.headers()),
        await http.get("/v1/projects", params={"limit": 100}, headers=a.ws.headers()),
    ]


async def test_application_layer_alone_isolates_tenants(
    worlds: Worlds, without_rls: ClientFactory
) -> None:
    """Database protection removed: the repositories' tenant predicate holds."""
    a, b = worlds
    async with without_rls() as http:
        project, task, listing = await _attack(http, a, b)

    assert_problem(project, 404, "not_found")
    assert_problem(task, 404, "not_found")
    assert {p["id"] for p in listing.json()["items"]} == {a.project["id"]}


@pytest.mark.usefixtures("without_repository_scoping")
async def test_row_level_security_alone_isolates_tenants(
    worlds: Worlds, client: httpx.AsyncClient
) -> None:
    """Application protection removed (queries carry no tenant filter at all):
    PostgreSQL returns only the current tenant's rows anyway."""
    a, b = worlds

    project, task, listing = await _attack(client, a, b)

    assert_problem(project, 404, "not_found")
    assert_problem(task, 404, "not_found")
    ids = {p["id"] for p in listing.json()["items"]}
    assert a.project["id"] in ids
    assert not (ids & b.all_ids)


@pytest.mark.usefixtures("without_repository_scoping")
async def test_control_with_both_layers_removed_the_leak_is_real(
    worlds: Worlds, without_rls: ClientFactory
) -> None:
    """No tenant predicate and no RLS: tenant A now reads tenant B's data. This is
    what every other isolation test would look like if the protections failed."""
    a, b = worlds
    async with without_rls() as http:
        project, task, listing = await _attack(http, a, b)

    assert project.status_code == 200
    assert project.json()["name"] == b.project["name"]
    assert task.status_code == 200
    assert b.project["id"] in {p["id"] for p in listing.json()["items"]}
