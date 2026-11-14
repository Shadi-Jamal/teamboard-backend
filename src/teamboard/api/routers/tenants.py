from fastapi import APIRouter, Depends, status

from teamboard.api.deps import CurrentUser, TenantServiceDep, TenantSettingsDep, require
from teamboard.api.problems import problem_responses
from teamboard.domain import Permission
from teamboard.schemas.tenants import (
    PlanLimitsRead,
    TenantCreate,
    TenantRead,
    TenantUpdate,
    TenantWithRole,
    UsageCounts,
    UsageRead,
)
from teamboard.services.tenants import TenantMembership

router = APIRouter(prefix="/v1/tenants", tags=["Tenants"])


def _with_role(item: TenantMembership) -> TenantWithRole:
    return TenantWithRole(**TenantRead.model_validate(item.tenant).model_dump(), role=item.role)


@router.post(
    "",
    summary="Create a tenant",
    status_code=status.HTTP_201_CREATED,
    response_model=TenantWithRole,
    responses=problem_responses(401, 403, 409),
)
async def create_tenant(
    payload: TenantCreate, user: CurrentUser, tenants: TenantServiceDep
) -> TenantWithRole:
    """Create a tenant (workspace). The caller becomes its owner.

    The slug is globally unique, which makes this request naturally idempotent: a
    retry answers `409 slug_taken`.
    """
    return _with_role(await tenants.create(user, payload.name, payload.slug))


@router.get(
    "",
    summary="List my tenants",
    response_model=list[TenantWithRole],
    responses=problem_responses(401, 403),
)
async def list_my_tenants(user: CurrentUser, tenants: TenantServiceDep) -> list[TenantWithRole]:
    """All tenants the current user is a member of, with their role in each."""
    return [_with_role(item) for item in await tenants.list_for_user(user)]


@router.get(
    "/current",
    summary="Get the current tenant",
    response_model=TenantRead,
    dependencies=[Depends(require(Permission.TENANT_READ))],
    responses=problem_responses(401, 403, 429),
)
async def get_current_tenant(settings: TenantSettingsDep) -> TenantRead:
    return TenantRead.model_validate(await settings.get())


@router.patch(
    "/current",
    summary="Update the current tenant",
    response_model=TenantRead,
    dependencies=[Depends(require(Permission.TENANT_WRITE))],
    responses=problem_responses(401, 403, 429),
)
async def update_current_tenant(payload: TenantUpdate, settings: TenantSettingsDep) -> TenantRead:
    return TenantRead.model_validate(await settings.rename(payload.name))


@router.get(
    "/current/usage",
    summary="Get plan limits and current usage",
    response_model=UsageRead,
    dependencies=[Depends(require(Permission.TENANT_READ))],
    responses=problem_responses(401, 403, 429),
)
async def get_current_tenant_usage(settings: TenantSettingsDep) -> UsageRead:
    usage = await settings.usage()
    return UsageRead(
        plan=usage.plan,
        limits=PlanLimitsRead.model_validate(usage.limits),
        usage=UsageCounts(
            projects=usage.projects,
            members=usage.members,
            api_keys=usage.api_keys,
            exports_today=usage.exports_today,
        ),
    )
