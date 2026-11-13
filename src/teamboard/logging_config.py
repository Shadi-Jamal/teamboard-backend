"""Structured JSON logging.

Every record is one JSON object per line and carries the request ID, tenant ID and
actor of the request being processed (taken from context variables), so a single
request can be traced across log lines without passing loggers around.
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from teamboard.context import actor_var, request_id_var, tenant_id_var

# Attributes present on every LogRecord; anything else was passed via ``extra=``.
_STANDARD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys() | {"message", "asctime"}
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_var.get(),
            "tenant_id": tenant_id_var.get(),
            "actor": actor_var.get(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Route all logging (including uvicorn's) through the JSON formatter."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())

    for name in ("uvicorn", "uvicorn.error", "arq.worker", "arq.jobs"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True
    # Access logs are emitted by RequestContextMiddleware with tenant context instead.
    logging.getLogger("uvicorn.access").disabled = True
