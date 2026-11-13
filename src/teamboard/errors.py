"""Domain errors.

Services raise these; the API layer renders them as RFC 7807 problem documents
(see ``teamboard.api.problems``). Each error has a stable machine-readable ``code``.
"""

from typing import Any


class AppError(Exception):
    status: int = 500
    code: str = "internal_error"
    title: str = "Internal Server Error"

    def __init__(
        self,
        detail: str | None = None,
        *,
        code: str | None = None,
        headers: dict[str, str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail or self.title)
        self.detail = detail
        if code is not None:
            self.code = code
        self.headers = headers or {}
        self.extra = extra or {}


class BadRequestError(AppError):
    status = 400
    code = "bad_request"
    title = "Bad Request"


class AuthenticationError(AppError):
    status = 401
    code = "unauthenticated"
    title = "Unauthenticated"

    def __init__(self, detail: str | None = None, *, code: str | None = None) -> None:
        super().__init__(detail, code=code, headers={"WWW-Authenticate": "Bearer"})


class PermissionDeniedError(AppError):
    status = 403
    code = "permission_denied"
    title = "Forbidden"


class QuotaExceededError(AppError):
    """A plan limit on stored resources (projects, members, ...) was reached."""

    status = 403
    code = "quota_exceeded"
    title = "Plan Quota Exceeded"


class NotFoundError(AppError):
    status = 404
    code = "not_found"
    title = "Not Found"


class ConflictError(AppError):
    status = 409
    code = "conflict"
    title = "Conflict"


class GoneError(AppError):
    status = 410
    code = "gone"
    title = "Gone"


class UnprocessableError(AppError):
    status = 422
    code = "unprocessable"
    title = "Unprocessable Content"


class RateLimitedError(AppError):
    status = 429
    code = "rate_limited"
    title = "Too Many Requests"

    def __init__(
        self, detail: str | None = None, *, retry_after: int, code: str | None = None
    ) -> None:
        super().__init__(detail, code=code)
        self.headers = {"Retry-After": str(max(1, retry_after))}
