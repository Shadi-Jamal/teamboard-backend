import asyncio
import logging
from typing import Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel
from sqlalchemy import text

from teamboard.api.deps import Resources

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Health"])

_CHECK_TIMEOUT_SECONDS = 2.0


class Liveness(BaseModel):
    status: Literal["ok"] = "ok"


class Readiness(BaseModel):
    status: Literal["ready", "unavailable"]
    checks: dict[str, Literal["ok", "failed"]]


@router.get("/healthz", summary="Liveness probe", response_model=Liveness)
async def liveness() -> Liveness:
    """The process is running and able to serve requests.

    Does not touch any dependency, so a database outage does not cause the
    orchestrator to restart healthy containers.
    """
    return Liveness()


@router.get(
    "/readyz",
    summary="Readiness probe",
    response_model=Readiness,
    responses={503: {"model": Readiness, "description": "A dependency is unavailable."}},
)
async def readiness(resources: Resources, response: Response) -> Readiness:
    """The instance can reach PostgreSQL and Redis and should receive traffic."""

    async def check_database() -> None:
        async with resources.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def check_redis() -> None:
        await resources.redis.ping()

    checks: dict[str, Literal["ok", "failed"]] = {}
    for name, check in (("database", check_database), ("redis", check_redis)):
        try:
            await asyncio.wait_for(check(), timeout=_CHECK_TIMEOUT_SECONDS)
            checks[name] = "ok"
        except Exception:
            logger.warning("readiness check failed", extra={"check": name}, exc_info=True)
            checks[name] = "failed"

    ready = all(result == "ok" for result in checks.values())
    if not ready:
        response.status_code = 503
    return Readiness(status="ready" if ready else "unavailable", checks=checks)
