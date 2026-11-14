import uuid

import pytest

from teamboard.domain import Role
from tests.support import ApiDriver, assert_problem


async def test_create_task_with_defaults(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    project = await api.create_project(ws)

    response = await api.client.post(
        f"/v1/projects/{project['id']}/tasks", json={"title": " Write docs "}, headers=ws.headers()
    )

    assert response.status_code == 201
    task = response.json()
    assert task["title"] == "Write docs"
    assert (task["status"], task["priority"]) == ("todo", "medium")
    assert (task["assignee_id"], task["due_date"]) == (None, None)
    assert task["project_id"] == project["id"]


async def test_create_task_with_all_fields(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    member = await api.add_member(ws, Role.MEMBER)
    project = await api.create_project(ws)

    task = await api.create_task(
        ws,
        project["id"],
        title="Ship it",
        description="carefully",
        status="in_progress",
        priority="urgent",
        assignee_id=member.id,
        due_date="2030-01-31",
    )

    assert task["description"] == "carefully"
    assert (task["status"], task["priority"]) == ("in_progress", "urgent")
    assert (task["assignee_id"], task["due_date"]) == (member.id, "2030-01-31")


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"title": ""},
        {"title": "x" * 201},
        {"title": "ok", "status": "blocked"},
        {"title": "ok", "priority": "critical"},
        {"title": "ok", "due_date": "31.01.2030"},
        {"title": "ok", "assignee_id": "nobody"},
    ],
)
async def test_create_validates_input(api: ApiDriver, payload: dict[str, str]) -> None:
    ws = await api.create_workspace()
    project = await api.create_project(ws)

    response = await api.client.post(
        f"/v1/projects/{project['id']}/tasks", json=payload, headers=ws.headers()
    )

    assert_problem(response, 422, "validation_error")


async def test_task_requires_an_existing_project(api: ApiDriver) -> None:
    ws = await api.create_workspace()

    response = await api.client.post(
        f"/v1/projects/{uuid.uuid4()}/tasks", json={"title": "orphan"}, headers=ws.headers()
    )

    assert_problem(response, 404, "not_found")


async def test_assignee_must_be_a_member_of_the_tenant(api: ApiDriver) -> None:
    """A user of another tenant and a user that does not exist get the same answer."""
    ws = await api.create_workspace()
    project = await api.create_project(ws)
    outsider = await api.register()

    responses = [
        await api.client.post(
            f"/v1/projects/{project['id']}/tasks",
            json={"title": "t", "assignee_id": assignee},
            headers=ws.headers(),
        )
        for assignee in (outsider.id, str(uuid.uuid4()))
    ]

    bodies = [assert_problem(r, 422, "invalid_assignee") for r in responses]
    assert bodies[0]["detail"] == bodies[1]["detail"]


