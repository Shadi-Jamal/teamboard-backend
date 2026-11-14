import asyncio
import uuid

import pytest

from teamboard.domain import Plan, Role
from tests.support import AdminDb, ApiDriver, UserSession, Workspace, assert_problem


async def _team(api: ApiDriver) -> tuple[Workspace, dict[Role, UserSession]]:
    ws = await api.create_workspace()
    people = {Role.OWNER: ws.owner}
    for role in (Role.ADMIN, Role.MEMBER, Role.VIEWER):
        people[role] = await api.add_member(ws, role)
    return ws, people


async def test_list_members_shows_identity_and_role(api: ApiDriver) -> None:
    ws, people = await _team(api)

    members = await api.collect("/v1/members", ws.headers(people[Role.VIEWER]), limit=2)

    assert {m["user_id"]: m["role"] for m in members} == {
        user.id: role.value for role, user in people.items()
    }
    assert {m["email"] for m in members} == {user.email for user in people.values()}
    assert set(members[0]) == {"user_id", "email", "full_name", "role", "joined_at"}


async def test_admin_can_change_roles_below_owner(api: ApiDriver, db: AdminDb) -> None:
    ws, people = await _team(api)
    target = people[Role.MEMBER]

    response = await api.client.patch(
        f"/v1/members/{target.id}", json={"role": "viewer"}, headers=ws.headers(people[Role.ADMIN])
    )

    assert response.status_code == 200
    assert response.json()["role"] == "viewer"
    assert response.json()["email"] == target.email
    # The demotion is effective on the very next request (roles are not cached in the JWT).
    denied = await api.client.post("/v1/projects", json={"name": "x"}, headers=ws.headers(target))
    assert_problem(denied, 403, "permission_denied")
    audit = await db.scalar(
        "SELECT changes FROM audit_logs WHERE tenant_id = :t AND action = 'member.role_changed'",
        t=uuid.UUID(ws.tenant_id),
    )
    assert audit == {"role": {"old": "member", "new": "viewer"}}


async def test_admin_cannot_grant_owner_or_modify_an_owner(api: ApiDriver) -> None:
    ws, people = await _team(api)
    as_admin = ws.headers(people[Role.ADMIN])

    promote = await api.client.patch(
        f"/v1/members/{people[Role.MEMBER].id}", json={"role": "owner"}, headers=as_admin
    )
    demote_owner = await api.client.patch(
        f"/v1/members/{ws.owner.id}", json={"role": "member"}, headers=as_admin
    )
    remove_owner = await api.client.delete(f"/v1/members/{ws.owner.id}", headers=as_admin)

    for response in (promote, demote_owner, remove_owner):
        assert_problem(response, 403, "insufficient_role")


@pytest.mark.parametrize("role", [Role.MEMBER, Role.VIEWER])
async def test_non_admins_cannot_manage_members(api: ApiDriver, role: Role) -> None:
    ws, people = await _team(api)
    headers = ws.headers(people[role])

    change = await api.client.patch(
        f"/v1/members/{people[Role.ADMIN].id}", json={"role": "viewer"}, headers=headers
    )
    remove = await api.client.delete(f"/v1/members/{people[Role.ADMIN].id}", headers=headers)

    assert_problem(change, 403, "permission_denied")
    assert_problem(remove, 403, "permission_denied")


async def test_owner_can_transfer_ownership(api: ApiDriver) -> None:
    ws, people = await _team(api)
    successor = people[Role.ADMIN]

    promote = await api.client.patch(
        f"/v1/members/{successor.id}", json={"role": "owner"}, headers=ws.headers()
    )
    step_down = await api.client.patch(
        f"/v1/members/{ws.owner.id}", json={"role": "member"}, headers=ws.headers()
    )

    assert promote.status_code == 200
    assert step_down.status_code == 200
    assert step_down.json()["role"] == "member"


async def test_the_last_owner_cannot_be_demoted_or_leave(api: ApiDriver) -> None:
    ws, _ = await _team(api)

    demote = await api.client.patch(
        f"/v1/members/{ws.owner.id}", json={"role": "admin"}, headers=ws.headers()
    )
    leave = await api.client.delete(f"/v1/members/{ws.owner.id}", headers=ws.headers())

    assert_problem(demote, 409, "last_owner")
    assert_problem(leave, 409, "last_owner")


