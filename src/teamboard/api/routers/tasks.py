import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from teamboard.api.deps import Pagination, TaskServiceDep, require
from teamboard.api.problems import problem_responses
from teamboard.domain import Permission, TaskPriority, TaskStatus
from teamboard.filters import TaskFilters
from teamboard.schemas.common import PageResponse
from teamboard.schemas.work import TaskRead, TaskUpdate

router = APIRouter(prefix="/v1/tasks", tags=["Tasks"])


def get_task_filters(
    project_id: Annotated[
        uuid.UUID | None, Query(description="Only tasks of this project.")
    ] = None,
    status_filter: Annotated[TaskStatus | None, Query(alias="status")] = None,
    priority: Annotated[TaskPriority | None, Query()] = None,
    assignee_id: Annotated[
        uuid.UUID | None, Query(description="Only tasks assigned to this user.")
    ] = None,
    due_before: Annotated[
        date | None, Query(description="Due on or before this date (YYYY-MM-DD).")
    ] = None,
    due_after: Annotated[
        date | None, Query(description="Due on or after this date (YYYY-MM-DD).")
    ] = None,
    q: Annotated[
        str | None,
        Query(
            min_length=1,
            max_length=100,
            description="Case-insensitive match on title or description.",
        ),
    ] = None,
) -> TaskFilters:
    return TaskFilters(
        project_id=project_id,
        status=status_filter,
        priority=priority,
        assignee_id=assignee_id,
        due_before=due_before,
        due_after=due_after,
        search=q,
    )


@router.get(
    "",
    summary="List tasks",
    response_model=PageResponse[TaskRead],
    dependencies=[Depends(require(Permission.TASKS_READ))],
    responses=problem_responses(400, 401, 403, 429),
)
async def list_tasks(
    tasks: TaskServiceDep,
    page: Pagination,
    filters: Annotated[TaskFilters, Depends(get_task_filters)],
) -> PageResponse[TaskRead]:
    """Tasks of the current tenant across all projects, newest first. All filters
    are optional and combined with AND."""
    result = await tasks.list_tasks(page, filters)
    return PageResponse(
        items=[TaskRead.model_validate(t) for t in result.items], next_cursor=result.next_cursor
    )


@router.get(
    "/{task_id}",
    summary="Get a task",
    response_model=TaskRead,
    dependencies=[Depends(require(Permission.TASKS_READ))],
    responses=problem_responses(401, 403, 404, 429),
)
async def get_task(task_id: uuid.UUID, tasks: TaskServiceDep) -> TaskRead:
    return TaskRead.model_validate(await tasks.get(task_id))


@router.patch(
    "/{task_id}",
    summary="Update a task",
    response_model=TaskRead,
    dependencies=[Depends(require(Permission.TASKS_WRITE))],
    responses=problem_responses(401, 403, 404, 422, 429),
)
async def update_task(task_id: uuid.UUID, payload: TaskUpdate, tasks: TaskServiceDep) -> TaskRead:
    """Partial update: only the fields present in the body are changed. Send
    `"assignee_id": null` or `"due_date": null` to clear those fields."""
    return TaskRead.model_validate(await tasks.update(task_id, payload))


@router.delete(
    "/{task_id}",
    summary="Delete a task",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require(Permission.TASKS_WRITE))],
    responses=problem_responses(401, 403, 404, 429),
)
async def delete_task(task_id: uuid.UUID, tasks: TaskServiceDep) -> None:
    """Soft-delete a task. Restore it with `POST /v1/tasks/{task_id}/restore`."""
    await tasks.delete(task_id)


@router.post(
    "/{task_id}/restore",
    summary="Restore a deleted task",
    response_model=TaskRead,
    dependencies=[Depends(require(Permission.TASKS_WRITE))],
    responses=problem_responses(401, 403, 404, 409, 429),
)
async def restore_task(task_id: uuid.UUID, tasks: TaskServiceDep) -> TaskRead:
    return TaskRead.model_validate(await tasks.restore(task_id))
