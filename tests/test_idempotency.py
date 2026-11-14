import asyncio
import uuid

import httpx

from teamboard.api.deps import AppResources
from teamboard.domain import Role
from tests.support import AdminDb, ApiDriver, UserSession, Workspace, assert_problem, unique

REPLAYED = "Idempotent-Replayed"


async def _create_project(
    api: ApiDriver, ws: Workspace, key: str, name: str, user: UserSession | None = None
) -> httpx.Response:
    return await api.client.post(
        "/v1/projects", json={"name": name}, headers={**ws.headers(user), "Idempotency-Key": key}
    )


async def _project_count(db: AdminDb, ws: Workspace) -> int:
    count: int = await db.scalar(
        "SELECT count(*) FROM projects WHERE tenant_id = :t", t=uuid.UUID(ws.tenant_id)
    )
    return count


async def test_retry_with_the_same_key_returns_the_original_response(
    api: ApiDriver, db: AdminDb
) -> None:
    ws = await api.create_workspace()
    key = unique("idem")

    first = await _create_project(api, ws, key, "Once")
    retry = await _create_project(api, ws, key, "Once")

    assert first.status_code == retry.status_code == 201
    assert retry.json() == first.json()
    assert REPLAYED not in first.headers
    assert retry.headers[REPLAYED] == "true"
    assert await _project_count(db, ws) == 1
    stored = await db.scalar(
        "SELECT response_body FROM idempotency_keys WHERE tenant_id = :t AND key = :k",
        t=uuid.UUID(ws.tenant_id),
        k=key,
    )
    assert stored == first.json()


async def test_replay_returns_the_original_response_even_after_later_changes(
    api: ApiDriver,
) -> None:
    ws = await api.create_workspace()
    key = unique("idem")
    first = await _create_project(api, ws, key, "Snapshot")
    await api.client.patch(
        f"/v1/projects/{first.json()['id']}", json={"description": "edited"}, headers=ws.headers()
    )

    retry = await _create_project(api, ws, key, "Snapshot")

    assert retry.json() == first.json()


async def test_same_key_with_a_different_payload_is_rejected(api: ApiDriver, db: AdminDb) -> None:
    ws = await api.create_workspace()
    key = unique("idem")
    await _create_project(api, ws, key, "Original")

    response = await _create_project(api, ws, key, "Something else")

    assert_problem(response, 422, "idempotency_key_reused")
    assert await _project_count(db, ws) == 1