async def test_two_owners_demoting_each_other_concurrently_leave_one_owner(
    api: ApiDriver, db: AdminDb
) -> None:
    """Race on the "at least one owner" invariant: without serialization both
    requests would see two owners and both demotions would succeed."""
    ws = await api.create_workspace()
    second = await api.add_member(ws, Role.OWNER)

    responses = await asyncio.gather(
        api.client.patch(
            f"/v1/members/{second.id}", json={"role": "admin"}, headers=ws.headers(ws.owner)
        ),
        api.client.patch(
            f"/v1/members/{ws.owner.id}", json={"role": "admin"}, headers=ws.headers(second)
        ),
    )

    assert sorted(r.status_code for r in responses) == [200, 403]
    owners = await db.scalar(
        "SELECT count(*) FROM memberships WHERE tenant_id = :t AND role = 'owner'",
        t=uuid.UUID(ws.tenant_id),
    )
    assert owners == 1


async def test_any_member_can_leave(api: ApiDriver) -> None:
    ws, people = await _team(api)
    viewer = people[Role.VIEWER]

    response = await api.client.delete(f"/v1/members/{viewer.id}", headers=ws.headers(viewer))

    assert response.status_code == 204
    after = await api.client.get("/v1/projects", headers=ws.headers(viewer))
    assert_problem(after, 403, "tenant_access_denied")


async def test_removing_a_member_revokes_access_and_unassigns_their_tasks(
    api: ApiDriver, db: AdminDb
) -> None:
    ws, people = await _team(api)
    leaver = people[Role.MEMBER]
    project = await api.create_project(ws)
    task = await api.create_task(ws, project["id"], assignee_id=leaver.id)

    response = await api.client.delete(
        f"/v1/members/{leaver.id}", headers=ws.headers(people[Role.ADMIN])
    )

    assert response.status_code == 204
    assert_problem(
        await api.client.get("/v1/projects", headers=ws.headers(leaver)),
        403,
        "tenant_access_denied",
    )
    refreshed = await api.client.get(f"/v1/tasks/{task['id']}", headers=ws.headers())
    assert refreshed.json()["assignee_id"] is None
    actions = await db.column(
        "SELECT action FROM audit_logs WHERE tenant_id = :t AND entity_id = :u ORDER BY created_at",
        t=uuid.UUID(ws.tenant_id),
        u=uuid.UUID(leaver.id),
    )
    assert actions == ["member.joined", "member.removed"]
    # The account itself still exists; only the membership is gone.
    assert (await api.client.get("/v1/auth/me", headers=leaver.auth)).status_code == 200


async def test_unknown_member_is_not_found(api: ApiDriver) -> None:
    ws, _ = await _team(api)

    change = await api.client.patch(
        f"/v1/members/{uuid.uuid4()}", json={"role": "viewer"}, headers=ws.headers()
    )
    remove = await api.client.delete(f"/v1/members/{uuid.uuid4()}", headers=ws.headers())

    assert_problem(change, 404, "not_found")
    assert_problem(remove, 404, "not_found")


async def test_invalid_role_is_rejected(api: ApiDriver) -> None:
    ws, people = await _team(api)

    response = await api.client.patch(
        f"/v1/members/{people[Role.VIEWER].id}", json={"role": "superuser"}, headers=ws.headers()
    )

    assert_problem(response, 422, "validation_error")


# ------------------------------------------------------------------ RBAC matrix

# The expected permissions, written out independently of the implementation's
# role table: (description, method, path template, JSON body, roles allowed).
ALL = {Role.OWNER, Role.ADMIN, Role.MEMBER, Role.VIEWER}
WRITERS = {Role.OWNER, Role.ADMIN, Role.MEMBER}
ADMINS = {Role.OWNER, Role.ADMIN}

