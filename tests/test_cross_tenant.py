"""Cross-tenant leak tests for EVERY endpoint.

Two unrelated, fully populated tenants exist: A (the attacker) and B (the victim).
For every tenant-scoped route the suite proves that A can never read, update,
delete or even *detect* B's data, through each way a client can address a tenant:

1. **Foreign object ID** - A is properly authenticated for tenant A and puts B's
   IDs into the URL. The answer must be the same 404 as for an ID that does not
   exist at all, byte for byte (apart from the request ID).
2. **Foreign tenant header** - A's user token with ``X-Tenant-ID: <B>``. The answer
   must be the same 403 as for a tenant ID that does not exist.
3. **Redirected API key** - A's API key with ``X-Tenant-ID: <B>``.
4. **Collections and filters** - A's lists never contain B's rows, whatever the
   filter.

After every attack B's data is compared with a fingerprint taken before it.

``test_every_tenant_scoped_route_is_in_the_matrix`` fails when a route is added
without a matrix entry, so "every endpoint" stays true as the API grows.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from teamboard.api.deps import get_tenant_context
from teamboard.db.base import Base
from teamboard.main import ROUTERS
from tests.support import AdminDb, ApiDriver, TenantWorld, api_key_headers, assert_problem, unique

Worlds = tuple[TenantWorld, TenantWorld]
Ids = Callable[[TenantWorld], dict[str, str]]


def _no_ids(world: TenantWorld) -> dict[str, str]:
    return {}


@dataclass(frozen=True)
class Case:
    method: str
    path: str  # the route template, exactly as registered
    ids: Ids = _no_ids  # path parameters, taken from a tenant's own data
    body: dict[str, Any] | None = None
    # What A's (fully scoped) API key gets for a *missing* object: 404 normally, 403
    # where API keys are not allowed at all. Either way B's objects must look the same.
    api_key_status: int = 404
    collection_id_field: str | None = None  # set for list endpoints
    # Filter values taken from a tenant's own data (list endpoints with filters only).
    filters: Callable[[TenantWorld], list[dict[str, str]]] | None = None

    @property
    def id(self) -> str:
        return f"{self.method} {self.path}"

    @property
    def has_object_id(self) -> bool:
        return "{" in self.path


MATRIX: list[Case] = [
    # ---- tenant
    Case("GET", "/v1/tenants/current"),
    Case("PATCH", "/v1/tenants/current", body={"name": "pwned"}),
    Case("GET", "/v1/tenants/current/usage"),
    # ---- members
    Case("GET", "/v1/members", collection_id_field="user_id"),
    Case(
        "PATCH",
        "/v1/members/{user_id}",
        ids=lambda w: {"user_id": w.member.id},
        body={"role": "viewer"},
        api_key_status=403,
    ),
    Case(
        "DELETE",
        "/v1/members/{user_id}",
        ids=lambda w: {"user_id": w.member.id},
        api_key_status=403,
    ),
    # ---- invitations
    Case("POST", "/v1/invitations", body={"email": "planted@example.com", "role": "admin"}),
    Case("GET", "/v1/invitations", collection_id_field="id"),
    Case(
        "DELETE",
        "/v1/invitations/{invitation_id}",
        ids=lambda w: {"invitation_id": w.invitation["id"]},
        api_key_status=403,
    ),
    # ---- API keys
    Case("POST", "/v1/api-keys", body={"name": "planted", "scopes": ["projects:read"]}),
    Case("GET", "/v1/api-keys", collection_id_field="id"),
    Case(
        "DELETE",
        "/v1/api-keys/{key_id}",
        ids=lambda w: {"key_id": w.api_key["id"]},
        api_key_status=403,
    ),
    # ---- projects
    Case("POST", "/v1/projects", body={"name": "planted"}),
    Case(
        "GET",
        "/v1/projects",
        collection_id_field="id",
        filters=lambda w: [{"q": w.project["name"]}, {"status": "active"}],
    ),
    Case("GET", "/v1/projects/{project_id}", ids=lambda w: {"project_id": w.project["id"]}),
    Case(
        "PATCH",
        "/v1/projects/{project_id}",
        ids=lambda w: {"project_id": w.project["id"]},
        body={"name": "pwned", "status": "archived"},
    ),
    Case("DELETE", "/v1/projects/{project_id}", ids=lambda w: {"project_id": w.project["id"]}),
    Case(
        "POST",
        "/v1/projects/{project_id}/restore",
        ids=lambda w: {"project_id": w.deleted_project["id"]},
    ),
    Case(
        "POST",
        "/v1/projects/{project_id}/tasks",
        ids=lambda w: {"project_id": w.project["id"]},
        body={"title": "planted"},
    ),
    # ---- tasks
    Case(
        "GET",
        "/v1/tasks",
        collection_id_field="id",
        filters=lambda w: [
            {"project_id": w.project["id"]},
            {"assignee_id": w.member.id},
            {"q": w.task["title"]},
        ],
    ),
    Case("GET", "/v1/tasks/{task_id}", ids=lambda w: {"task_id": w.task["id"]}),
    Case(
        "PATCH",
        "/v1/tasks/{task_id}",
        ids=lambda w: {"task_id": w.task["id"]},
        body={"title": "pwned", "status": "done"},
    ),
    Case("DELETE", "/v1/tasks/{task_id}", ids=lambda w: {"task_id": w.task["id"]}),
    Case("POST", "/v1/tasks/{task_id}/restore", ids=lambda w: {"task_id": w.deleted_task["id"]}),
    # ---- audit log
    Case(
        "GET",
        "/v1/audit-logs",
        collection_id_field="entity_id",
        filters=lambda w: [
            {"entity_id": w.project["id"]},
            {"actor_id": w.ws.owner.id},
            {"entity_type": "project"},
        ],
    ),
    # ---- exports
    Case("POST", "/v1/exports"),
    Case("GET", "/v1/exports", collection_id_field="id"),
    Case("GET", "/v1/exports/{export_id}", ids=lambda w: {"export_id": w.export["id"]}),
    Case("GET", "/v1/exports/{export_id}/download", ids=lambda w: {"export_id": w.export["id"]}),
]

# Routes that do not act on a tenant. Each is listed with the reason it cannot
# leak tenant data, and has its own test further down where data is involved.
GLOBAL_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/healthz"): "returns no data",
    ("GET", "/readyz"): "returns no data",
    ("POST", "/v1/auth/register"): "creates the caller's own account",
    ("POST", "/v1/auth/login"): "authenticates the caller",
    ("POST", "/v1/auth/refresh"): "bound to the presented refresh token",
    ("POST", "/v1/auth/logout"): "bound to the presented refresh token",
    ("GET", "/v1/auth/me"): "returns the caller's own account",
    ("POST", "/v1/tenants"): "creates a new tenant owned by the caller",
    ("GET", "/v1/tenants"): "lists the caller's own memberships",
    ("POST", "/v1/invitations/accept"): "bound to the presented invitation token and email",
}

OBJECT_CASES = [case for case in MATRIX if case.has_object_id]
COLLECTION_CASES = [case for case in MATRIX if case.collection_id_field]


def _params(cases: list[Case]) -> list[Any]:
    return [pytest.param(case, id=case.id) for case in cases]


def _random_ids(case: Case, world: TenantWorld) -> dict[str, str]:
    return {name: str(uuid.uuid4()) for name in case.ids(world)}


async def _send(
    client: httpx.AsyncClient, case: Case, ids: dict[str, str], headers: dict[str, str]
) -> httpx.Response:
    return await client.request(
        case.method, case.path.format(**ids), json=case.body, headers=headers
    )


def _comparable(response: httpx.Response) -> tuple[int, dict[str, str], dict[str, Any]]:
    """Everything an attacker could compare between two responses: status, all
    headers (including the body length) and the body itself."""
    body = response.json()
    body.pop("request_id")
    body.pop("instance")  # echoes the URL the attacker chose themselves
    headers = {
        name: value
        for name, value in response.headers.items()
        if name not in ("x-request-id", "ratelimit-remaining")  # differ on every request
    }
    return response.status_code, headers, body


async def fingerprint(db: AdminDb, world: TenantWorld) -> dict[str, str]:
    """A digest of every row the tenant owns, per table."""
    tenant = uuid.UUID(world.tenant_id)
    digests = {
        table: await db.scalar(
            f"SELECT coalesce(md5(string_agg(t::text, '|' ORDER BY t::text)), '') "
            f"FROM {table} t WHERE tenant_id = :tenant",
            tenant=tenant,
        )
        for table, definition in Base.metadata.tables.items()
        if "tenant_id" in definition.columns
    }
    digests["tenants"] = await db.scalar(
        "SELECT md5(t::text) FROM tenants t WHERE id = :tenant", tenant=tenant
    )
    digests["users"] = await db.scalar(
        "SELECT md5(string_agg(u::text, '|' ORDER BY u::text)) FROM users u "
        "WHERE id IN (SELECT user_id FROM memberships WHERE tenant_id = :tenant)",
        tenant=tenant,
    )
    return digests


# ------------------------------------------------------------- completeness


def _api_routes() -> dict[tuple[str, str], APIRoute]:
    """Every route object of the application, keyed by (method, path)."""
    return {
        (method, route.path): route
        for router in ROUTERS
        for route in router.routes
        if isinstance(route, APIRoute)
        for method in route.methods or ()
    }


def _published_operations(app: FastAPI) -> set[tuple[str, str]]:
    """Every operation the running application actually serves (from OpenAPI)."""
    return {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        for method in operations
    }


def _requires_tenant_context(dependant: Dependant) -> bool:
    return dependant.call is get_tenant_context or any(
        _requires_tenant_context(sub) for sub in dependant.dependencies
    )


def test_route_inventory_matches_what_the_application_serves(app: FastAPI) -> None:
    """The two views of "all endpoints" used below agree, and are not empty."""
    assert set(_api_routes()) == _published_operations(app)
    assert len(_api_routes()) == 39


def test_every_tenant_scoped_route_is_in_the_matrix(app: FastAPI) -> None:
    """Adding an endpoint without a cross-tenant test case fails here."""
    routes = _published_operations(app)
    matrix = [(case.method, case.path) for case in MATRIX]

    assert len(matrix) == len(set(matrix)), "duplicate matrix entry"
    assert set(GLOBAL_ROUTES) <= routes, "stale entry in GLOBAL_ROUTES"
    assert routes - set(GLOBAL_ROUTES) == set(matrix)
    assert len(matrix) == 29


def test_tenant_context_is_required_on_exactly_the_matrix_routes() -> None:
    """Structural check: a route is either explicitly global, or it cannot run
    without an authorized tenant context (which also scopes the DB session)."""
    routes = _api_routes()
    assert routes
    for key, route in routes.items():
        assert _requires_tenant_context(route.dependant) == (key not in GLOBAL_ROUTES), key


def test_the_victim_fixture_is_fully_populated(worlds: Worlds) -> None:
    """Guards the matrix against passing vacuously on empty data."""
    _, b = worlds
    assert len(b.all_ids) == 12
    for case in OBJECT_CASES:
        assert all(case.ids(b).values()), case.id


# ------------------------------------------------- 1. foreign object IDs


@pytest.mark.parametrize("case", _params(OBJECT_CASES))
async def test_foreign_object_is_indistinguishable_from_a_missing_one(
    case: Case, worlds: Worlds, client: httpx.AsyncClient, db: AdminDb
) -> None:
    """A's owner (the most privileged role) addresses B's object by its real ID."""
    a, b = worlds
    before = await fingerprint(db, b)

    foreign = await _send(client, case, case.ids(b), a.ws.headers())
    missing = await _send(client, case, _random_ids(case, b), a.ws.headers())

    assert_problem(foreign, 404, "not_found")
    assert _comparable(foreign) == _comparable(missing)
    assert not (b.all_ids & set(foreign.text.replace('"', " ").split())), "response echoes B's data"
    assert await fingerprint(db, b) == before


@pytest.mark.parametrize("case", _params(OBJECT_CASES))
async def test_foreign_object_is_indistinguishable_for_an_api_key(
    case: Case, worlds: Worlds, client: httpx.AsyncClient, db: AdminDb
) -> None:
    a, b = worlds
    headers = api_key_headers(a.api_key_secret)
    before = await fingerprint(db, b)

    foreign = await _send(client, case, case.ids(b), headers)
    missing = await _send(client, case, _random_ids(case, b), headers)

    assert foreign.status_code == case.api_key_status
    assert _comparable(foreign) == _comparable(missing)
    assert await fingerprint(db, b) == before


@pytest.mark.parametrize("role", ["admin", "member", "viewer"])
@pytest.mark.parametrize("case", _params(OBJECT_CASES))
async def test_foreign_object_is_indistinguishable_for_every_role(
    case: Case, role: str, worlds: Worlds, client: httpx.AsyncClient
) -> None:
    """Lower roles may be refused earlier (403) - but never differently for an
    object that exists in another tenant than for one that does not exist."""
    a, b = worlds
    headers = a.ws.headers(getattr(a, role))

    foreign = await _send(client, case, case.ids(b), headers)
    missing = await _send(client, case, _random_ids(case, b), headers)

    assert foreign.status_code in (403, 404)
    assert _comparable(foreign) == _comparable(missing)


# ------------------------------------------------ 2. foreign tenant header


@pytest.mark.parametrize("case", _params(MATRIX))
async def test_user_token_cannot_be_pointed_at_another_tenant(
    case: Case, worlds: Worlds, client: httpx.AsyncClient, db: AdminDb
) -> None:
    """A's owner sends X-Tenant-ID of B, with B's real object IDs in the URL."""
    a, b = worlds
    before = await fingerprint(db, b)

    foreign = await _send(
        client, case, case.ids(b), {**a.ws.owner.auth, "X-Tenant-ID": b.tenant_id}
    )
    missing = await _send(
        client, case, case.ids(b), {**a.ws.owner.auth, "X-Tenant-ID": str(uuid.uuid4())}
    )

    assert_problem(foreign, 403, "tenant_access_denied")
    assert _comparable(foreign) == _comparable(missing)
    assert await fingerprint(db, b) == before


# ----------------------------------------------- 3. redirected API key


@pytest.mark.parametrize("case", _params(MATRIX))
async def test_api_key_cannot_be_pointed_at_another_tenant(
    case: Case, worlds: Worlds, client: httpx.AsyncClient, db: AdminDb
) -> None:
    a, b = worlds
    key = api_key_headers(a.api_key_secret)
    before = await fingerprint(db, b)

    foreign = await _send(client, case, case.ids(b), {**key, "X-Tenant-ID": b.tenant_id})
    missing = await _send(client, case, case.ids(b), {**key, "X-Tenant-ID": str(uuid.uuid4())})

    assert_problem(foreign, 403, "tenant_access_denied")
    assert _comparable(foreign) == _comparable(missing)
    assert await fingerprint(db, b) == before


# -------------------------------------------- 4. collections and filters


@pytest.mark.parametrize("case", _params(COLLECTION_CASES))
async def test_collections_never_contain_another_tenants_rows(
    case: Case, worlds: Worlds, api: ApiDriver
) -> None:
    a, b = worlds
    assert case.collection_id_field is not None
    credentials = [a.ws.headers(), a.ws.headers(a.admin)]
    if case.path not in ("/v1/invitations", "/v1/api-keys"):  # not available to API keys
        credentials.append(api_key_headers(a.api_key_secret))

    for headers in credentials:
        items = await api.collect(case.path, headers, limit=100)
        assert items, f"{case.id}: expected A to have rows of its own"
        assert not ({str(item[case.collection_id_field]) for item in items} & b.all_ids)
        assert not any(b_id in str(item) for item in items for b_id in b.all_ids)


@pytest.mark.parametrize("case", _params([c for c in COLLECTION_CASES if c.filters]))
async def test_filtering_by_another_tenants_values_finds_nothing(
    case: Case, worlds: Worlds, api: ApiDriver
) -> None:
    """Filters are applied *inside* A's scope: asking for B's project, assignee,
    name or actor yields the same empty page as asking for random values."""
    a, b = worlds
    assert case.filters is not None
    for filters in case.filters(b):
        with_foreign_value = await api.collect(case.path, a.ws.headers(), **filters)
        # The same filter does match for B itself, so the test is not vacuous.
        for_owner = await api.collect(case.path, b.ws.headers(), **filters)

        if set(filters) & {"status", "entity_type"}:  # generic values match A's own rows
            assert not any(b_id in str(with_foreign_value) for b_id in b.all_ids)
        else:
            assert with_foreign_value == [], filters
        assert for_owner, filters


# ---------------------------------------------- 5. references in bodies


async def test_tasks_cannot_reference_another_tenants_members(
    worlds: Worlds, client: httpx.AsyncClient, db: AdminDb
) -> None:
    a, b = worlds
    before = await fingerprint(db, b)

    responses = []
    for assignee in (b.member.id, str(uuid.uuid4())):
        responses.append(
            await client.post(
                f"/v1/projects/{a.project['id']}/tasks",
                json={"title": "x", "assignee_id": assignee},
                headers=a.ws.headers(),
            )
        )
        responses.append(
            await client.patch(
                f"/v1/tasks/{a.task['id']}", json={"assignee_id": assignee}, headers=a.ws.headers()
            )
        )

    for response in responses:
        assert_problem(response, 422, "invalid_assignee")
    assert _comparable(responses[0]) == _comparable(responses[2])
    assert _comparable(responses[1]) == _comparable(responses[3])
    assert await fingerprint(db, b) == before


async def test_another_tenants_idempotency_key_does_not_replay_their_response(
    worlds: Worlds, client: httpx.AsyncClient
) -> None:
    a, b = worlds

    response = await client.post(
        "/v1/exports", headers={**a.ws.headers(), "Idempotency-Key": b.export_idempotency_key}
    )

    assert response.status_code == 202
    assert "Idempotent-Replayed" not in response.headers
    assert response.json()["id"] != b.export["id"]


async def test_names_in_use_by_another_tenant_cause_no_conflict(
    worlds: Worlds, client: httpx.AsyncClient
) -> None:
    """A unique-constraint error would be an existence oracle."""
    a, b = worlds

    response = await client.post(
        "/v1/projects", json={"name": b.project["name"]}, headers=a.ws.headers()
    )

    assert response.status_code == 201


async def test_inviting_a_member_of_another_tenant_reveals_nothing(
    worlds: Worlds, client: httpx.AsyncClient
) -> None:
    a, b = worlds

    known = await client.post(
        "/v1/invitations", json={"email": b.member.email}, headers=a.ws.headers()
    )
    unknown = await client.post(
        "/v1/invitations", json={"email": f"{unique('nobody')}@example.com"}, headers=a.ws.headers()
    )

    assert known.status_code == unknown.status_code == 201
    assert known.json()["invited_by_user_id"] == unknown.json()["invited_by_user_id"]


# ------------------------------------------------------- 6. global routes


async def test_me_and_tenant_list_expose_only_the_callers_own_data(
    worlds: Worlds, client: httpx.AsyncClient
) -> None:
    a, b = worlds

    me = await client.get("/v1/auth/me", headers=a.viewer.auth)
    tenants = await client.get("/v1/tenants", headers=a.viewer.auth)

    assert me.json()["id"] == a.viewer.id
    assert [t["id"] for t in tenants.json()] == [a.tenant_id]
    assert not any(b_id in me.text + tenants.text for b_id in b.all_ids)


async def test_invitation_token_of_another_tenant_cannot_be_redeemed_by_a_stranger(
    worlds: Worlds, client: httpx.AsyncClient, db: AdminDb
) -> None:
    """Even with B's invitation token in hand, A's user gets no membership in B."""
    a, b = worlds
    before = await fingerprint(db, b)

    response = await client.post(
        "/v1/invitations/accept", json={"token": b.invitation["token"]}, headers=a.ws.owner.auth
    )

    assert_problem(response, 403, "invitation_email_mismatch")
    assert b.tenant_id not in response.text
    assert await fingerprint(db, b) == before
    denied = await client.get(
        "/v1/projects", headers={**a.ws.owner.auth, "X-Tenant-ID": b.tenant_id}
    )
    assert_problem(denied, 403, "tenant_access_denied")


async def test_the_victim_can_still_see_all_of_its_own_data(worlds: Worlds, api: ApiDriver) -> None:
    """Sanity check on the whole module: B's data exists and is reachable by B, so
    the 404s above were isolation and not simply missing data."""
    _, b = worlds
    headers = b.ws.headers()

    for path in (
        f"/v1/projects/{b.project['id']}",
        f"/v1/tasks/{b.task['id']}",
        f"/v1/exports/{b.export['id']}",
    ):
        assert (await api.client.get(path, headers=headers)).status_code == 200
    assert (await api.client.get("/v1/projects", headers=headers)).json()["items"][0]["name"] == (
        b.project["name"]
    )
