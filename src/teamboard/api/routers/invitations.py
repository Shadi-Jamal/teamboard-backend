import uuid

from fastapi import APIRouter, Depends, status

from teamboard.api.deps import (
    CurrentUser,
    InvitationAcceptanceDep,
    InvitationServiceDep,
    Pagination,
    require,
)
from teamboard.api.problems import problem_responses
from teamboard.domain import Permission
from teamboard.schemas.common import PageResponse
from teamboard.schemas.tenants import (
    InvitationAccept,
    InvitationCreate,
    InvitationCreated,
    InvitationRead,
    MembershipRead,
)

router = APIRouter(prefix="/v1/invitations", tags=["Invitations"])

_manage = [Depends(require(Permission.INVITATIONS_MANAGE))]


@router.post(
    "",
    summary="Invite a user to the tenant",
    status_code=status.HTTP_201_CREATED,
    response_model=InvitationCreated,
    dependencies=_manage,
    responses=problem_responses(401, 403, 409, 429),
)
async def create_invitation(
    payload: InvitationCreate, invitations: InvitationServiceDep
) -> InvitationCreated:
    """Create an invitation for an email address.

    The response contains the invitation `token` **once**; only its hash is stored.
    Deliver it to the invitee, who redeems it with `POST /v1/invitations/accept`.
    Inviting an address again replaces the previous open invitation.
    """
    invitation, token = await invitations.create(payload.email, payload.role)
    return InvitationCreated(**InvitationRead.model_validate(invitation).model_dump(), token=token)


@router.get(
    "",
    summary="List invitations",
    response_model=PageResponse[InvitationRead],
    dependencies=_manage,
    responses=problem_responses(401, 403, 429),
)
async def list_invitations(
    invitations: InvitationServiceDep, page: Pagination
) -> PageResponse[InvitationRead]:
    result = await invitations.list_invitations(page)
    return PageResponse(
        items=[InvitationRead.model_validate(i) for i in result.items],
        next_cursor=result.next_cursor,
    )


@router.delete(
    "/{invitation_id}",
    summary="Revoke an invitation",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=_manage,
    responses=problem_responses(401, 403, 404, 409, 429),
)
async def revoke_invitation(invitation_id: uuid.UUID, invitations: InvitationServiceDep) -> None:
    await invitations.revoke(invitation_id)


@router.post(
    "/accept",
    summary="Accept an invitation",
    response_model=MembershipRead,
    responses=problem_responses(401, 403, 404, 409, 410),
)
async def accept_invitation(
    payload: InvitationAccept, user: CurrentUser, acceptance: InvitationAcceptanceDep
) -> MembershipRead:
    """Join a tenant with an invitation token.

    Called by the invitee with their own access token and **without** `X-Tenant-ID`.
    The account's email must match the address the invitation was issued for.
    """
    return MembershipRead.model_validate(await acceptance.accept(user, payload.token))
