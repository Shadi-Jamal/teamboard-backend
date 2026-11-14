import uuid

from teamboard.domain import Role
from teamboard.services.audit import diff, snapshot
from tests.support import AdminDb, ApiDriver, api_key_headers, assert_problem


def test_diff_reports_only_changed_fields_in_json_safe_form() -> None:
    user = uuid.uuid4()

    changes = diff(
        {"name": "old", "role": Role.MEMBER, "assignee_id": None, "same": 1},
        {"name": "new", "role": Role.ADMIN, "assignee_id": user, "same": 1},
    )

    assert changes == {
        "name": {"old": "old", "new": "new"},
        "role": {"old": "member", "new": "admin"},
        "assignee_id": {"old": None, "new": str(user)},
    }
    assert snapshot({"role": Role.VIEWER}) == {"role": {"old": None, "new": "viewer"}}


async def test_changes_are_recorded_with_actor_time_and_diff(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    member = await api.add_member(ws, Role.MEMBER)
    project = await api.create_project(ws, name="Original")
    await api.client.patch(
        f"/v1/projects/{project['id']}",
        json={"name": "Renamed", "status": "archived"},
        headers=ws.headers(member),
    )
    await api.client.delete(f"/v1/projects/{project['id']}", headers=ws.headers())

    entries = await api.collect(
        "/v1/audit-logs", ws.headers(), entity_type="project", entity_id=project["id"]
    )

    assert [e["action"] for e in entries] == [
        "project.deleted",
        "project.updated",
        "project.created",
    ]
    deleted, updated, created = entries
    assert (created["actor_type"], created["actor_id"]) == ("user", ws.owner.id)
    assert created["changes"]["name"] == {"old": None, "new": "Original"}
    # The update was made by a different user, and only changed fields are listed.
    assert updated["actor_id"] == member.id
    assert updated["changes"] == {
        "name": {"old": "Original", "new": "Renamed"},
        "status": {"old": "active", "new": "archived"},
    }
    assert deleted["actor_id"] == ws.owner.id
    assert created["created_at"] <= updated["created_at"] <= deleted["created_at"]
    assert set(created) == {
        "id",
        "actor_type",
        "actor_id",
        "action",
        "entity_type",
        "entity_id",
        "changes",
        "request_id",
        "created_at",
    }


async def test_audit_entry_carries_the_request_id(api: ApiDriver) -> None:
    ws = await api.create_workspace()

    response = await api.client.post(
        "/v1/projects",
        json={"name": "traced"},
        headers={**ws.headers(), "X-Request-ID": "trace-abc-123"},
    )

    entries = await api.collect("/v1/audit-logs", ws.headers(), entity_id=response.json()["id"])
    assert response.headers["X-Request-ID"] == "trace-abc-123"
    assert [e["request_id"] for e in entries] == ["trace-abc-123"]


async def test_task_changes_are_audited(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    project = await api.create_project(ws)
    task = await api.create_task(ws, project["id"], title="t")
    await api.client.patch(
        f"/v1/tasks/{task['id']}",
        json={"status": "done", "assignee_id": ws.owner.id},
        headers=ws.headers(),
    )
    await api.client.delete(f"/v1/tasks/{task['id']}", headers=ws.headers())
    await api.client.post(f"/v1/tasks/{task['id']}/restore", headers=ws.headers())

    entries = await api.collect("/v1/audit-logs", ws.headers(), entity_id=task["id"])

    assert [e["action"] for e in reversed(entries)] == [
        "task.created",
        "task.updated",
        "task.deleted",
        "task.restored",
    ]
    assert entries[2]["changes"] == {
        "status": {"old": "todo", "new": "done"},
        "assignee_id": {"old": None, "new": ws.owner.id},
    }


async def test_no_op_update_writes_no_audit_entry(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    project = await api.create_project(ws, name="Same")

    response = await api.client.patch(
        f"/v1/projects/{project['id']}", json={"name": "Same"}, headers=ws.headers()
    )

    entries = await api.collect("/v1/audit-logs", ws.headers(), entity_id=project["id"])
    assert response.status_code == 200
    assert response.json()["updated_at"] == project["updated_at"]
    assert [e["action"] for e in entries] == ["project.created"]


async def test_failed_operations_leave_no_audit_trace(api: ApiDriver, db: AdminDb) -> None:
    """The audit entry shares the transaction of the change: when the change is
    rolled back, so is the entry."""
    ws = await api.create_workspace()
    await api.create_project(ws, name="Taken")
    before = await db.scalar(
        "SELECT count(*) FROM audit_logs WHERE tenant_id = :t", t=uuid.UUID(ws.tenant_id)
    )

    conflict = await api.client.post("/v1/projects", json={"name": "Taken"}, headers=ws.headers())

    after = await db.scalar(
        "SELECT count(*) FROM audit_logs WHERE tenant_id = :t", t=uuid.UUID(ws.tenant_id)
    )
    assert_problem(conflict, 409, "project_name_taken")
    assert after == before


async def test_filters(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    key, secret = await api.create_api_key(ws, ["projects:write", "tasks:write"])
    by_user = await api.create_project(ws)
    by_key = (
        await api.client.post("/v1/projects", json={"name": "bot"}, headers=api_key_headers(secret))
    ).json()
    await api.create_task(ws, by_user["id"])

    async def entities(**params: str) -> list[str]:
        return [e["entity_id"] for e in await api.collect("/v1/audit-logs", ws.headers(), **params)]

    assert await entities(action="project.created") == [by_key["id"], by_user["id"]]
    assert await entities(actor_id=key["id"]) == [by_key["id"]]
    assert await entities(entity_type="project", actor_id=ws.owner.id) == [by_user["id"]]
    assert len(await entities(entity_type="task")) == 1
    assert await entities(entity_type="nonexistent") == []


async def test_administrative_actions_are_audited(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    await api.add_member(ws, Role.VIEWER)
    key, _ = await api.create_api_key(ws, ["tasks:read"])
    await api.client.delete(f"/v1/api-keys/{key['id']}", headers=ws.headers())
    await api.client.patch("/v1/tenants/current", json={"name": "New name"}, headers=ws.headers())

    actions = {e["action"] for e in await api.collect("/v1/audit-logs", ws.headers())}

    assert actions == {
        "tenant.created",
        "tenant.updated",
        "invitation.created",
        "member.joined",
        "api_key.created",
        "api_key.revoked",
    }


async def test_secrets_never_appear_in_the_audit_log(api: ApiDriver, db: AdminDb) -> None:
    ws = await api.create_workspace()
    _, key = await api.create_api_key(ws, ["tasks:read"])
    invitation = await api.invite(ws, "secret@example.com", Role.VIEWER)

    dump = "".join(
        await db.column(
            "SELECT row_to_json(a)::text FROM audit_logs a WHERE tenant_id = :t",
            t=uuid.UUID(ws.tenant_id),
        )
    )

    assert key not in dump
    assert invitation["token"] not in dump
    assert key[4:] not in dump


async def test_only_admins_may_read_the_audit_log(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    member = await api.add_member(ws, Role.MEMBER)
    admin = await api.add_member(ws, Role.ADMIN)

    denied = await api.client.get("/v1/audit-logs", headers=ws.headers(member))
    allowed = await api.client.get("/v1/audit-logs", headers=ws.headers(admin))

    assert_problem(denied, 403, "permission_denied")
    assert allowed.status_code == 200


async def test_audit_log_has_no_write_endpoints(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    entry = (await api.collect("/v1/audit-logs", ws.headers()))[0]

    for method in ("POST", "PUT", "PATCH", "DELETE"):
        collection = await api.client.request(method, "/v1/audit-logs", headers=ws.headers())
        item = await api.client.request(
            method, f"/v1/audit-logs/{entry['id']}", headers=ws.headers()
        )
        assert collection.status_code == 405
        assert item.status_code == 404
