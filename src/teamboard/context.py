"""Per-request context shared with logging and the audit log."""

from contextvars import ContextVar

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
tenant_id_var: ContextVar[str | None] = ContextVar("tenant_id", default=None)
actor_var: ContextVar[str | None] = ContextVar("actor", default=None)
