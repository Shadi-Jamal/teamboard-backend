"""Helpers for translating database errors into domain errors."""

from sqlalchemy.exc import IntegrityError


def violated_constraint(exc: IntegrityError) -> str | None:
    """Name of the constraint that an INSERT/UPDATE violated, if the driver reports it."""
    # SQLAlchemy wraps the asyncpg exception; the original carries the constraint name.
    driver_error = getattr(exc.orig, "__cause__", None)
    name = getattr(driver_error, "constraint_name", None)
    return name if isinstance(name, str) else None
