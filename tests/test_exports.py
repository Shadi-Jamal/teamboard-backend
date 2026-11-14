import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from arq import Retry
from arq.worker import async_check_health
from sqlalchemy.ext.asyncio import AsyncSession

from teamboard.api.deps import AppResources
from teamboard.config import Settings
from teamboard.db.engine import UnsafeDatabaseRoleError
from teamboard.domain import Plan
from teamboard.infra.redis import redis_settings
from teamboard.services import exports as exports_module
from teamboard.worker.jobs import (
    HEARTBEAT_INTERVAL_SECONDS,
    create_worker,
    run_export,
    startup,
)
from tests.conftest import DatabaseUrls
from tests.support import AdminDb, ApiDriver, Workspace, api_key_headers, assert_problem

RunWorker = Callable[[], Awaitable[None]]
Builder = Callable[[AsyncSession, uuid.UUID, uuid.UUID], Awaitable[dict[str, Any]]]


@pytest.fixture
def run_worker(settings: Settings) -> RunWorker:
    """Process everything in the queue (including retries) with a real arq worker,
    then return."""

    async def run() -> None:
        worker = create_worker(settings, burst=True, poll_delay=0.01, handle_signals=False)
        try:
            await worker.async_run()
        finally:
            await worker.close()

    return run


@pytest.fixture
def flaky_builder(monkeypatch: pytest.MonkeyPatch) -> Callable[[int], list[int]]:
    """Make the export fail a given number of times before working again."""

    def install(failures: int) -> list[int]:
        calls: list[int] = []
        original: Builder = exports_module.build_export_document

        async def builder(
            session: AsyncSession, tenant_id: uuid.UUID, job_id: uuid.UUID
        ) -> dict[str, Any]:
            calls.append(len(calls) + 1)
            if len(calls) <= failures:
                raise ConnectionError(f"storage unavailable (attempt {len(calls)})")
            return await original(session, tenant_id, job_id)

        monkeypatch.setattr(exports_module, "build_export_document", builder)
        return calls

    return install


async def _request_export(api: ApiDriver, ws: Workspace) -> dict[str, Any]:
    response = await api.client.post("/v1/exports", headers=ws.headers())
    assert response.status_code == 202, response.text
    body: dict[str, Any] = response.json()
    return body


async def _get_export(api: ApiDriver, ws: Workspace, export_id: str) -> dict[str, Any]:
    response = await api.client.get(f"/v1/exports/{export_id}", headers=ws.headers())
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _job_context(resources: AppResources, settings: Settings, attempt: int) -> dict[str, Any]:
    """What arq passes to a job function."""
    return {"settings": settings, "sessionmaker": resources.sessionmaker, "job_try": attempt}


# ------------------------------------------------------------------ happy path


async def test_export_is_accepted_and_queued(api: ApiDriver) -> None:
    ws = await api.create_workspace()

    export = await _request_export(api, ws)

    assert (export["status"], export["attempts"], export["last_error"]) == ("queued", 0, None)
    assert (export["started_at"], export["finished_at"]) == (None, None)
    assert await _get_export(api, ws, export["id"]) == export
    not_ready = await api.client.get(f"/v1/exports/{export['id']}/download", headers=ws.headers())
    assert_problem(not_ready, 409, "export_not_ready")


async def test_worker_builds_the_export_document(api: ApiDriver, run_worker: RunWorker) -> None:
    ws = await api.create_workspace()
    website = await api.create_project(ws, name="Website", description="relaunch")
    empty = await api.create_project(ws, name="Empty")
    first = await api.create_task(ws, website["id"], title="Design", priority="high")
    second = await api.create_task(ws, website["id"], title="Build", assignee_id=ws.owner.id)
    # Soft-deleted data is not exported.
    deleted_task = await api.create_task(ws, website["id"], title="Dropped")
    await api.client.delete(f"/v1/tasks/{deleted_task['id']}", headers=ws.headers())
    deleted_project = await api.create_project(ws, name="Cancelled")
    await api.client.delete(f"/v1/projects/{deleted_project['id']}", headers=ws.headers())
    export = await _request_export(api, ws)

    await run_worker()

    finished = await _get_export(api, ws, export["id"])
    assert (finished["status"], finished["attempts"], finished["last_error"]) == (
        "succeeded",
        1,
        None,
    )
    assert finished["started_at"] <= finished["finished_at"]

    download = await api.client.get(f"/v1/exports/{export['id']}/download", headers=ws.headers())
    assert download.status_code == 200
    assert download.headers["Content-Disposition"] == (
        f'attachment; filename="teamboard-export-{export["id"]}.json"'
    )
    document = download.json()
    assert (document["export_id"], document["tenant_id"]) == (export["id"], ws.tenant_id)
    assert [p["id"] for p in document["projects"]] == [website["id"], empty["id"]]
    assert document["projects"][0]["tasks"] == [first, second]
    assert document["projects"][1]["tasks"] == []


