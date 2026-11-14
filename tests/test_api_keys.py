import uuid
from datetime import UTC, datetime, timedelta

import pytest

from teamboard.domain import Plan, Role
from teamboard.security.tokens import hash_token
from tests.support import AdminDb, ApiDriver, api_key_headers, assert_problem


async def test_key_is_shown_once_and_stored_only_as_a_hash(api: ApiDriver, db: AdminDb) -> None:
    ws = await api.create_workspace()

    created, key = await api.create_api_key(ws, ["projects:read", "tasks:read"])

    assert key.startswith("tbk_")
    assert created["prefix"] == key[:12]
    assert created["scopes"] == ["projects:read", "tasks:read"]
    assert created["created_by_user_id"] == ws.owner.id
    assert created["revoked_at"] is None

    listed = (await api.client.get("/v1/api-keys", headers=ws.headers())).json()["items"]
    assert [k["id"] for k in listed] == [created["id"]]
    assert "key" not in listed[0]

    stored = await db.column(
        "SELECT key_hash FROM api_keys WHERE id = :id", id=uuid.UUID(created["id"])
    )
    assert stored == [hash_token(key)]
    everything = await db.column(
        "SELECT row_to_json(k)::text FROM api_keys k WHERE id = :id", id=uuid.UUID(created["id"])
    )
    assert key not in everything[0]  # the secret appears in no column


async def test_api_key_authenticates_for_its_tenant_without_a_tenant_header(
    api: ApiDriver,
) -> None:
    ws = await api.create_workspace()
    project = await api.create_project(ws)
    _, key = await api.create_api_key(ws, ["projects:read"])

    response = await api.client.get("/v1/projects", headers=api_key_headers(key))
    with_matching_header = await api.client.get(
        "/v1/projects", headers={**api_key_headers(key), "X-Tenant-ID": ws.tenant_id}
    )

    assert response.status_code == 200
    assert [p["id"] for p in response.json()["items"]] == [project["id"]]
    assert with_matching_header.status_code == 200


