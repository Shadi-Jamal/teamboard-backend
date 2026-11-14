import uuid

import httpx
import pytest

from teamboard.domain import Plan, Role
from tests.support import AdminDb, ApiDriver, api_key_headers, assert_problem, unique


async def test_create_tenant_makes_the_caller_owner(api: ApiDriver, db: AdminDb) -> None:
    user = await api.register()
    slug = unique("acme")

    response = await api.client.post(
        "/v1/tenants", json={"name": " Acme Inc. ", "slug": slug}, headers=user.auth
    )

    assert response.status_code == 201
    body = response.json()
    assert (body["name"], body["slug"], body["plan"], body["role"]) == (
        "Acme Inc.",
        slug,
        "free",
        "owner",
    )
    role = await db.scalar(
        "SELECT role FROM memberships WHERE tenant_id = :t AND user_id = :u",
        t=uuid.UUID(body["id"]),
        u=uuid.UUID(user.id),
    )
    assert role == "owner"


async def test_slug_is_globally_unique(api: ApiDriver) -> None:
    first = await api.create_workspace()
    other_user = await api.register()

    response = await api.client.post(
        "/v1/tenants", json={"name": "Copycat", "slug": first.slug}, headers=other_user.auth
    )

    assert_problem(response, 409, "slug_taken")


@pytest.mark.parametrize(
    "slug", ["ab", "Has-Upper", "-leading", "trailing-", "with space", "a" * 41]
)
async def test_slug_format_is_validated(api: ApiDriver, slug: str) -> None:
    user = await api.register()

    response = await api.client.post(
        "/v1/tenants", json={"name": "X", "slug": slug}, headers=user.auth
    )

    body = assert_problem(response, 422, "validation_error")
    assert body["errors"][0]["field"] == "body.slug"


async def test_list_my_tenants_shows_only_memberships_with_roles(api: ApiDriver) -> None:
    mine = await api.create_workspace()
    theirs = await api.create_workspace()
    guest = await api.add_member(theirs, Role.VIEWER)
    second = await api.create_workspace(owner=guest)

    response = await api.client.get("/v1/tenants", headers=guest.auth)

    assert response.status_code == 200
    listed = {item["id"]: item["role"] for item in response.json()}
    assert listed == {theirs.tenant_id: "viewer", second.tenant_id: "owner"}
    assert mine.tenant_id not in listed


async def test_a_user_can_work_in_several_tenants_with_different_roles(api: ApiDriver) -> None:
    home = await api.create_workspace()
    away = await api.create_workspace()
    user = await api.add_member(away, Role.VIEWER)
    home_as_owner = await api.create_workspace(owner=user)

    can_write_at_home = await api.client.post(
        "/v1/projects", json={"name": "mine"}, headers=home_as_owner.headers()
    )
    cannot_write_away = await api.client.post(
        "/v1/projects", json={"name": "not mine"}, headers=away.headers(user)
    )
    no_access = await api.client.get("/v1/projects", headers=home.headers(user))

    assert can_write_at_home.status_code == 201
    assert_problem(cannot_write_away, 403, "permission_denied")
    assert_problem(no_access, 403, "tenant_access_denied")


async def test_tenant_scoped_endpoints_require_the_tenant_header(api: ApiDriver) -> None:
    ws = await api.create_workspace()

    response = await api.client.get("/v1/tenants/current", headers=ws.owner.auth)

    assert_problem(response, 400, "tenant_required")


async def test_malformed_tenant_header_is_a_validation_error(api: ApiDriver) -> None:
    ws = await api.create_workspace()

    response = await api.client.get(
        "/v1/tenants/current", headers={**ws.owner.auth, "X-Tenant-ID": "not-a-uuid"}
    )

    body = assert_problem(response, 422, "validation_error")
    assert body["errors"][0]["field"] == "header.X-Tenant-ID"


async def test_foreign_and_nonexistent_tenants_are_indistinguishable(api: ApiDriver) -> None:
    """A user probing tenant IDs learns nothing: same status, same body."""
    ws = await api.create_workspace()
    foreign = await api.create_workspace()

    exists = await api.client.get(
        "/v1/tenants/current", headers={**ws.owner.auth, "X-Tenant-ID": foreign.tenant_id}
    )
    missing = await api.client.get(
        "/v1/tenants/current", headers={**ws.owner.auth, "X-Tenant-ID": str(uuid.uuid4())}
    )

    first = assert_problem(exists, 403, "tenant_access_denied")
    second = assert_problem(missing, 403, "tenant_access_denied")
    first.pop("request_id")
    second.pop("request_id")
    assert first == second


async def test_get_and_rename_current_tenant(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    admin = await api.add_member(ws, Role.ADMIN)
    member = await api.add_member(ws, Role.MEMBER)

    renamed = await api.client.patch(
        "/v1/tenants/current", json={"name": "Renamed"}, headers=ws.headers(admin)
    )
    fetched = await api.client.get("/v1/tenants/current", headers=ws.headers(member))
    denied = await api.client.patch(
        "/v1/tenants/current", json={"name": "Nope"}, headers=ws.headers(member)
    )

    assert renamed.status_code == 200
    assert fetched.json()["name"] == "Renamed"
    assert fetched.json()["id"] == ws.tenant_id
    assert renamed.json()["updated_at"] > renamed.json()["created_at"]
    assert_problem(denied, 403, "permission_denied")


async def test_usage_reports_limits_and_current_consumption(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    await api.add_member(ws, Role.MEMBER)
    await api.create_project(ws)
    await api.create_project(ws)
    await api.create_api_key(ws, ["projects:read"])
    await api.client.post("/v1/exports", headers=ws.headers())

    response = await api.client.get("/v1/tenants/current/usage", headers=ws.headers())

    assert response.status_code == 200
    assert response.json() == {
        "plan": "free",
        "limits": {
            "requests_per_minute": 120,
            "max_projects": 5,
            "max_members": 5,
            "max_api_keys": 2,
            "exports_per_day": 5,
        },
        "usage": {"projects": 2, "members": 2, "api_keys": 1, "exports_today": 1},
    }


async def test_plan_changes_take_effect_immediately(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    await api.set_plan(ws.tenant_id, Plan.PRO)

    response = await api.client.get("/v1/tenants/current/usage", headers=ws.headers())

    assert response.json()["plan"] == "pro"
    assert response.json()["limits"]["max_projects"] == 200


async def test_api_keys_cannot_use_user_account_endpoints(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    _, key = await api.create_api_key(ws, ["tenant:read"])

    for request in (
        api.client.get("/v1/tenants", headers=api_key_headers(key)),
        api.client.post(
            "/v1/tenants", json={"name": "X", "slug": unique("x")}, headers=api_key_headers(key)
        ),
        api.client.get("/v1/auth/me", headers=api_key_headers(key)),
    ):
        assert_problem(await request, 403, "user_token_required")


async def test_unauthenticated_requests_are_rejected_before_anything_else(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/v1/tenants/current", headers={"X-Tenant-ID": str(uuid.uuid4())})

    assert_problem(response, 401, "not_authenticated")