RBAC_MATRIX: list[tuple[str, str, str, dict[str, object] | None, set[Role]]] = [
    ("read tenant", "GET", "/v1/tenants/current", None, ALL),
    ("read usage", "GET", "/v1/tenants/current/usage", None, ALL),
    ("rename tenant", "PATCH", "/v1/tenants/current", {"name": "New"}, ADMINS),
    ("list members", "GET", "/v1/members", None, ALL),
    ("change role", "PATCH", "/v1/members/{viewer_id}", {"role": "viewer"}, ADMINS),
    ("create invitation", "POST", "/v1/invitations", {"email": "new@example.com"}, ADMINS),
    ("list invitations", "GET", "/v1/invitations", None, ADMINS),
    ("revoke invitation", "DELETE", "/v1/invitations/{invitation_id}", None, ADMINS),
    ("create api key", "POST", "/v1/api-keys", {"name": "k", "scopes": ["tasks:read"]}, ADMINS),
    ("list api keys", "GET", "/v1/api-keys", None, ADMINS),
    ("revoke api key", "DELETE", "/v1/api-keys/{api_key_id}", None, ADMINS),
    ("list projects", "GET", "/v1/projects", None, ALL),
    ("get project", "GET", "/v1/projects/{project_id}", None, ALL),
    ("create project", "POST", "/v1/projects", {"name": "by-{role}"}, WRITERS),
    ("update project", "PATCH", "/v1/projects/{project_id}", {"description": "d"}, WRITERS),
    ("delete project", "DELETE", "/v1/projects/{spare_project_id}", None, ADMINS),
    ("restore project", "POST", "/v1/projects/{spare_project_id}/restore", None, ADMINS),
    ("list tasks", "GET", "/v1/tasks", None, ALL),
    ("get task", "GET", "/v1/tasks/{task_id}", None, ALL),
    ("create task", "POST", "/v1/projects/{project_id}/tasks", {"title": "t"}, WRITERS),
    ("update task", "PATCH", "/v1/tasks/{task_id}", {"priority": "high"}, WRITERS),
    ("delete task", "DELETE", "/v1/tasks/{task_id}", None, WRITERS),
    ("restore task", "POST", "/v1/tasks/{task_id}/restore", None, WRITERS),
    ("read audit log", "GET", "/v1/audit-logs", None, ADMINS),
    ("create export", "POST", "/v1/exports", None, ADMINS),
    ("list exports", "GET", "/v1/exports", None, ADMINS),
    ("get export", "GET", "/v1/exports/{export_id}", None, ADMINS),
    ("download export", "GET", "/v1/exports/{export_id}/download", None, ADMINS),
]


@pytest.mark.parametrize("role", [Role.OWNER, Role.ADMIN, Role.MEMBER, Role.VIEWER])
async def test_role_permission_matrix(api: ApiDriver, role: Role) -> None:
    """Every tenant-scoped operation, attempted by one role. Allowed means the
    request gets past authorization (2xx, or a 409 state conflict); denied means
    exactly 403 permission_denied."""
    ws = await api.create_workspace(plan=Plan.PRO)
    people = {Role.OWNER: ws.owner}
    for other in (Role.ADMIN, Role.MEMBER, Role.VIEWER):
        people[other] = await api.add_member(ws, other)
    spare_viewer = await api.add_member(ws, Role.VIEWER)
    project = await api.create_project(ws)
    spare_project = await api.create_project(ws)
    task = await api.create_task(ws, project["id"])
    invitation = await api.invite(ws, "pending@example.com", Role.VIEWER)
    api_key, _ = await api.create_api_key(ws, ["tasks:read"])
    export = (await api.client.post("/v1/exports", headers=ws.headers())).json()
    ids = {
        "viewer_id": spare_viewer.id,
        "invitation_id": invitation["id"],
        "api_key_id": api_key["id"],
        "project_id": project["id"],
        "spare_project_id": spare_project["id"],
        "task_id": task["id"],
        "export_id": export["id"],
        "role": role.value,
    }
    headers = ws.headers(people[role])

    outcomes: dict[str, bool] = {}
    for description, method, template, body, _allowed in RBAC_MATRIX:
        payload = {k: v.format(**ids) if isinstance(v, str) else v for k, v in (body or {}).items()}
        response = await api.client.request(
            method,
            template.format(**ids),
            json=payload if body is not None else None,
            headers=headers,
        )
        if response.status_code == 403:
            assert response.json()["code"] == "permission_denied", description
            outcomes[description] = False
        else:
            assert response.status_code in (200, 201, 202, 204, 409), (
                f"{description}: {response.status_code} {response.text}"
            )
            outcomes[description] = True

    expected = {description: role in allowed for description, *_, allowed in RBAC_MATRIX}
    assert outcomes == expected
