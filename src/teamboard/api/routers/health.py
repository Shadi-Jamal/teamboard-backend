from typing import Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel

from teamboard.api.deps import HealthServiceDep
from teamboard.services.health import CheckResult

router = APIRouter(tags=["Health"])


class Liveness(BaseModel):
    status: Literal["ok"] = "ok"


class Readiness(BaseModel):
    status: Literal["ready", "unavailable"]
    checks: dict[str, CheckResult]


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
async def readiness(health: HealthServiceDep, response: Response) -> Readiness:
    """The instance can reach PostgreSQL and Redis and should receive traffic."""
    checks = await health.check_dependencies()
    ready = all(result == "ok" for result in checks.values())
    if not ready:
        response.status_code = 503
    return Readiness(status="ready" if ready else "unavailable", checks=checks)