async def test_export_contains_only_the_requesting_tenants_data(
    api: ApiDriver, run_worker: RunWorker
) -> None:
    """Both tenants export at the same time through the same worker."""
    a, b = await api.create_workspace(), await api.create_workspace()
    project_a, project_b = await api.create_project(a), await api.create_project(b)
    task_a = await api.create_task(a, project_a["id"])
    task_b = await api.create_task(b, project_b["id"])
    export_a, export_b = await _request_export(api, a), await _request_export(api, b)

    await run_worker()

    for ws, export, project, task, foreign in (
        (a, export_a, project_a, task_a, (project_b, task_b)),
        (b, export_b, project_b, task_b, (project_a, task_a)),
    ):
        response = await api.client.get(
            f"/v1/exports/{export['id']}/download", headers=ws.headers()
        )
        document = response.json()
        assert document["tenant_id"] == ws.tenant_id
        assert [p["id"] for p in document["projects"]] == [project["id"]]
        assert [t["id"] for t in document["projects"][0]["tasks"]] == [task["id"]]
        assert foreign[0]["id"] not in response.text
        assert foreign[1]["id"] not in response.text


async def test_export_lifecycle_is_audited(api: ApiDriver, run_worker: RunWorker) -> None:
    ws = await api.create_workspace()
    await api.create_project(ws)
    export = await _request_export(api, ws)
    await run_worker()

    entries = await api.collect("/v1/audit-logs", ws.headers(), entity_id=export["id"])

    assert [(e["action"], e["actor_type"], e["actor_id"]) for e in reversed(entries)] == [
        ("export.requested", "user", ws.owner.id),
        ("export.completed", "system", None),
    ]
    assert entries[0]["changes"] == {"projects": 1}


async def test_api_key_with_export_scope_can_run_the_whole_flow(
    api: ApiDriver, run_worker: RunWorker
) -> None:
    ws = await api.create_workspace()
    project = await api.create_project(ws)
    _, key = await api.create_api_key(ws, ["exports:manage"])
    headers = api_key_headers(key)

    export = (await api.client.post("/v1/exports", headers=headers)).json()
    await run_worker()
    download = await api.client.get(f"/v1/exports/{export['id']}/download", headers=headers)

    assert download.status_code == 200
    assert [p["id"] for p in download.json()["projects"]] == [project["id"]]


