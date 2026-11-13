"""Cursor (keyset) pagination primitives.

Lists are ordered newest-first by ``(created_at, id)``. The cursor is the position of
the last item on the previous page, so pages stay stable under concurrent inserts and
cost the same regardless of depth (unlike OFFSET).
"""

import base64
import binascii
import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from teamboard.errors import BadRequestError

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


@dataclass(frozen=True, slots=True)
class Cursor:
    created_at: datetime
    id: uuid.UUID

    def encode(self) -> str:
        raw = json.dumps({"c": self.created_at.isoformat(), "i": str(self.id)})
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, value: str) -> "Cursor":
        try:
            padded = value + "=" * (-len(value) % 4)
            data = json.loads(base64.urlsafe_b64decode(padded.encode()))
            created_at = datetime.fromisoformat(data["c"])
            if created_at.tzinfo is None:
                raise ValueError("cursor timestamp must be timezone-aware")
            return cls(created_at=created_at, id=uuid.UUID(data["i"]))
        except (ValueError, KeyError, TypeError, binascii.Error) as exc:
            raise BadRequestError(
                "The pagination cursor is invalid.", code="invalid_cursor"
            ) from exc


@dataclass(frozen=True, slots=True)
class PageParams:
    limit: int = DEFAULT_PAGE_SIZE
    cursor: Cursor | None = None


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: list[T]
    next_cursor: str | None
