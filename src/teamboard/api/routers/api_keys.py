import uuid

from fastapi import APIRouter, Depends, status

from teamboard.api.deps import ApiKeyServiceDep, Pagination, require
from teamboard.api.problems import problem_responses
from teamboard.domain import Permission
from teamboard.schemas.common import PageResponse
from teamboard.schemas.tenants import ApiKeyCreate, ApiKeyCreated, ApiKeyRead

router = APIRouter(
    prefix="/v1/api-keys",
    tags=["API keys"],
    dependencies=[Depends(require(Permission.API_KEYS_MANAGE))],
)


@router.post(
    "",
    summary="Create an API key",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiKeyCreated,
    responses=problem_responses(401, 403, 429),
)
async def create_api_key(payload: ApiKeyCreate, api_keys: ApiKeyServiceDep) -> ApiKeyCreated:
    """Create an API key for the current tenant.

    The `key` is returned **only in this response**; the server keeps just a
    SHA-256 hash and cannot show it again. Grantable scopes: `tenant:read`,
    `members:read`, `projects:read`, `projects:write`, `projects:delete`,
    `tasks:read`, `tasks:write`, `audit:read`, `exports:manage`.
    """
    api_key, raw_key = await api_keys.create(payload.name, payload.scopes, payload.expires_at)
    return ApiKeyCreated(**ApiKeyRead.model_validate(api_key).model_dump(), key=raw_key)


@router.get(
    "",
    summary="List API keys",
    response_model=PageResponse[ApiKeyRead],
    responses=problem_responses(401, 403, 429),
)
async def list_api_keys(api_keys: ApiKeyServiceDep, page: Pagination) -> PageResponse[ApiKeyRead]:
    """Keys are listed by name and prefix; the secret itself is never returned."""
    result = await api_keys.list_keys(page)
    return PageResponse(
        items=[ApiKeyRead.model_validate(k) for k in result.items], next_cursor=result.next_cursor
    )


@router.delete(
    "/{key_id}",
    summary="Revoke an API key",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=problem_responses(401, 403, 404, 429),
)
async def revoke_api_key(key_id: uuid.UUID, api_keys: ApiKeyServiceDep) -> None:
    """Revocation takes effect immediately and cannot be undone."""
    await api_keys.revoke(key_id)