async def test_list_and_unknown_export(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    first, second = await _request_export(api, ws), await _request_export(api, ws)

    listed = await api.collect("/v1/exports", ws.headers())

    assert [e["id"] for e in listed] == [second["id"], first["id"]]
    assert "result" not in listed[0]
    for path in (f"/v1/exports/{uuid.uuid4()}", f"/v1/exports/{uuid.uuid4()}/download"):
        assert_problem(await api.client.get(path, headers=ws.headers()), 404, "not_found")


# --------------------------------------------------------------------- retries


async def test_failed_attempts_are_retried_until_the_export_succeeds(
    api: ApiDriver, run_worker: RunWorker, flaky_builder: Callable[[int], list[int]]
) -> None:
    ws = await api.create_workspace()
    project = await api.create_project(ws)
    export = await _request_export(api, ws)
    calls = flaky_builder(2)

    await run_worker()

    finished = await _get_export(api, ws, export["id"])
    assert calls == [1, 2, 3]
    assert (finished["status"], finished["attempts"], finished["last_error"]) == (
        "succeeded",
        3,
        None,
    )
    download = await api.client.get(f"/v1/exports/{export['id']}/download", headers=ws.headers())
    assert [p["id"] for p in download.json()["projects"]] == [project["id"]]


async def test_export_fails_permanently_after_the_last_attempt(
    api: ApiDriver,
    run_worker: RunWorker,
    flaky_builder: Callable[[int], list[int]],
    settings: Settings,
) -> None:
    ws = await api.create_workspace()
    export = await _request_export(api, ws)
    calls = flaky_builder(1_000)

    await run_worker()

    failed = await _get_export(api, ws, export["id"])
    assert len(calls) == settings.export_max_attempts == 3
    assert (failed["status"], failed["attempts"]) == ("failed", 3)
    assert failed["last_error"] == "ConnectionError: storage unavailable (attempt 3)"
    assert failed["finished_at"] is not None
    download = await api.client.get(f"/v1/exports/{export['id']}/download", headers=ws.headers())
    assert_problem(download, 409, "export_not_ready")
    actions = [e["action"] for e in await api.collect("/v1/audit-logs", ws.headers())]
    assert "export.failed" in actions
    assert "export.completed" not in actions


async def test_between_attempts_the_job_is_queued_with_the_error_recorded(
    api: ApiDriver,
    resources: AppResources,
    settings: Settings,
    flaky_builder: Callable[[int], list[int]],
) -> None:
    """One attempt at a time: a failing attempt asks arq for a delayed retry with
    exponential backoff and leaves a truthful status behind - never "running"."""
    ws = await api.create_workspace()
    export = await _request_export(api, ws)
    flaky_builder(2)

    delays = []
    for attempt in (1, 2):
        with pytest.raises(Retry) as retry:
            await run_export(_job_context(resources, settings, attempt), ws.tenant_id, export["id"])
        delays.append(retry.value.defer_score)
        pending = await _get_export(api, ws, export["id"])
        assert (pending["status"], pending["attempts"]) == ("queued", attempt)
        assert pending["last_error"] == f"ConnectionError: storage unavailable (attempt {attempt})"
        assert pending["finished_at"] is None

    outcome = await run_export(_job_context(resources, settings, 3), ws.tenant_id, export["id"])

    base_ms = settings.export_retry_base_seconds * 1000
    assert delays == [base_ms, base_ms * 2]  # exponential backoff
    assert outcome == "succeeded"
    assert (await _get_export(api, ws, export["id"]))["status"] == "succeeded"


async def test_duplicate_delivery_of_a_finished_job_changes_nothing(
    api: ApiDriver, resources: AppResources, settings: Settings, run_worker: RunWorker
) -> None:
    """The queue delivers at least once, so the job must be safe to run again."""
    ws = await api.create_workspace()
    export = await _request_export(api, ws)
    await run_worker()
    finished = await _get_export(api, ws, export["id"])

    outcome = await run_export(_job_context(resources, settings, 1), ws.tenant_id, export["id"])

    assert outcome == "succeeded"
    assert await _get_export(api, ws, export["id"]) == finished


async def test_job_whose_row_never_appears_is_retried_then_dropped(
    api: ApiDriver, resources: AppResources, settings: Settings
) -> None:
    """The API enqueues just before committing. If that commit never happens the
    worker finds no row: it retries (the commit may be in flight) and then gives up."""
    ws = await api.create_workspace()
    ghost = str(uuid.uuid4())

    with pytest.raises(Retry):
        await run_export(_job_context(resources, settings, 1), ws.tenant_id, ghost)
    outcome = await run_export(
        _job_context(resources, settings, settings.export_max_attempts), ws.tenant_id, ghost
    )

    assert outcome == "missing"


# ------------------------------------------------------------------- isolation


async def test_worker_cannot_be_pointed_at_another_tenants_job(
    api: ApiDriver, resources: AppResources, settings: Settings, db: AdminDb
) -> None:
    """A queue message naming tenant A and a job of tenant B does nothing: the
    worker's session is scoped to A, where that job does not exist."""
    a, b = await api.create_workspace(), await api.create_workspace()
    await api.create_project(b, name="confidential")
    export_b = await _request_export(api, b)

    outcome = await run_export(
        _job_context(resources, settings, settings.export_max_attempts), a.tenant_id, export_b["id"]
    )

    assert outcome == "missing"
    row = await db.scalar(
        "SELECT status || ':' || attempts FROM export_jobs WHERE id = :id",
        id=uuid.UUID(export_b["id"]),
    )
    assert row == "queued:0"


async def test_worker_refuses_to_start_with_a_role_that_bypasses_rls(
    settings: Settings, database_urls: DatabaseUrls
) -> None:
    unsafe = settings.model_copy(update={"database_url": database_urls.admin})

    with pytest.raises(UnsafeDatabaseRoleError):
        await startup({"settings": unsafe})


async def test_only_holders_of_the_export_permission_can_access_exports(
    api: ApiDriver,
) -> None:
    ws = await api.create_workspace(plan=Plan.PRO)
    export = await _request_export(api, ws)
    _, key = await api.create_api_key(ws, ["projects:read", "tasks:read"])

    for request in (
        api.client.post("/v1/exports", headers=api_key_headers(key)),
        api.client.get("/v1/exports", headers=api_key_headers(key)),
        api.client.get(f"/v1/exports/{export['id']}", headers=api_key_headers(key)),
        api.client.get(f"/v1/exports/{export['id']}/download", headers=api_key_headers(key)),
    ):
        assert_problem(await request, 403, "permission_denied")


async def test_worker_health_check_reflects_the_heartbeat(
    settings: Settings, resources: AppResources
) -> None:
    """`python -m teamboard.worker --check` is the container health check."""
    connection = redis_settings(settings)
    assert await async_check_health(connection) == 1  # no worker has run yet

    worker = create_worker(settings, burst=True, poll_delay=0.01, handle_signals=False)
    try:
        await worker.async_run()
        assert await async_check_health(connection) == 0
        # The heartbeat expires on its own if the worker dies without cleaning up.
        ttl = await resources.redis.ttl("arq:queue:health-check")
        assert 0 < ttl <= HEARTBEAT_INTERVAL_SECONDS + 1
    finally:
        await worker.close()

    assert await async_check_health(connection) == 1  # a stopped worker is unhealthy
