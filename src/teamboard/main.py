"""Application factory."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute

from teamboard import __version__
from teamboard.api.deps import AppResources
from teamboard.api.middleware import RequestContextMiddleware
from teamboard.api.problems import (
    PROBLEM_MEDIA_TYPE,
    problem_responses,
    register_exception_handlers,
)
from teamboard.api.routers import (
    api_keys,
    audit,
    auth,
    exports,
    health,
    invitations,
    members,
    projects,
    tasks,
    tenants,
)
from teamboard.config import Settings, get_settings
from teamboard.db.engine import assert_role_is_subject_to_rls, create_engine, create_sessionmaker
from teamboard.infra.queue import JobQueue
from teamboard.infra.rate_limit import RateLimiter, UsageCounter
from teamboard.infra.redis import create_redis
from teamboard.logging_config import configure_logging
from teamboard.security.passwords import PasswordService
from teamboard.security.tokens import AccessTokenService

logger = logging.getLogger(__name__)

_DESCRIPTION = """
TeamBoard is a multi-tenant project and task management API.

## Authentication

* **Users** log in at `POST /v1/auth/login` and send the access token as
  `Authorization: Bearer <token>`. A user can belong to several tenants, so every
  tenant-scoped request also names its tenant in the **`X-Tenant-ID`** header.
* **API keys** are sent as `X-API-Key: <key>`. A key belongs to exactly one tenant
  and carries its own scopes; `X-Tenant-ID` is not needed.

## Conventions

* **Errors** are RFC 7807 problem documents (`application/problem+json`) with a
  stable `code` and the `request_id` to quote in support requests.
* **Lists** use cursor pagination: pass `next_cursor` of one page as `cursor` of
  the next. Results are ordered newest first.
* **Isolation**: a resource of another tenant is indistinguishable from one that
  does not exist (`404`).
* **Rate limits** are per tenant; responses carry `RateLimit-Limit` and
  `RateLimit-Remaining`, and `429` responses a `Retry-After` header.
* **Idempotency**: creating endpoints accept an `Idempotency-Key` header.
"""

_TAGS = [
    {"name": "Auth", "description": "Accounts, login and token rotation."},
    {"name": "Tenants", "description": "Workspaces, their settings and plan usage."},
    {"name": "Members", "description": "Who belongs to the tenant, and with which role."},
    {"name": "Invitations", "description": "Inviting users to a tenant."},
    {"name": "API keys", "description": "Scoped machine credentials for a tenant."},
    {"name": "Projects", "description": "Projects group tasks."},
    {"name": "Tasks", "description": "Units of work inside a project."},
    {"name": "Audit log", "description": "Append-only history of changes."},
    {"name": "Exports", "description": "Asynchronous export of tenant data."},
    {"name": "Health", "description": "Probes for orchestrators and load balancers."},
]


# Every router of the application. Tests enumerate this to prove that each route is
# either explicitly tenant-free or covered by the cross-tenant leak matrix.
ROUTERS = (
    health.router,
    auth.router,
    tenants.router,
    members.router,
    invitations.router,
    api_keys.router,
    projects.router,
    tasks.router,
    audit.router,
    exports.router,
)


def _operation_id(route: APIRoute) -> str:
    return route.name


def _use_problem_media_type(schema: dict[str, Any]) -> None:
    """Document error responses with the media type they are actually sent with."""
    for path_item in schema.get("paths", {}).values():
        for operation in path_item.values():
            if not isinstance(operation, dict):
                continue
            for status_code, response in operation.get("responses", {}).items():
                content = response.get("content", {})
                is_problem = (
                    content.get("application/json", {})
                    .get("schema", {})
                    .get("$ref", "")
                    .endswith("/Problem")
                )
                if status_code.startswith(("4", "5")) and is_problem:
                    content[PROBLEM_MEDIA_TYPE] = content.pop("application/json")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings)
        try:
            # Fail fast if tenant isolation would be silently disabled.
            await assert_role_is_subject_to_rls(engine)
            redis = await create_redis(settings)
        except BaseException:
            await engine.dispose()
            raise
        app.state.resources = AppResources(
            settings=settings,
            engine=engine,
            sessionmaker=create_sessionmaker(engine),
            redis=redis,
            passwords=PasswordService(settings),
            access_tokens=AccessTokenService(settings),
            rate_limiter=RateLimiter(redis),
            counter=UsageCounter(redis),
            queue=JobQueue(redis),
        )
        logger.info("application started", extra={"environment": settings.environment})
        try:
            yield
        finally:
            await redis.aclose()
            await engine.dispose()

    app = FastAPI(
        title="TeamBoard API",
        version=__version__,
        description=_DESCRIPTION,
        lifespan=lifespan,
        openapi_tags=_TAGS,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        generate_unique_id_function=_operation_id,
        # Replaces FastAPI's default validation error schema with the problem format.
        responses=problem_responses(422),
    )
    app.add_middleware(RequestContextMiddleware, max_body_bytes=settings.max_request_body_bytes)
    register_exception_handlers(app)

    for router in ROUTERS:
        app.include_router(router)

    default_openapi = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            _use_problem_media_type(default_openapi())
        assert app.openapi_schema is not None  # noqa: S101 - set by default_openapi()
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]
    return app
