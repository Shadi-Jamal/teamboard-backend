import asyncio
import uuid

import pytest

from teamboard.domain import Plan
from tests.support import AdminDb, ApiDriver, assert_problem, unique


async def test_create_and_get_project(api: ApiDriver) -> None:
    ws = await api.create_workspace()

    created = await api.client.post(
        "/v1/projects",
        json={"name": "  Website relaunch ", "description": "Q3 goal"},
        headers=ws.headers(),
    )

    assert created.status_code == 201
    project = created.json()
    assert (project["name"], project["description"], project["status"]) == (
        "Website relaunch",
        "Q3 goal",
        "active",
    )
    assert set(project) == {"id", "name", "description", "status", "created_at", "updated_at"}
    fetched = await api.client.get(f"/v1/projects/{project['id']}", headers=ws.headers())
    assert fetched.status_code == 200
    assert fetched.json() == project


@pytest.mark.parametrize(
    "payload",
    [{}, {"name": ""}, {"name": "x" * 121}, {"name": "ok", "description": "d" * 10_001}],
)
async def test_create_validates_input(api: ApiDriver, payload: dict[str, str]) -> None:
    ws = await api.create_workspace()

    response = await api.client.post("/v1/projects", json=payload, headers=ws.headers())

    assert_problem(response, 422, "validation_error")


