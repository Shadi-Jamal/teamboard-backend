import json
import logging
import re
import sys
import uuid

import httpx
import pytest

from teamboard.context import actor_var, request_id_var, tenant_id_var
from teamboard.logging_config import JsonFormatter, configure_logging
from teamboard.services.projects import ProjectService
from tests.support import ApiDriver, api_key_headers, assert_problem


class JsonCapture(logging.Handler):
    """Collects log lines exactly as the JSON formatter would write them."""

    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(JsonFormatter())
        self.lines: list[dict[str, object]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(json.loads(self.format(record)))


@pytest.fixture
def access_log() -> object:
    logger = logging.getLogger("teamboard.access")
    capture = JsonCapture()
    previous_level = logger.level
    logger.addHandler(capture)
    logger.setLevel(logging.INFO)
    yield capture.lines
    logger.removeHandler(capture)
    logger.setLevel(previous_level)


# -------------------------------------------------------------- problem format


async def test_unknown_route_is_a_problem_document(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/does-not-exist")

    body = assert_problem(response, 404, "not_found")
    assert body["instance"] == "/v1/does-not-exist"
    assert set(body) == {"type", "title", "status", "detail", "instance", "code", "request_id"}


async def test_wrong_method_is_a_problem_document(client: httpx.AsyncClient) -> None:
    response = await client.put("/v1/auth/login")

    assert_problem(response, 405, "method_not_allowed")
    assert response.headers["Allow"] == "POST"


async def test_validation_errors_list_every_invalid_field(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/auth/register", json={"email": "nope", "password": "short"})

    body = assert_problem(response, 422, "validation_error")
    assert {error["field"] for error in body["errors"]} == {
        "body.email",
        "body.password",
        "body.full_name",
    }
    assert all(set(error) == {"field", "message", "type"} for error in body["errors"])


async def test_malformed_json_is_a_validation_problem(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/auth/login", content=b"{not json", headers={"Content-Type": "application/json"}
    )

    assert_problem(response, 422, "validation_error")


async def test_unexpected_errors_do_not_leak_internals(
    api: ApiDriver, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ws = await api.create_workspace()

    async def explode(self: ProjectService, project_id: uuid.UUID) -> None:
        raise RuntimeError("connection string postgres://user:hunter2@db/prod")

    monkeypatch.setattr(ProjectService, "get", explode)

    with caplog.at_level(logging.ERROR, logger="teamboard.access"):
        response = await api.client.get(f"/v1/projects/{uuid.uuid4()}", headers=ws.headers())

    body = assert_problem(response, 500, "internal_error")
    assert body["detail"] == "An unexpected error occurred."
    assert "hunter2" not in response.text
    assert "RuntimeError" not in response.text
    # The details are in the server log, findable through the request ID.
    record = next(r for r in caplog.records if r.message == "unhandled exception")
    assert record.exc_info is not None
    assert "hunter2" in str(record.exc_info[1])
    assert response.headers["X-Request-ID"] == body["request_id"]


async def test_the_application_keeps_working_after_an_unexpected_error(
    api: ApiDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failed request's transaction is rolled back and its connection returned."""
    ws = await api.create_workspace()
    project = await api.create_project(ws)
    original = ProjectService.get

    async def explode_once(self: ProjectService, project_id: uuid.UUID) -> object:
        monkeypatch.setattr(ProjectService, "get", original)
        raise RuntimeError("boom")

    monkeypatch.setattr(ProjectService, "get", explode_once)

    failed = await api.client.get(f"/v1/projects/{project['id']}", headers=ws.headers())
    recovered = await api.client.get(f"/v1/projects/{project['id']}", headers=ws.headers())

    assert failed.status_code == 500
    assert recovered.status_code == 200


# ------------------------------------------------------------------ request IDs


async def test_request_id_is_generated(client: httpx.AsyncClient) -> None:
    first, second = await client.get("/healthz"), await client.get("/healthz")

    assert re.fullmatch(r"[0-9a-f]{32}", first.headers["X-Request-ID"])
    assert first.headers["X-Request-ID"] != second.headers["X-Request-ID"]


async def test_client_request_id_is_propagated(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/nope", headers={"X-Request-ID": "client-trace_1.A"})

    assert response.headers["X-Request-ID"] == "client-trace_1.A"
    assert response.json()["request_id"] == "client-trace_1.A"


@pytest.mark.parametrize("value", ["x" * 65, "has space", 'quote"injection', "new\tline", ""])
async def test_unsafe_client_request_ids_are_replaced(
    client: httpx.AsyncClient, value: str
) -> None:
    """The ID is written to logs, so only a conservative alphabet is accepted."""
    response = await client.get("/healthz", headers={"X-Request-ID": value})

    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["X-Request-ID"])


async def test_responses_carry_defensive_headers(api: ApiDriver) -> None:
    ws = await api.create_workspace()

    for response in (
        await api.client.get("/v1/projects", headers=ws.headers()),
        await api.client.get("/v1/projects"),  # error responses too
    ):
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Cache-Control"] == "no-store"


# ---------------------------------------------------------------------- logging


def test_json_formatter_emits_one_object_with_context() -> None:
    tokens = (request_id_var.set("req-1"), tenant_id_var.set("tenant-1"), actor_var.set("user:1"))
    try:
        raise ValueError("bad input")
    except ValueError:
        record = logging.LogRecord(
            "teamboard.test", logging.ERROR, __file__, 1, "failed %s", ("thing",), sys.exc_info()
        )
    record.attempt = 3
    try:
        line = JsonFormatter().format(record)
    finally:
        request_id_var.reset(tokens[0])
        tenant_id_var.reset(tokens[1])
        actor_var.reset(tokens[2])

    payload = json.loads(line)
    assert "\n" not in line
    assert payload["message"] == "failed thing"
    assert (payload["level"], payload["logger"]) == ("ERROR", "teamboard.test")
    assert (payload["request_id"], payload["tenant_id"], payload["actor"]) == (
        "req-1",
        "tenant-1",
        "user:1",
    )
    assert payload["attempt"] == 3
    assert payload["timestamp"].endswith("+00:00")
    assert "ValueError: bad input" in payload["exception"]


async def test_access_log_carries_request_tenant_and_actor(
    api: ApiDriver, access_log: list[dict[str, object]]
) -> None:
    ws = await api.create_workspace()
    key, secret = await api.create_api_key(ws, ["projects:read"])
    access_log.clear()

    by_user = await api.client.get(
        "/v1/projects", headers={**ws.headers(), "X-Request-ID": "trace-user"}
    )
    await api.client.get("/v1/projects", headers=api_key_headers(secret))
    await api.client.get("/healthz")

    user_line, key_line, anonymous_line = access_log
    assert user_line["message"] == "request completed"
    assert (user_line["method"], user_line["path"], user_line["status"]) == (
        "GET",
        "/v1/projects",
        200,
    )
    assert user_line["request_id"] == "trace-user" == by_user.headers["X-Request-ID"]
    assert user_line["tenant_id"] == ws.tenant_id
    assert user_line["actor"] == f"user:{ws.owner.id}"
    assert isinstance(user_line["duration_ms"], float)
    assert (key_line["tenant_id"], key_line["actor"]) == (ws.tenant_id, f"api_key:{key['id']}")
    assert (anonymous_line["tenant_id"], anonymous_line["actor"]) == (None, None)


async def test_access_log_never_contains_credentials(
    api: ApiDriver, access_log: list[dict[str, object]]
) -> None:
    ws = await api.create_workspace()
    _, secret = await api.create_api_key(ws, ["projects:read"])
    access_log.clear()

    await api.client.get("/v1/projects", headers=api_key_headers(secret))
    await api.client.get("/v1/projects", headers=ws.headers())

    dump = json.dumps(access_log)
    assert secret not in dump
    assert ws.owner.access_token not in dump


async def test_log_context_does_not_bleed_into_the_next_request(api: ApiDriver) -> None:
    ws = await api.create_workspace()

    await api.client.get("/v1/projects", headers=ws.headers())

    assert (request_id_var.get(), tenant_id_var.get(), actor_var.get()) == (None, None, None)


def test_configure_logging_routes_everything_through_json() -> None:
    root = logging.getLogger()
    saved = (root.handlers[:], root.level, logging.getLogger("uvicorn.access").disabled)
    try:
        configure_logging("debug")

        assert root.level == logging.DEBUG
        assert [type(h.formatter) for h in root.handlers] == [JsonFormatter]
        assert logging.getLogger("uvicorn.error").handlers == []
        assert logging.getLogger("uvicorn.access").disabled is True
    finally:
        root.handlers, root.level = saved[0], saved[1]
        logging.getLogger("uvicorn.access").disabled = saved[2]
