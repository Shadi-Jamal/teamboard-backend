"""Test helpers: a small driver that sets up users, tenants and data through the
public HTTP API, exactly as a client would."""

import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from teamboard.domain import Plan, Role

PASSWORD = "correct-horse-battery-staple"


def unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@dataclass
class UserSession:
    id: str
    email: str
    password: str
    access_token: str
    refresh_token: str

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}


@dataclass
class Workspace:
    """A tenant together with the credentials of its members."""

    tenant_id: str
    slug: str
    owner: UserSession
    members: dict[Role, UserSession] = field(default_factory=dict)

    def headers(self, user: UserSession | None = None) -> dict[str, str]:
        return {**(user or self.owner).auth, "X-Tenant-ID": self.tenant_id}


def api_key_headers(key: str) -> dict[str, str]:
    return {"X-API-Key": key}


class ApiDriver:
    def __init__(self, client: httpx.AsyncClient, admin_engine: AsyncEngine) -> None:
        self.client = client
        self._admin_engine = admin_engine

    # ------------------------------------------------------------------ accounts

    async def register(
        self, *, email: str | None = None, full_name: str = "Test User"
    ) -> UserSession:
        email = email or f"{unique('user')}@example.com"
        response = await self.client.post(
            "/v1/auth/register",
            json={"email": email, "password": PASSWORD, "full_name": full_name},
        )
        assert response.status_code == 201, response.text
        return await self.login(email, user_id=response.json()["id"])

    async def login(self, email: str, *, user_id: str = "") -> UserSession:
        response = await self.client.post(
            "/v1/auth/login", json={"email": email, "password": PASSWORD}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        return UserSession(
            id=user_id,
            email=email,
            password=PASSWORD,
            access_token=body["access_token"],
            refresh_token=body["refresh_token"],
        )

    # ------------------------------------------------------------------- tenants

    async def create_workspace(
        self, *, owner: UserSession | None = None, plan: Plan | None = None
    ) -> Workspace:
        owner = owner or await self.register()
        slug = unique("ws")
        response = await self.client.post(
            "/v1/tenants", json={"name": f"Workspace {slug}", "slug": slug}, headers=owner.auth
        )
        assert response.status_code == 201, response.text
        workspace = Workspace(tenant_id=response.json()["id"], slug=slug, owner=owner)
        if plan is not None:
            await self.set_plan(workspace.tenant_id, plan)
        return workspace

    async def set_plan(self, tenant_id: str, plan: Plan) -> None:
        """Plans are changed by billing/back-office, not through the public API."""
        async with self._admin_engine.begin() as connection:
            await connection.execute(
                text("UPDATE tenants SET plan = :plan WHERE id = :id"),
                {"plan": plan.value, "id": uuid.UUID(tenant_id)},
            )

    async def invite(self, workspace: Workspace, email: str, role: Role) -> dict[str, Any]:
        response = await self.client.post(
            "/v1/invitations",
            json={"email": email, "role": role.value},
            headers=workspace.headers(),
        )
        assert response.status_code == 201, response.text
        body: dict[str, Any] = response.json()
        return body

    async def add_member(self, workspace: Workspace, role: Role) -> UserSession:
        """Register a new user and bring them into the workspace with ``role``."""
        user = await self.register()
        # Ownership cannot be granted by invitation: invite as admin, then promote.
        invited_as = Role.ADMIN if role is Role.OWNER else role
        invitation = await self.invite(workspace, user.email, invited_as)
        response = await self.client.post(
            "/v1/invitations/accept", json={"token": invitation["token"]}, headers=user.auth
        )
        assert response.status_code == 200, response.text
        if role is Role.OWNER:
            response = await self.client.patch(
                f"/v1/members/{user.id}", json={"role": "owner"}, headers=workspace.headers()
            )
            assert response.status_code == 200, response.text
        workspace.members[role] = user
        return user

    async def create_api_key(
        self, workspace: Workspace, scopes: list[str]
    ) -> tuple[dict[str, Any], str]:
        response = await self.client.post(
            "/v1/api-keys",
            json={"name": unique("key"), "scopes": scopes},
            headers=workspace.headers(),
        )
        assert response.status_code == 201, response.text
        body: dict[str, Any] = response.json()
        return body, body["key"]

    # ---------------------------------------------------------------------- work

    async def create_project(
        self, workspace: Workspace, *, name: str | None = None, **fields: Any
    ) -> dict[str, Any]:
        response = await self.client.post(
            "/v1/projects",
            json={"name": name or unique("project"), **fields},
            headers=workspace.headers(),
        )
        assert response.status_code == 201, response.text
        body: dict[str, Any] = response.json()
        return body

    async def create_task(
        self, workspace: Workspace, project_id: str, *, title: str | None = None, **fields: Any
    ) -> dict[str, Any]:
        response = await self.client.post(
            f"/v1/projects/{project_id}/tasks",
            json={"title": title or unique("task"), **fields},
            headers=workspace.headers(),
        )
        assert response.status_code == 201, response.text
        body: dict[str, Any] = response.json()
        return body

    async def collect(
        self, path: str, headers: dict[str, str], **params: Any
    ) -> list[dict[str, Any]]:
        """Follow ``next_cursor`` until the list is exhausted."""
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            query = {**params, **({"cursor": cursor} if cursor else {})}
            response = await self.client.get(path, params=query, headers=headers)
            assert response.status_code == 200, response.text
            body = response.json()
            items.extend(body["items"])
            cursor = body["next_cursor"]
            if cursor is None:
                return items


@dataclass
class TenantWorld:
    """A fully populated tenant: at least one row in every tenant-owned table."""

    ws: Workspace
    admin: UserSession
    member: UserSession
    viewer: UserSession
    project: dict[str, Any]
    task: dict[str, Any]
    deleted_project: dict[str, Any]
    deleted_task: dict[str, Any]
    invitation: dict[str, Any]  # pending; includes the one-time token
    api_key: dict[str, Any]
    api_key_secret: str
    export: dict[str, Any]
    export_idempotency_key: str

    @property
    def tenant_id(self) -> str:
        return self.ws.tenant_id

    @property
    def all_ids(self) -> set[str]:
        """Every identifier that must never show up in another tenant's responses."""
        return {
            self.tenant_id,
            self.ws.owner.id,
            self.admin.id,
            self.member.id,
            self.viewer.id,
            self.project["id"],
            self.task["id"],
            self.deleted_project["id"],
            self.deleted_task["id"],
            self.invitation["id"],
            self.api_key["id"],
            self.export["id"],
        }


ALL_API_KEY_SCOPES = [
    "tenant:read",
    "members:read",
    "projects:read",
    "projects:write",
    "projects:delete",
    "tasks:read",
    "tasks:write",
    "audit:read",
    "exports:manage",
]


async def build_tenant_world(api: ApiDriver) -> TenantWorld:
    ws = await api.create_workspace(plan=Plan.PRO)
    admin = await api.add_member(ws, Role.ADMIN)
    member = await api.add_member(ws, Role.MEMBER)
    viewer = await api.add_member(ws, Role.VIEWER)
    headers = ws.headers()

    project = await api.create_project(ws, description="visible project")
    task = await api.create_task(ws, project["id"], assignee_id=member.id)

    deleted_project = await api.create_project(ws)
    await api.create_task(ws, deleted_project["id"])
    response = await api.client.delete(f"/v1/projects/{deleted_project['id']}", headers=headers)
    assert response.status_code == 204, response.text

    deleted_task = await api.create_task(ws, project["id"])
    response = await api.client.delete(f"/v1/tasks/{deleted_task['id']}", headers=headers)
    assert response.status_code == 204, response.text

    invitation = await api.invite(ws, f"{unique('pending')}@example.com", Role.MEMBER)
    api_key, secret = await api.create_api_key(ws, ALL_API_KEY_SCOPES)

    export_key = unique("idem")
    response = await api.client.post(
        "/v1/exports", headers={**headers, "Idempotency-Key": export_key}
    )
    assert response.status_code == 202, response.text

    return TenantWorld(
        ws=ws,
        admin=admin,
        member=member,
        viewer=viewer,
        project=project,
        task=task,
        deleted_project=deleted_project,
        deleted_task=deleted_task,
        invitation=invitation,
        api_key=api_key,
        api_key_secret=secret,
        export=response.json(),
        export_idempotency_key=export_key,
    )


def assert_problem(response: httpx.Response, status: int, code: str) -> dict[str, Any]:
    """Assert an RFC 7807 error response and return its body."""
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/problem+json"
    body: dict[str, Any] = response.json()
    assert body["status"] == status
    assert body["code"] == code
    assert body["type"] == f"urn:teamboard:problem:{code}"
    assert body["title"]
    assert body["request_id"] == response.headers["X-Request-ID"]
    return body


class AdminDb:
    """Superuser SQL access for assertions about what is really stored, and for
    setting up states the public API cannot produce (expiry, plans, ...)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def scalar(self, sql: str, **params: Any) -> Any:
        async with self._engine.connect() as connection:
            return (await connection.execute(text(sql), params)).scalar_one()

    async def column(self, sql: str, **params: Any) -> list[Any]:
        async with self._engine.connect() as connection:
            return list((await connection.execute(text(sql), params)).scalars().all())

    async def execute(self, sql: str, **params: Any) -> int:
        async with self._engine.begin() as connection:
            result = await connection.execute(text(sql), params)
            return int(result.rowcount)
