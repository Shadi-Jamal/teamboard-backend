#!/usr/bin/env python3
"""End-to-end smoke test against a running TeamBoard stack.

    docker compose up --build --detach --wait
    python scripts/smoke_test.py [http://localhost:8000]

Walks through the main user journey over real HTTP - including the background
worker and a cross-tenant check - and exits non-zero on the first failure. It
uses only the standard library so it can run anywhere (CI, a laptop, a bastion).
"""

import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

BASE_URL = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000").rstrip("/")
PASSWORD = "smoke-test-password-123"  # noqa: S105 - throwaway account in a test stack


def call(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(  # noqa: S310 - URL is the operator-supplied base
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        raw, status = error.read(), error.code
    try:
        return status, json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return status, raw.decode(errors="replace")


def check(description: str, condition: bool, detail: object = "") -> None:
    print(f"  {'ok  ' if condition else 'FAIL'}  {description}", flush=True)  # noqa: T201
    if not condition:
        print(f"        {detail}", flush=True)  # noqa: T201
        sys.exit(1)


def sign_up(label: str) -> tuple[dict[str, str], str]:
    """Register a user, log in and create a tenant. Returns (headers, tenant_id)."""
    suffix = uuid.uuid4().hex[:10]
    email = f"smoke-{label}-{suffix}@example.com"
    status, _ = call(
        "POST",
        "/v1/auth/register",
        {"email": email, "password": PASSWORD, "full_name": f"Smoke {label}"},
    )
    check(f"[{label}] register", status == 201, status)
    status, tokens = call("POST", "/v1/auth/login", {"email": email, "password": PASSWORD})
    check(f"[{label}] login", status == 200, tokens)
    auth = {"Authorization": f"Bearer {tokens['access_token']}"}
    status, tenant = call(
        "POST", "/v1/tenants", {"name": f"Smoke {label}", "slug": f"smoke-{label}-{suffix}"}, auth
    )
    check(f"[{label}] create tenant", status == 201, tenant)
    return {**auth, "X-Tenant-ID": tenant["id"]}, tenant["id"]


def main() -> None:
    print(f"Smoke test against {BASE_URL}", flush=True)  # noqa: T201

    status, body = call("GET", "/readyz")
    check("readiness: database and redis reachable", status == 200, body)
    status, spec = call("GET", "/openapi.json")
    check("OpenAPI document is served", status == 200 and "paths" in spec, status)
    status, page = call("GET", "/docs")
    check("interactive docs are served", status == 200 and "swagger" in str(page).lower(), status)

    alice, _ = sign_up("alice")
    status, project = call("POST", "/v1/projects", {"name": "Smoke project"}, alice)
    check("create project", status == 201, project)
    status, task = call(
        "POST",
        f"/v1/projects/{project['id']}/tasks",
        {"title": "Smoke task", "priority": "high"},
        {**alice, "Idempotency-Key": "smoke-task-1"},
    )
    check("create task", status == 201, task)
    status, replay = call(
        "POST",
        f"/v1/projects/{project['id']}/tasks",
        {"title": "Smoke task", "priority": "high"},
        {**alice, "Idempotency-Key": "smoke-task-1"},
    )
    check("idempotent retry returns the same task", status == 201 and replay["id"] == task["id"])

    status, export = call("POST", "/v1/exports", headers=alice)
    check("request export (202 Accepted)", status == 202, export)
    deadline = time.monotonic() + 30
    while export["status"] in ("queued", "running") and time.monotonic() < deadline:
        time.sleep(0.5)
        _, export = call("GET", f"/v1/exports/{export['id']}", headers=alice)
    check("background worker finished the export", export["status"] == "succeeded", export)
    status, document = call("GET", f"/v1/exports/{export['id']}/download", headers=alice)
    exported_tasks = [t["id"] for p in document["projects"] for t in p["tasks"]]
    check("export contains the task", status == 200 and exported_tasks == [task["id"]], document)

    bob, _ = sign_up("bob")
    status, problem = call("GET", f"/v1/projects/{project['id']}", headers=bob)
    check(
        "another tenant gets 404 for alice's project",
        status == 404 and problem["code"] == "not_found",
        problem,
    )
    status, listing = call("GET", "/v1/projects", headers=bob)
    check("another tenant's project list is empty", status == 200 and listing["items"] == [])
    status, problem = call(
        "GET", "/v1/projects", headers={**bob, "X-Tenant-ID": alice["X-Tenant-ID"]}
    )
    check("foreign tenant header is rejected", status == 403, problem)

    status, audit = call("GET", "/v1/audit-logs", headers=alice)
    actions = {entry["action"] for entry in audit["items"]}
    check(
        "audit log recorded the changes",
        {"project.created", "task.created", "export.completed"} <= actions,
        actions,
    )
    print("All smoke checks passed.", flush=True)  # noqa: T201


if __name__ == "__main__":
    main()