async def test_update_task_fields(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    member = await api.add_member(ws, Role.MEMBER)
    project = await api.create_project(ws)
    task = await api.create_task(ws, project["id"], title="Before")

    response = await api.client.patch(
        f"/v1/tasks/{task['id']}",
        json={
            "title": "After",
            "status": "done",
            "priority": "low",
            "assignee_id": member.id,
            "due_date": "2031-05-01",
        },
        headers=ws.headers(member),
    )

    assert response.status_code == 200
    updated = response.json()
    assert (updated["title"], updated["status"], updated["priority"]) == ("After", "done", "low")
    assert (updated["assignee_id"], updated["due_date"]) == (member.id, "2031-05-01")
    assert updated["updated_at"] > task["updated_at"]


async def test_null_clears_assignee_and_due_date_but_omitted_fields_are_kept(
    api: ApiDriver,
) -> None:
    ws = await api.create_workspace()
    project = await api.create_project(ws)
    task = await api.create_task(
        ws, project["id"], assignee_id=ws.owner.id, due_date="2030-01-01", priority="high"
    )

    untouched = await api.client.patch(
        f"/v1/tasks/{task['id']}", json={"title": "renamed"}, headers=ws.headers()
    )
    cleared = await api.client.patch(
        f"/v1/tasks/{task['id']}",
        json={"assignee_id": None, "due_date": None, "priority": None},
        headers=ws.headers(),
    )

    assert (untouched.json()["assignee_id"], untouched.json()["due_date"]) == (
        ws.owner.id,
        "2030-01-01",
    )
    assert (cleared.json()["assignee_id"], cleared.json()["due_date"]) == (None, None)
    assert cleared.json()["priority"] == "high"  # null on a required field means "no change"


async def test_update_rejects_a_non_member_assignee(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    project = await api.create_project(ws)
    task = await api.create_task(ws, project["id"])
    outsider = await api.register()

    response = await api.client.patch(
        f"/v1/tasks/{task['id']}", json={"assignee_id": outsider.id}, headers=ws.headers()
    )

    assert_problem(response, 422, "invalid_assignee")


async def test_list_filters(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    member = await api.add_member(ws, Role.MEMBER)
    web, app = await api.create_project(ws), await api.create_project(ws)
    t1 = await api.create_task(
        ws, web["id"], title="Design header", status="todo", priority="high", due_date="2030-01-10"
    )
    t2 = await api.create_task(
        ws,
        web["id"],
        title="Fix footer",
        description="the HEADER overlaps",
        status="done",
        priority="low",
        assignee_id=member.id,
        due_date="2030-02-10",
    )
    t3 = await api.create_task(
        ws, app["id"], title="Release build", status="todo", priority="high", assignee_id=member.id
    )

    async def ids(**params: str) -> list[str]:
        return [t["id"] for t in await api.collect("/v1/tasks", ws.headers(), **params)]

    assert await ids() == [t3["id"], t2["id"], t1["id"]]
    assert await ids(project_id=web["id"]) == [t2["id"], t1["id"]]
    assert await ids(status="todo") == [t3["id"], t1["id"]]
    assert await ids(priority="low") == [t2["id"]]
    assert await ids(assignee_id=member.id) == [t3["id"], t2["id"]]
    assert await ids(due_before="2030-01-31") == [t1["id"]]
    assert await ids(due_after="2030-01-31") == [t2["id"]]
    assert await ids(due_after="2030-01-10", due_before="2030-02-10") == [t2["id"], t1["id"]]
    assert await ids(q="header") == [t2["id"], t1["id"]]  # title or description
    assert await ids(status="todo", priority="high", project_id=app["id"]) == [t3["id"]]
    assert await ids(project_id=str(uuid.uuid4())) == []
    invalid = await api.client.get("/v1/tasks", params={"due_before": "soon"}, headers=ws.headers())
    assert_problem(invalid, 422, "validation_error")


async def test_soft_delete_and_restore(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    project = await api.create_project(ws)
    task = await api.create_task(ws, project["id"])
    headers = ws.headers()

    deleted = await api.client.delete(f"/v1/tasks/{task['id']}", headers=headers)

    assert deleted.status_code == 204
    assert_problem(
        await api.client.get(f"/v1/tasks/{task['id']}", headers=headers), 404, "not_found"
    )
    assert_problem(
        await api.client.patch(f"/v1/tasks/{task['id']}", json={"title": "x"}, headers=headers),
        404,
        "not_found",
    )
    assert_problem(
        await api.client.delete(f"/v1/tasks/{task['id']}", headers=headers), 404, "not_found"
    )
    assert await api.collect("/v1/tasks", headers) == []

    restored = await api.client.post(f"/v1/tasks/{task['id']}/restore", headers=headers)

    assert restored.status_code == 200
    assert restored.json()["id"] == task["id"]
    assert [t["id"] for t in await api.collect("/v1/tasks", headers)] == [task["id"]]


async def test_task_cannot_be_restored_into_a_deleted_project(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    project = await api.create_project(ws)
    task = await api.create_task(ws, project["id"])
    await api.client.delete(f"/v1/tasks/{task['id']}", headers=ws.headers())
    await api.client.delete(f"/v1/projects/{project['id']}", headers=ws.headers())

    response = await api.client.post(f"/v1/tasks/{task['id']}/restore", headers=ws.headers())

    assert_problem(response, 409, "project_deleted")


async def test_unknown_task_is_not_found(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    missing = uuid.uuid4()
    headers = ws.headers()

    for request in (
        api.client.get(f"/v1/tasks/{missing}", headers=headers),
        api.client.patch(f"/v1/tasks/{missing}", json={"title": "x"}, headers=headers),
        api.client.delete(f"/v1/tasks/{missing}", headers=headers),
        api.client.post(f"/v1/tasks/{missing}/restore", headers=headers),
    ):
        assert_problem(await request, 404, "not_found")


async def test_no_op_changes_are_not_recorded(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    project = await api.create_project(ws)
    task = await api.create_task(ws, project["id"], title="Same", priority="low")

    unchanged = await api.client.patch(
        f"/v1/tasks/{task['id']}", json={"title": "Same", "priority": "low"}, headers=ws.headers()
    )
    restored_while_live = await api.client.post(
        f"/v1/tasks/{task['id']}/restore", headers=ws.headers()
    )

    assert unchanged.json() == task
    assert restored_while_live.json() == task
    actions = [
        e["action"] for e in await api.collect("/v1/audit-logs", ws.headers(), entity_id=task["id"])
    ]
    assert actions == ["task.created"]