async def test_api_key_is_limited_to_its_scopes(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    project = await api.create_project(ws)
    _, read_only = await api.create_api_key(ws, ["projects:read"])
    headers = api_key_headers(read_only)

    allowed = await api.client.get(f"/v1/projects/{project['id']}", headers=headers)
    write = await api.client.post("/v1/projects", json={"name": "nope"}, headers=headers)
    other_resource = await api.client.get("/v1/tasks", headers=headers)
    admin_area = await api.client.get("/v1/api-keys", headers=headers)

    assert allowed.status_code == 200
    for denied in (write, other_resource, admin_area):
        assert_problem(denied, 403, "permission_denied")


async def test_writes_by_api_key_are_attributed_to_the_key(api: ApiDriver, db: AdminDb) -> None:
    ws = await api.create_workspace()
    created, key = await api.create_api_key(ws, ["projects:write"])

    response = await api.client.post(
        "/v1/projects", json={"name": "from automation"}, headers=api_key_headers(key)
    )

    assert response.status_code == 201
    actor = await db.column(
        "SELECT actor_type || ':' || actor_id FROM audit_logs WHERE entity_id = :id",
        id=uuid.UUID(response.json()["id"]),
    )
    assert actor == [f"api_key:{created['id']}"]


async def test_revoked_key_stops_working_immediately(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    created, key = await api.create_api_key(ws, ["projects:read"])
    assert (await api.client.get("/v1/projects", headers=api_key_headers(key))).status_code == 200

    revoke = await api.client.delete(f"/v1/api-keys/{created['id']}", headers=ws.headers())
    again = await api.client.delete(f"/v1/api-keys/{created['id']}", headers=ws.headers())

    assert revoke.status_code == 204
    assert again.status_code == 204
    after = await api.client.get("/v1/projects", headers=api_key_headers(key))
    assert_problem(after, 401, "invalid_api_key")
    listed = (await api.client.get("/v1/api-keys", headers=ws.headers())).json()["items"]
    assert listed[0]["revoked_at"] is not None


async def test_expired_key_is_rejected(api: ApiDriver, db: AdminDb) -> None:
    ws = await api.create_workspace()
    expiry = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    response = await api.client.post(
        "/v1/api-keys",
        json={"name": "short-lived", "scopes": ["projects:read"], "expires_at": expiry},
        headers=ws.headers(),
    )
    key = response.json()["key"]
    assert (await api.client.get("/v1/projects", headers=api_key_headers(key))).status_code == 200

    await db.execute(
        "UPDATE api_keys SET expires_at = now() - interval '1 second' WHERE id = :id",
        id=uuid.UUID(response.json()["id"]),
    )

    after = await api.client.get("/v1/projects", headers=api_key_headers(key))
    assert_problem(after, 401, "invalid_api_key")


@pytest.mark.parametrize("key", ["tbk_this-key-does-not-exist", "not-even-the-right-format"])
async def test_unknown_key_is_rejected(api: ApiDriver, key: str) -> None:
    response = await api.client.get("/v1/projects", headers=api_key_headers(key))

    assert_problem(response, 401, "invalid_api_key")


async def test_empty_key_header_counts_as_no_credentials(api: ApiDriver) -> None:
    response = await api.client.get("/v1/projects", headers=api_key_headers(""))

    assert_problem(response, 401, "not_authenticated")


async def test_sending_both_credential_types_is_rejected(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    _, key = await api.create_api_key(ws, ["projects:read"])

    response = await api.client.get("/v1/projects", headers={**ws.headers(), "X-API-Key": key})

    assert_problem(response, 400, "ambiguous_credentials")


async def test_key_usage_is_recorded_even_for_read_only_requests(
    api: ApiDriver, db: AdminDb
) -> None:
    ws = await api.create_workspace()
    created, key = await api.create_api_key(ws, ["projects:read"])
    assert created["last_used_at"] is None

    await api.client.get("/v1/projects", headers=api_key_headers(key))
    first = await db.scalar(
        "SELECT last_used_at FROM api_keys WHERE id = :id", id=uuid.UUID(created["id"])
    )
    await api.client.get("/v1/projects", headers=api_key_headers(key))
    second = await db.scalar(
        "SELECT last_used_at FROM api_keys WHERE id = :id", id=uuid.UUID(created["id"])
    )

    assert first is not None
    assert second == first  # throttled to once a minute: no write per request


@pytest.mark.parametrize(
    "scopes",
    [["api_keys:manage"], ["members:manage"], ["tenant:write"], ["projects:read", "bogus:scope"]],
)
async def test_administrative_and_unknown_scopes_cannot_be_granted(
    api: ApiDriver, scopes: list[str]
) -> None:
    """No key can manage members, invitations, tenant settings or other keys."""
    ws = await api.create_workspace()

    response = await api.client.post(
        "/v1/api-keys", json={"name": "too powerful", "scopes": scopes}, headers=ws.headers()
    )

    assert_problem(response, 422, "invalid_scope")


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "k", "scopes": []},
        {"name": "", "scopes": ["projects:read"]},
        {"name": "k", "scopes": ["projects:read"], "expires_at": "2099-01-01T00:00:00"},  # naive
    ],
)
async def test_create_validates_input(api: ApiDriver, payload: dict[str, object]) -> None:
    ws = await api.create_workspace()

    response = await api.client.post("/v1/api-keys", json=payload, headers=ws.headers())

    assert_problem(response, 422, "validation_error")


async def test_expiry_must_be_in_the_future(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()

    response = await api.client.post(
        "/v1/api-keys",
        json={"name": "k", "scopes": ["projects:read"], "expires_at": past},
        headers=ws.headers(),
    )

    assert_problem(response, 422, "invalid_expiry")


async def test_only_admins_manage_api_keys(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    member = await api.add_member(ws, Role.MEMBER)
    admin = await api.add_member(ws, Role.ADMIN)
    payload = {"name": "k", "scopes": ["projects:read"]}

    denied = await api.client.post("/v1/api-keys", json=payload, headers=ws.headers(member))
    allowed = await api.client.post("/v1/api-keys", json=payload, headers=ws.headers(admin))

    assert_problem(denied, 403, "permission_denied")
    assert allowed.status_code == 201


async def test_active_keys_are_limited_by_plan_and_revoking_frees_a_slot(api: ApiDriver) -> None:
    ws = await api.create_workspace()  # free plan: 2 active keys
    first, _ = await api.create_api_key(ws, ["projects:read"])
    await api.create_api_key(ws, ["projects:read"])
    payload = {"name": "third", "scopes": ["projects:read"]}

    over_limit = await api.client.post("/v1/api-keys", json=payload, headers=ws.headers())
    await api.client.delete(f"/v1/api-keys/{first['id']}", headers=ws.headers())
    after_revoke = await api.client.post("/v1/api-keys", json=payload, headers=ws.headers())

    assert_problem(over_limit, 403, "api_key_quota_exceeded")
    assert after_revoke.status_code == 201


async def test_unknown_key_cannot_be_revoked(api: ApiDriver) -> None:
    ws = await api.create_workspace(plan=Plan.PRO)

    response = await api.client.delete(f"/v1/api-keys/{uuid.uuid4()}", headers=ws.headers())

    assert_problem(response, 404, "not_found")
