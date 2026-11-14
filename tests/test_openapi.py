from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from teamboard import __version__
from teamboard.config import Settings
from teamboard.main import create_app

GLOBAL_PREFIXES = ("/healthz", "/readyz", "/v1/auth/")
USER_ONLY = {("post", "/v1/tenants"), ("get", "/v1/tenants"), ("post", "/v1/invitations/accept")}
IDEMPOTENT = {
    ("post", "/v1/projects"),
    ("post", "/v1/projects/{project_id}/tasks"),
    ("post", "/v1/exports"),
}


@pytest.fixture(scope="module")
def schema(app: FastAPI) -> dict[str, Any]:
    return app.openapi()


def _operations(schema: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (method, path, operation)
        for path, item in schema["paths"].items()
        for method, operation in item.items()
    ]


def _is_tenant_scoped(method: str, path: str) -> bool:
    return not path.startswith(GLOBAL_PREFIXES) and (method, path) not in USER_ONLY


async def test_documentation_endpoints_are_served(client: httpx.AsyncClient) -> None:
    spec = await client.get("/openapi.json")
    swagger = await client.get("/docs")
    redoc = await client.get("/redoc")

    assert spec.status_code == 200
    assert spec.json()["openapi"].startswith("3.")
    assert spec.json()["info"] == {
        "title": "TeamBoard API",
        "description": spec.json()["info"]["description"],
        "version": __version__,
    }
    assert swagger.status_code == 200
    assert "swagger-ui" in swagger.text
    assert redoc.status_code == 200


async def test_documentation_can_be_switched_off(settings: Settings) -> None:
    application = create_app(settings.model_copy(update={"docs_enabled": False}))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://testserver"
    ) as http:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert (await http.get(path)).status_code == 404


def test_every_operation_is_described(schema: dict[str, Any]) -> None:
    operations = _operations(schema)
    assert len(operations) == 39

    for method, path, operation in operations:
        where = f"{method.upper()} {path}"
        assert operation.get("summary"), where
        assert operation.get("tags"), where
        assert operation.get("operationId"), where
        assert any(code.startswith("2") for code in operation["responses"]), where
    operation_ids = [operation["operationId"] for _, _, operation in operations]
    assert len(operation_ids) == len(set(operation_ids))
    tags = {tag["name"] for tag in schema["tags"]}
    assert {tag for _, _, op in operations for tag in op["tags"]} == tags
    assert all(tag.get("description") for tag in schema["tags"])


def test_complex_operations_have_long_descriptions(schema: dict[str, Any]) -> None:
    for method, path in (
        ("post", "/v1/auth/refresh"),
        ("post", "/v1/api-keys"),
        ("post", "/v1/invitations"),
        ("post", "/v1/exports"),
        ("delete", "/v1/projects/{project_id}"),
        ("get", "/v1/audit-logs"),
    ):
        assert len(schema["paths"][path][method].get("description", "")) > 80, (method, path)


def test_errors_are_documented_as_problem_json(schema: dict[str, Any]) -> None:
    documented = 0
    for method, path, operation in _operations(schema):
        for status, response in operation["responses"].items():
            if not status.startswith(("4", "5")) or path == "/readyz":
                continue
            content = response.get("content", {})
            assert list(content) == ["application/problem+json"], (method, path, status)
            assert content["application/problem+json"]["schema"] == {
                "$ref": "#/components/schemas/Problem"
            }
            assert response["description"]
            documented += 1
    assert documented > 100
    # FastAPI's default validation error schema is fully replaced.
    assert "HTTPValidationError" not in schema["components"]["schemas"]
    assert "ValidationError" not in schema["components"]["schemas"]


def test_tenant_scoped_operations_document_auth_tenant_header_and_limits(
    schema: dict[str, Any],
) -> None:
    scoped = [(m, p, op) for m, p, op in _operations(schema) if _is_tenant_scoped(m, p)]
    assert len(scoped) == 29

    for method, path, operation in scoped:
        where = f"{method.upper()} {path}"
        assert {"401", "403", "429"} <= set(operation["responses"]), where
        assert {"AccessToken": []} in operation["security"], where
        assert {"ApiKey": []} in operation["security"], where
        headers = {p["name"]: p for p in operation["parameters"] if p["in"] == "header"}
        assert "X-Tenant-ID" in headers, where
        assert headers["X-Tenant-ID"]["description"], where
        if "{" in path:
            assert "404" in operation["responses"], where


def test_public_operations_require_no_credentials(schema: dict[str, Any]) -> None:
    for path, method in (
        ("/healthz", "get"),
        ("/readyz", "get"),
        ("/v1/auth/register", "post"),
        ("/v1/auth/login", "post"),
        ("/v1/auth/refresh", "post"),
        ("/v1/auth/logout", "post"),
    ):
        assert "security" not in schema["paths"][path][method], path


def test_security_schemes_are_declared(schema: dict[str, Any]) -> None:
    schemes = schema["components"]["securitySchemes"]

    assert schemes["AccessToken"]["type"] == "http"
    assert schemes["AccessToken"]["scheme"] == "bearer"
    assert (schemes["ApiKey"]["type"], schemes["ApiKey"]["in"], schemes["ApiKey"]["name"]) == (
        "apiKey",
        "header",
        "X-API-Key",
    )
    assert all(scheme["description"] for scheme in schemes.values())


def test_idempotency_key_is_documented_where_supported(schema: dict[str, Any]) -> None:
    supporting = {
        (method, path)
        for method, path, operation in _operations(schema)
        if any(p["name"] == "Idempotency-Key" for p in operation.get("parameters", []))
    }

    assert supporting == IDEMPOTENT
    parameter = next(
        p
        for p in schema["paths"]["/v1/projects"]["post"]["parameters"]
        if p["name"] == "Idempotency-Key"
    )
    assert parameter["required"] is False
    assert "Idempotent-Replayed" in parameter["description"]


def test_list_operations_document_cursor_pagination(schema: dict[str, Any]) -> None:
    paginated = [
        path
        for method, path, operation in _operations(schema)
        if method == "get"
        and {"limit", "cursor"} <= {p["name"] for p in operation.get("parameters", [])}
    ]

    assert sorted(paginated) == [
        "/v1/api-keys",
        "/v1/audit-logs",
        "/v1/exports",
        "/v1/invitations",
        "/v1/members",
        "/v1/projects",
        "/v1/tasks",
    ]
    for path in paginated:
        ref = schema["paths"][path]["get"]["responses"]["200"]["content"]["application/json"][
            "schema"
        ]["$ref"]
        page = schema["components"]["schemas"][ref.rsplit("/", 1)[1]]
        assert set(page["properties"]) == {"items", "next_cursor"}


def test_secrets_are_only_part_of_creation_responses(schema: dict[str, Any]) -> None:
    schemas = schema["components"]["schemas"]

    assert "key" in schemas["ApiKeyCreated"]["properties"]
    assert "key" not in schemas["ApiKeyRead"]["properties"]
    assert "token" in schemas["InvitationCreated"]["properties"]
    assert "token" not in schemas["InvitationRead"]["properties"]
    assert not any("password" in name for name in schemas["UserRead"]["properties"])
    assert not any(
        "hash" in prop
        for definition in schemas.values()
        for prop in definition.get("properties", {})
    )
