"""Request context middleware: request IDs, access logs, the request body limit
and the last-resort error handler."""

import logging
import re
import time
import uuid

from fastapi import HTTPException
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from teamboard.api.problems import problem_response
from teamboard.context import actor_var, request_id_var, tenant_id_var

logger = logging.getLogger("teamboard.access")

REQUEST_ID_HEADER = "X-Request-ID"
# A client-supplied request ID is echoed into logs, so its alphabet is restricted.
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class RequestContextMiddleware:
    """Pure ASGI middleware (not BaseHTTPMiddleware), so context variables set by
    downstream dependencies stay visible here for the access log."""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_headers = Headers(scope=scope)
        incoming = request_headers.get(REQUEST_ID_HEADER, "")
        request_id = incoming if _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        state = scope.setdefault("state", {})

        context_tokens = (
            (request_id_var, request_id_var.set(request_id)),
            (tenant_id_var, tenant_id_var.set(None)),
            (actor_var, actor_var.set(None)),
        )
        started = time.perf_counter()
        status_code = 500
        response_started = False

        async def send_with_headers(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                headers = MutableHeaders(scope=message)
                headers[REQUEST_ID_HEADER] = request_id
                headers.setdefault("X-Content-Type-Options", "nosniff")
                # Responses contain tenant data: keep them out of shared caches.
                headers.setdefault("Cache-Control", "no-store")
                rate_limit = state.get("rate_limit")
                if rate_limit is not None:
                    headers["RateLimit-Limit"] = str(rate_limit.limit)
                    headers["RateLimit-Remaining"] = str(rate_limit.remaining)
            await send(message)

        too_large = HTTPException(
            status_code=413,
            detail=f"The request body exceeds the limit of {self.max_body_bytes} bytes.",
        )
        received_bytes = 0

        async def receive_with_limit() -> Message:
            # Counts what actually arrives, so the limit also holds for chunked
            # uploads and for clients that understate Content-Length.
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > self.max_body_bytes:
                    raise too_large
            return message

        try:
            declared_length = request_headers.get("content-length", "")
            if declared_length.isdigit() and int(declared_length) > self.max_body_bytes:
                # Refuse up front, without reading the body at all.
                raise too_large
            await self.app(scope, receive_with_limit, send_with_headers)
        except HTTPException as exc:
            if response_started or exc is not too_large:
                raise
            response = problem_response(
                status=413,
                code="payload_too_large",
                title="Content Too Large",
                detail=exc.detail,
                instance=scope["path"],
            )
            await response(scope, receive, send_with_headers)
        except Exception:
            logger.exception("unhandled exception")
            if response_started:
                raise
            # Never leak internals: the client gets a generic problem document and
            # the request ID to quote; the traceback stays in the logs.
            response = problem_response(
                status=500,
                code="internal_error",
                title="Internal Server Error",
                detail="An unexpected error occurred.",
                instance=scope["path"],
            )
            await response(scope, receive, send_with_headers)
        finally:
            client = scope.get("client")
            logger.info(
                "request completed",
                extra={
                    "method": scope["method"],
                    "path": scope["path"],
                    "status": status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "client_ip": client[0] if client else None,
                },
            )
            for variable, token in context_tokens:
                variable.reset(token)