async def test_names_are_unique_per_tenant_case_insensitively(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    await api.create_project(ws, name="Roadmap")

    duplicate = await api.client.post(
        "/v1/projects", json={"name": "ROADMAP"}, headers=ws.headers()
    )

    assert_problem(duplicate, 409, "project_name_taken")


async def test_the_same_name_can_exist_in_different_tenants(api: ApiDriver) -> None:
    """Uniqueness is per tenant, so a name collision can never reveal that another
    tenant has a project of that name."""
    first, second = await api.create_workspace(), await api.create_workspace()
    name = unique("shared-name")

    await api.create_project(first, name=name)
    await api.create_project(second, name=name)


async def test_update_changes_only_the_given_fields(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    project = await api.create_project(ws, name="Before", description="keep me")

    response = await api.client.patch(
        f"/v1/projects/{project['id']}", json={"name": "After"}, headers=ws.headers()
    )

    assert response.status_code == 200
    updated = response.json()
    assert (updated["name"], updated["description"]) == ("After", "keep me")
    assert updated["updated_at"] > project["updated_at"]
    assert updated["created_at"] == project["created_at"]


async def test_update_to_a_taken_name_conflicts(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    await api.create_project(ws, name="Taken")
    project = await api.create_project(ws, name="Other")

    response = await api.client.patch(
        f"/v1/projects/{project['id']}", json={"name": "taken"}, headers=ws.headers()
    )

    assert_problem(response, 409, "project_name_taken")
    unchanged = await api.client.get(f"/v1/projects/{project['id']}", headers=ws.headers())
    assert unchanged.json()["name"] == "Other"


async def test_archived_project_accepts_no_new_tasks(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    project = await api.create_project(ws)
    await api.client.patch(
        f"/v1/projects/{project['id']}", json={"status": "archived"}, headers=ws.headers()
    )

    response = await api.client.post(
        f"/v1/projects/{project['id']}/tasks", json={"title": "late"}, headers=ws.headers()
    )

    assert_problem(response, 409, "project_archived")


async def test_list_filters_by_status_and_name(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    alpha = await api.create_project(ws, name="Alpha launch")
    beta = await api.create_project(ws, name="Beta launch")
    gamma = await api.create_project(ws, name="100% done_")
    await api.client.patch(
        f"/v1/projects/{beta['id']}", json={"status": "archived"}, headers=ws.headers()
    )

    async def ids(**params: str) -> list[str]:
        return [p["id"] for p in await api.collect("/v1/projects", ws.headers(), **params)]

    assert await ids() == [gamma["id"], beta["id"], alpha["id"]]  # newest first
    assert await ids(status="archived") == [beta["id"]]
    assert await ids(status="active") == [gamma["id"], alpha["id"]]
    assert await ids(q="LAUNCH") == [beta["id"], alpha["id"]]
    assert await ids(q="launch", status="active") == [alpha["id"]]
    # LIKE wildcards in the search term are matched literally.
    assert await ids(q="%") == [gamma["id"]]
    assert await ids(q="_") == [gamma["id"]]
    invalid = await api.client.get("/v1/projects", params={"status": "bogus"}, headers=ws.headers())
    assert_problem(invalid, 422, "validation_error")


async def test_soft_delete_hides_the_project_and_its_tasks(api: ApiDriver, db: AdminDb) -> None:
    ws = await api.create_workspace()
    project = await api.create_project(ws, name="Doomed")
    task = await api.create_task(ws, project["id"])

    response = await api.client.delete(f"/v1/projects/{project['id']}", headers=ws.headers())

    assert response.status_code == 204
    headers = ws.headers()
    assert_problem(
        await api.client.get(f"/v1/projects/{project['id']}", headers=headers), 404, "not_found"
    )
    assert_problem(
        await api.client.get(f"/v1/tasks/{task['id']}", headers=headers), 404, "not_found"
    )
    assert await api.collect("/v1/projects", headers) == []
    assert await api.collect("/v1/tasks", headers) == []
    for request in (
        api.client.patch(f"/v1/projects/{project['id']}", json={"name": "zombie"}, headers=headers),
        api.client.delete(f"/v1/projects/{project['id']}", headers=headers),
        api.client.post(
            f"/v1/projects/{project['id']}/tasks", json={"title": "t"}, headers=headers
        ),
    ):
        assert_problem(await request, 404, "not_found")
    # Soft delete: the rows are still there, marked with the same timestamp.
    stamps = await db.column(
        "SELECT deleted_at FROM projects WHERE id = :p "
        "UNION ALL SELECT deleted_at FROM tasks WHERE id = :t",
        p=uuid.UUID(project["id"]),
        t=uuid.UUID(task["id"]),
    )
    assert stamps[0] is not None
    assert stamps[0] == stamps[1]
    # The name is free again for a new project.
    await api.create_project(ws, name="Doomed")


async def test_restore_brings_back_the_project_and_the_tasks_deleted_with_it(
    api: ApiDriver,
) -> None:
    ws = await api.create_workspace()
    project = await api.create_project(ws)
    kept = await api.create_task(ws, project["id"])
    deleted_earlier = await api.create_task(ws, project["id"])
    await api.client.delete(f"/v1/tasks/{deleted_earlier['id']}", headers=ws.headers())
    await api.client.delete(f"/v1/projects/{project['id']}", headers=ws.headers())

    restored = await api.client.post(f"/v1/projects/{project['id']}/restore", headers=ws.headers())
    again = await api.client.post(f"/v1/projects/{project['id']}/restore", headers=ws.headers())

    assert restored.status_code == 200
    assert restored.json()["id"] == project["id"]
    assert again.status_code == 200  # restoring a live project is a no-op
    tasks = await api.collect("/v1/tasks", ws.headers())
    # Only the task that was deleted *by* the project deletion comes back.
    assert [t["id"] for t in tasks] == [kept["id"]]


async def test_restore_conflicts_if_the_name_was_reused(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    project = await api.create_project(ws, name="Phoenix")
    await api.client.delete(f"/v1/projects/{project['id']}", headers=ws.headers())
    await api.create_project(ws, name="Phoenix")

    response = await api.client.post(f"/v1/projects/{project['id']}/restore", headers=ws.headers())

    assert_problem(response, 409, "project_name_taken")


async def test_unknown_project_is_not_found(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    missing = uuid.uuid4()
    headers = ws.headers()

    for request in (
        api.client.get(f"/v1/projects/{missing}", headers=headers),
        api.client.patch(f"/v1/projects/{missing}", json={"name": "x"}, headers=headers),
        api.client.delete(f"/v1/projects/{missing}", headers=headers),
        api.client.post(f"/v1/projects/{missing}/restore", headers=headers),
    ):
        assert_problem(await request, 404, "not_found")
    malformed = await api.client.get("/v1/projects/not-a-uuid", headers=headers)
    assert_problem(malformed, 422, "validation_error")


# ------------------------------------------------------------------- plan quota


async def test_project_quota_of_the_plan_is_enforced(api: ApiDriver) -> None:
    ws = await api.create_workspace()  # free plan: 5 projects
    projects = [await api.create_project(ws) for _ in range(5)]

    over = await api.client.post("/v1/projects", json={"name": "sixth"}, headers=ws.headers())
    body = assert_problem(over, 403, "project_quota_exceeded")
    assert "at most 5 projects" in body["detail"]

    # Deleting frees a slot; restoring needs one.
    await api.client.delete(f"/v1/projects/{projects[0]['id']}", headers=ws.headers())
    await api.create_project(ws)
    restore = await api.client.post(
        f"/v1/projects/{projects[0]['id']}/restore", headers=ws.headers()
    )
    assert_problem(restore, 403, "project_quota_exceeded")

    # An upgrade lifts the limit without any other change.
    await api.set_plan(ws.tenant_id, Plan.PRO)
    await api.create_project(ws)


async def test_concurrent_creates_cannot_exceed_the_quota(api: ApiDriver, db: AdminDb) -> None:
    """Race: twelve creates at once against a limit of five. Count-then-insert
    without the per-tenant advisory lock would let several slip through."""
    ws = await api.create_workspace()

    responses = await asyncio.gather(
        *(
            api.client.post("/v1/projects", json={"name": f"race-{i}"}, headers=ws.headers())
            for i in range(12)
        )
    )

    statuses = sorted(r.status_code for r in responses)
    assert statuses == [201] * 5 + [403] * 7
    stored = await db.scalar(
        "SELECT count(*) FROM projects WHERE tenant_id = :t", t=uuid.UUID(ws.tenant_id)
    )
    assert stored == 5


async def test_quotas_are_per_tenant(api: ApiDriver) -> None:
    full, other = await api.create_workspace(), await api.create_workspace()
    for _ in range(5):
        await api.create_project(full)

    await api.create_project(other)  # unaffected by the other tenant being full
