import uuid

from fastapi import APIRouter, Depends, status

from teamboard.api.deps import MemberServiceDep, Pagination, require
from teamboard.api.problems import problem_responses
from teamboard.domain import Permission
from teamboard.schemas.common import PageResponse
from teamboard.schemas.tenants import MemberRead, MemberUpdate
from teamboard.services.members import Member

router = APIRouter(prefix="/v1/members", tags=["Members"])


def _read(member: Member) -> MemberRead:
    return MemberRead(
        user_id=member.user.id,
        email=member.user.email,
        full_name=member.user.full_name,
        role=member.membership.role,
        joined_at=member.membership.created_at,
    )


@router.get(
    "",
    summary="List members",
    response_model=PageResponse[MemberRead],
    dependencies=[Depends(require(Permission.MEMBERS_READ))],
    responses=problem_responses(401, 403, 429),
)
async def list_members(members: MemberServiceDep, page: Pagination) -> PageResponse[MemberRead]:
    result = await members.list_members(page)
    return PageResponse(items=[_read(m) for m in result.items], next_cursor=result.next_cursor)


@router.patch(
    "/{user_id}",
    summary="Change a member's role",
    response_model=MemberRead,
    dependencies=[Depends(require(Permission.MEMBERS_MANAGE))],
    responses=problem_responses(401, 403, 404, 409, 429),
)
async def change_member_role(
    user_id: uuid.UUID, payload: MemberUpdate, members: MemberServiceDep
) -> MemberRead:
    """Change a member's role.

    You cannot modify a member who outranks you or grant a role above your own, and
    the last owner of a tenant cannot be demoted (`409 last_owner`).
    """
    return _read(await members.change_role(user_id, payload.role))


@router.delete(
    "/{user_id}",
    summary="Remove a member or leave the tenant",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=problem_responses(401, 403, 404, 409, 429),
)
async def remove_member(user_id: uuid.UUID, members: MemberServiceDep) -> None:
    """Remove a member (requires `members:manage`), or leave the tenant by passing
    your own user ID (allowed for every role). Their task assignments are cleared."""
    await members.remove(user_id)
