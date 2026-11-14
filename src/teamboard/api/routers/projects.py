import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from teamboard.api.deps import (
    IdempotencyKeyHeader,
    Pagination,
    ProjectServiceDep,
    TaskServiceDep,
    require,
)
from teamboard.api.problems import problem_responses
from teamboard.domain import Permission, ProjectStatus
from teamboard.schemas.common import PageResponse
from teamboard.schemas.work import (
    ProjectCreate,
    ProjectRead,
    ProjectUpdate,
    TaskCreate,
    TaskRead,
)

router = APIRouter(prefix="/v1/projects", tags=["Projects"])

REPLAY_HEADER = "Idempotent-Replayed"


@router.post(
    "",
    summary="Create a project",
    status_code=status.HTTP_201_CREATED,
    response_model=ProjectRead,
    dependencies=[Depends(require(Permission.PROJECTS_WRITE))],
    responses=problem_responses(401, 403, 409, 422, 429),
)
async def create_project(
    payload: ProjectCreate,
    projects: ProjectServiceDep,
    response: Response,
    idempotency_key: IdempotencyKeyHeader = None,
) -> ProjectRead:
    """Create a project. Names are unique within a tenant.

    Supports the `Idempotency-Key` header for safe retries.
    """
    outcome = await projects.create(payload, idempotency_key)
    if outcome.replayed:
        response.headers[REPLAY_HEADER] = "true"
    return outcome.value


@router.get(
    "",
    summary="List projects",
    response_model=PageResponse[ProjectRead],
    dependencies=[Depends(require(Permission.PROJECTS_READ))],
    responses=problem_responses(400, 401, 403, 429),
)
async def list_projects(
    projects: ProjectServiceDep,
    page: Pagination,
    status_filter: Annotated[
        ProjectStatus | None, Query(alias="status", description="Only projects in this status.")
    ] = None,
    q: Annotated[
        str | None,
        Query(min_length=1, max_length=100, description="Case-insensitive match on the name."),
    ] = None,
) -> PageResponse[ProjectRead]:
    """Projects of the current tenant, newest first, with cursor pagination."""
    result = await projects.list_projects(page, status=status_filter, search=q)
    return PageResponse(
        items=[ProjectRead.model_validate(p) for p in result.items], next_cursor=result.next_cursor
    )


@router.get(
    "/{project_id}",
    summary="Get a project",
    response_model=ProjectRead,
    dependencies=[Depends(require(Permission.PROJECTS_READ))],
    responses=problem_responses(401, 403, 404, 429),
)
async def get_project(project_id: uuid.UUID, projects: ProjectServiceDep) -> ProjectRead:
    return ProjectRead.model_validate(await projects.get(project_id))


@router.patch(
    "/{project_id}",
    summary="Update a project",
    response_model=ProjectRead,
    dependencies=[Depends(require(Permission.PROJECTS_WRITE))],
    responses=problem_responses(401, 403, 404, 409, 429),
)
async def update_project(
    project_id: uuid.UUID, payload: ProjectUpdate, projects: ProjectServiceDep
) -> ProjectRead:
    """Partial update: only the fields present in the body are changed. Set
    `status` to `archived` to freeze a project (no new tasks)."""
    return ProjectRead.model_validate(await projects.update(project_id, payload))


@router.delete(
    "/{project_id}",
    summary="Delete a project",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require(Permission.PROJECTS_DELETE))],
    responses=problem_responses(401, 403, 404, 429),
)
async def delete_project(project_id: uuid.UUID, projects: ProjectServiceDep) -> None:
    """Soft-delete a project and its tasks. They disappear from the API but can be
    brought back with `POST /v1/projects/{project_id}/restore`."""
    await projects.delete(project_id)


@router.post(
    "/{project_id}/restore",
    summary="Restore a deleted project",
    response_model=ProjectRead,
    dependencies=[Depends(require(Permission.PROJECTS_DELETE))],
    responses=problem_responses(401, 403, 404, 409, 429),
)
async def restore_project(project_id: uuid.UUID, projects: ProjectServiceDep) -> ProjectRead:
    """Undo a deletion, including the tasks that were deleted along with the project."""
    return ProjectRead.model_validate(await projects.restore(project_id))


@router.post(
    "/{project_id}/tasks",
    summary="Create a task in a project",
    status_code=status.HTTP_201_CREATED,
    response_model=TaskRead,
    tags=["Tasks"],
    dependencies=[Depends(require(Permission.TASKS_WRITE))],
    responses=problem_responses(401, 403, 404, 409, 422, 429),
)
async def create_task(
    project_id: uuid.UUID,
    payload: TaskCreate,
    tasks: TaskServiceDep,
    response: Response,
    idempotency_key: IdempotencyKeyHeader = None,
) -> TaskRead:
    """Create a task. The assignee, if given, must be a member of the tenant.

    Supports the `Idempotency-Key` header for safe retries.
    """
    outcome = await tasks.create(project_id, payload, idempotency_key)
    if outcome.replayed:
        response.headers[REPLAY_HEADER] = "true"
    return outcome.value