async def test_same_key_from_a_different_caller_is_rejected(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    colleague = await api.add_member(ws, Role.MEMBER)
    key = unique("idem")
    await _create_project(api, ws, key, "Mine")

    response = await _create_project(api, ws, key, "Mine", user=colleague)

    assert_problem(response, 422, "idempotency_key_reused")


async def test_keys_are_scoped_to_the_tenant(api: ApiDriver) -> None:
    """Tenant B using a key that tenant A already used must run its own request -
    it must not be handed tenant A's stored response."""
    a, b = await api.create_workspace(), await api.create_workspace()
    key = "order-1"  # deliberately guessable

    from_a = await _create_project(api, a, key, "Shared name")
    from_b = await _create_project(api, b, key, "Shared name")

    assert from_a.status_code == from_b.status_code == 201
    assert REPLAYED not in from_b.headers
    assert from_b.json()["id"] != from_a.json()["id"]
    assert [p["id"] for p in await api.collect("/v1/projects", b.headers())] == [
        from_b.json()["id"]
    ]


async def test_concurrent_requests_with_one_key_create_exactly_one_resource(
    api: ApiDriver, db: AdminDb
) -> None:
    """Race: ten identical requests at once. They serialize on the key's primary
    key inside PostgreSQL; one executes, nine replay its committed response."""
    ws = await api.create_workspace()
    key = unique("idem")

    responses = await asyncio.gather(
        *(_create_project(api, ws, key, "Exactly once") for _ in range(10))
    )

    assert [r.status_code for r in responses] == [201] * 10
    assert len({r.json()["id"] for r in responses}) == 1
    assert sum(REPLAYED in r.headers for r in responses) == 9
    assert await _project_count(db, ws) == 1
    audited = await db.scalar(
        "SELECT count(*) FROM audit_logs WHERE tenant_id = :t AND action = 'project.created'",
        t=uuid.UUID(ws.tenant_id),
    )
    assert audited == 1


async def test_a_failed_request_does_not_consume_the_key(api: ApiDriver, db: AdminDb) -> None:
    """The key is claimed in the operation's transaction, so a rolled-back
    operation leaves no record and the client can retry with a corrected request."""
    ws = await api.create_workspace()
    await api.create_project(ws, name="Taken")
    key = unique("idem")

    failed = await _create_project(api, ws, key, "Taken")
    records = await db.scalar(
        "SELECT count(*) FROM idempotency_keys WHERE tenant_id = :t", t=uuid.UUID(ws.tenant_id)
    )
    corrected = await _create_project(api, ws, key, "Free name")

    assert_problem(failed, 409, "project_name_taken")
    assert records == 0
    assert corrected.status_code == 201
    assert REPLAYED not in corrected.headers


async def test_an_expired_key_can_be_used_again(api: ApiDriver, db: AdminDb) -> None:
    ws = await api.create_workspace()
    key = unique("idem")
    first = await _create_project(api, ws, key, "Old")
    await db.execute(
        "UPDATE idempotency_keys SET expires_at = now() - interval '1 second' WHERE key = :k", k=key
    )

    second = await _create_project(api, ws, key, "New")
    replay = await _create_project(api, ws, key, "New")

    assert second.status_code == 201
    assert REPLAYED not in second.headers
    assert second.json()["id"] != first.json()["id"]
    assert replay.json() == second.json()
    assert replay.headers[REPLAYED] == "true"


async def test_without_a_key_every_request_is_executed(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    project = await api.create_project(ws)

    first = await api.create_task(ws, project["id"], title="twice")
    second = await api.create_task(ws, project["id"], title="twice")

    assert first["id"] != second["id"]


async def test_task_creation_is_idempotent_per_project(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    one, other = await api.create_project(ws), await api.create_project(ws)
    headers = {**ws.headers(), "Idempotency-Key": unique("idem")}
    payload = {"title": "Deploy", "priority": "high"}

    first = await api.client.post(f"/v1/projects/{one['id']}/tasks", json=payload, headers=headers)
    replay = await api.client.post(f"/v1/projects/{one['id']}/tasks", json=payload, headers=headers)
    elsewhere = await api.client.post(
        f"/v1/projects/{other['id']}/tasks", json=payload, headers=headers
    )

    assert first.status_code == 201
    assert replay.json() == first.json()
    assert replay.headers[REPLAYED] == "true"
    # Same key, same body, different project: a different request.
    assert_problem(elsewhere, 422, "idempotency_key_reused")
    assert len(await api.collect("/v1/tasks", ws.headers())) == 1


async def test_a_key_cannot_be_shared_between_different_operations(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    key = unique("idem")
    project = (await _create_project(api, ws, key, "P")).json()

    response = await api.client.post(
        f"/v1/projects/{project['id']}/tasks",
        json={"title": "T"},
        headers={**ws.headers(), "Idempotency-Key": key},
    )

    assert_problem(response, 422, "idempotency_key_reused")


async def test_export_requests_are_idempotent(api: ApiDriver, resources: AppResources) -> None:
    ws = await api.create_workspace()
    headers = {**ws.headers(), "Idempotency-Key": unique("idem")}

    first = await api.client.post("/v1/exports", headers=headers)
    replay = await api.client.post("/v1/exports", headers=headers)

    assert first.status_code == replay.status_code == 202
    assert replay.json() == first.json()
    assert replay.headers[REPLAYED] == "true"
    # One job row, one queue entry, one unit of the daily quota.
    assert len(await api.collect("/v1/exports", ws.headers())) == 1
    assert await resources.redis.zcard("arq:queue") == 1
    usage = (await api.client.get("/v1/tenants/current/usage", headers=ws.headers())).json()
    assert usage["usage"]["exports_today"] == 1


async def test_replay_is_still_subject_to_authorization(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    viewer = await api.add_member(ws, Role.VIEWER)
    key = unique("idem")
    await _create_project(api, ws, key, "Private")

    response = await _create_project(api, ws, key, "Private", user=viewer)

    assert_problem(response, 403, "permission_denied")


async def test_key_length_is_validated(api: ApiDriver) -> None:
    ws = await api.create_workspace()

    too_long = await _create_project(api, ws, "k" * 256, "X")
    longest = await _create_project(api, ws, "k" * 255, "X")

    body = assert_problem(too_long, 422, "validation_error")
    assert body["errors"][0]["field"] == "header.Idempotency-Key"
    assert longest.status_code == 201
