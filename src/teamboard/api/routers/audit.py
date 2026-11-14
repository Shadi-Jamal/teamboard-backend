import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from teamboard.api.deps import AuditServiceDep, Pagination, require
from teamboard.api.problems import problem_responses
from teamboard.domain import Permission
from teamboard.repositories.audit import AuditFilters
from teamboard.schemas.common import PageResponse
from teamboard.schemas.work import AuditLogRead

router = APIRouter(prefix="/v1/audit-logs", tags=["Audit log"])


@router.get(
    "",
    summary="List audit log entries",
    response_model=PageResponse[AuditLogRead],
    dependencies=[Depends(require(Permission.AUDIT_READ))],
    responses=problem_responses(400, 401, 403, 429),
)
async def list_audit_logs(
    audit: AuditServiceDep,
    page: Pagination,
    entity_type: Annotated[
        str | None, Query(max_length=50, description="e.g. `project`, `task`, `member`.")
    ] = None,
    entity_id: Annotated[uuid.UUID | None, Query()] = None,
    actor_id: Annotated[
        uuid.UUID | None, Query(description="User ID or API key ID that made the change.")
    ] = None,
    action: Annotated[str | None, Query(max_length=100, description="e.g. `task.updated`.")] = None,
) -> PageResponse[AuditLogRead]:
    """Who changed what and when, newest first. The log is append-only: entries are
    written in the same transaction as the change they describe and can be neither
    modified nor deleted through the API."""
    result = await audit.list_entries(
        page,
        AuditFilters(
            entity_type=entity_type, entity_id=entity_id, actor_id=actor_id, action=action
        ),
    )
    return PageResponse(
        items=[AuditLogRead.model_validate(e) for e in result.items],
        next_cursor=result.next_cursor,
    )
