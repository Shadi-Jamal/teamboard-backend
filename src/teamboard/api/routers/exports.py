import uuid

from fastapi import APIRouter, Depends, Response, status
from fastapi.responses import JSONResponse

from teamboard.api.deps import ExportServiceDep, IdempotencyKeyHeader, Pagination, require
from teamboard.api.problems import problem_responses
from teamboard.api.routers.projects import REPLAY_HEADER
from teamboard.domain import Permission
from teamboard.schemas.common import PageResponse
from teamboard.schemas.work import ExportDocument, ExportRead

router = APIRouter(
    prefix="/v1/exports",
    tags=["Exports"],
    dependencies=[Depends(require(Permission.EXPORTS_MANAGE))],
)


@router.post(
    "",
    summary="Request a data export",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ExportRead,
    responses=problem_responses(401, 403, 422, 429, 503),
)
async def create_export(
    exports: ExportServiceDep,
    response: Response,
    idempotency_key: IdempotencyKeyHeader = None,
) -> ExportRead:
    """Start an asynchronous export of all projects and tasks of the tenant.

    Returns immediately with a job in status `queued`. A background worker builds
    the document, retrying with exponential backoff on failure. Poll
    `GET /v1/exports/{export_id}` until the status is `succeeded` (or `failed`),
    then fetch the result from `/download`.

    Counts against the plan's daily export quota. Supports `Idempotency-Key`.
    """
    outcome = await exports.create(idempotency_key)
    if outcome.replayed:
        response.headers[REPLAY_HEADER] = "true"
    return outcome.value


@router.get(
    "",
    summary="List exports",
    response_model=PageResponse[ExportRead],
    responses=problem_responses(400, 401, 403, 429),
)
async def list_exports(exports: ExportServiceDep, page: Pagination) -> PageResponse[ExportRead]:
    result = await exports.list_exports(page)
    return PageResponse(
        items=[ExportRead.model_validate(job) for job in result.items],
        next_cursor=result.next_cursor,
    )


@router.get(
    "/{export_id}",
    summary="Get the status of an export",
    response_model=ExportRead,
    responses=problem_responses(401, 403, 404, 429),
)
async def get_export(export_id: uuid.UUID, exports: ExportServiceDep) -> ExportRead:
    return ExportRead.model_validate(await exports.get(export_id))


@router.get(
    "/{export_id}/download",
    summary="Download a finished export",
    response_model=ExportDocument,
    responses=problem_responses(401, 403, 404, 409, 429),
)
async def download_export(export_id: uuid.UUID, exports: ExportServiceDep) -> JSONResponse:
    """The export document. `409 export_not_ready` until the job has succeeded."""
    document = await exports.download(export_id)
    return JSONResponse(
        document,
        headers={
            "Content-Disposition": f'attachment; filename="teamboard-export-{export_id}.json"'
        },
    )
