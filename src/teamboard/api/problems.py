"""RFC 7807 "Problem Details" error responses.

Every error the API returns - domain errors, validation failures, unknown routes,
unexpected exceptions - has the same JSON shape and the media type
``application/problem+json``.
"""

from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from teamboard.context import request_id_var
from teamboard.errors import AppError
from teamboard.schemas.common import Problem

PROBLEM_MEDIA_TYPE = "application/problem+json"

_HTTP_ERROR_CODES = {404: "not_found", 405: "method_not_allowed", 413: "payload_too_large"}


def problem_response(
    *,
    status: int,
    code: str,
    title: str,
    detail: str | None = None,
    instance: str | None = None,
    headers: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"urn:teamboard:problem:{code}",
        "title": title,
        "status": status,
        "detail": detail,
        "instance": instance,
        "code": code,
        "request_id": request_id_var.get(),
    }
    body.update(extra or {})
    return JSONResponse(body, status_code=status, headers=headers, media_type=PROBLEM_MEDIA_TYPE)


async def _handle_app_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)  # noqa: S101 - registered for AppError only
    return problem_response(
        status=exc.status,
        code=exc.code,
        title=exc.title,
        detail=exc.detail,
        instance=request.url.path,
        headers=exc.headers,
        extra=exc.extra,
    )


async def _handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)  # noqa: S101
    errors = [
        {
            "field": ".".join(str(part) for part in error["loc"]),
            "message": error["msg"],
            "type": error["type"],
        }
        for error in exc.errors()
    ]
    return problem_response(
        status=422,
        code="validation_error",
        title="Validation Error",
        detail="The request is invalid. See 'errors' for details.",
        instance=request.url.path,
        extra={"errors": errors},
    )


async def _handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    """Errors raised by the framework itself, e.g. 404 for an unknown route."""
    assert isinstance(exc, StarletteHTTPException)  # noqa: S101
    title = HTTPStatus(exc.status_code).phrase
    return problem_response(
        status=exc.status_code,
        code=_HTTP_ERROR_CODES.get(exc.status_code, "http_error"),
        title=title,
        detail=exc.detail if exc.detail != title else None,
        instance=request.url.path,
        headers=dict(exc.headers) if exc.headers else None,
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _handle_app_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    # Unexpected exceptions are handled in RequestContextMiddleware.


_DESCRIPTIONS = {
    400: "The request is malformed.",
    401: "Missing or invalid credentials.",
    403: "The caller is authenticated but not allowed to do this.",
    404: "The resource does not exist in this tenant.",
    409: "The request conflicts with the current state of the resource.",
    410: "The resource is no longer available.",
    422: "The request body or parameters failed validation.",
    429: "A rate limit or usage quota was exceeded. See the `Retry-After` header.",
    503: "A dependency is temporarily unavailable.",
}


def problem_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """OpenAPI documentation for the error statuses an endpoint can return."""
    return {status: {"model": Problem, "description": _DESCRIPTIONS[status]} for status in statuses}
