import asyncio
import uuid

import httpx

from teamboard.domain import Role
from teamboard.security.tokens import hash_token
from tests.support import (
    AdminDb,
    ApiDriver,
    UserSession,
    api_key_headers,
    assert_problem,
    unique,
)


async def _accept(api: ApiDriver, user: UserSession, token: str) -> httpx.Response:
    return await api.client.post("/v1/invitations/accept", json={"token": token}, headers=user.auth)


async def test_invitation_token_is_shown_once_and_stored_hashed(
    api: ApiDriver, db: AdminDb
) -> None:
    ws = await api.create_workspace()
    email = f"{unique('invitee')}@example.com"

    created = await api.client.post(
        "/v1/invitations", json={"email": email.upper(), "role": "viewer"}, headers=ws.headers()
    )

    assert created.status_code == 201
    body = created.json()
    assert body["token"].startswith("tbi_")
    assert (body["email"], body["role"]) == (email, "viewer")
    assert body["invited_by_user_id"] == ws.owner.id
    assert body["accepted_at"] is None

    listed = (await api.client.get("/v1/invitations", headers=ws.headers())).json()["items"]
    assert [i["id"] for i in listed] == [body["id"]]
    assert "token" not in listed[0]
    stored = await db.scalar(
        "SELECT token_hash FROM invitations WHERE id = :id", id=uuid.UUID(body["id"])
    )
    assert stored == hash_token(body["token"])


async def test_accepting_an_invitation_creates_the_membership(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    invitee = await api.register()
    invitation = await api.invite(ws, invitee.email, Role.MEMBER)
    before = await api.client.get("/v1/projects", headers=ws.headers(invitee))

    accepted = await _accept(api, invitee, invitation["token"])

    assert_problem(before, 403, "tenant_access_denied")
    assert accepted.status_code == 200
    assert accepted.json() == {
        "tenant_id": ws.tenant_id,
        "user_id": invitee.id,
        "role": "member",
    }
    assert (await api.client.get("/v1/projects", headers=ws.headers(invitee))).status_code == 200
    listed = (await api.client.get("/v1/invitations", headers=ws.headers())).json()["items"]
    assert listed[0]["accepted_at"] is not None


async def test_invitation_can_only_be_accepted_by_the_invited_address(api: ApiDriver) -> None:
    """A leaked token is useless to anyone but the intended recipient."""
    ws = await api.create_workspace()
    invitee, thief = await api.register(), await api.register()
    invitation = await api.invite(ws, invitee.email, Role.ADMIN)

    stolen = await _accept(api, thief, invitation["token"])

    assert_problem(stolen, 403, "invitation_email_mismatch")
    assert_problem(
        await api.client.get("/v1/projects", headers=ws.headers(thief)), 403, "tenant_access_denied"
    )
    assert (await _accept(api, invitee, invitation["token"])).status_code == 200


async def test_invitation_cannot_be_accepted_twice(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    invitee = await api.register()
    invitation = await api.invite(ws, invitee.email, Role.MEMBER)
    await _accept(api, invitee, invitation["token"])

    again = await _accept(api, invitee, invitation["token"])

    assert_problem(again, 409, "invitation_already_accepted")


async def test_concurrent_acceptance_creates_exactly_one_membership(
    api: ApiDriver, db: AdminDb
) -> None:
    ws = await api.create_workspace()
    invitee = await api.register()
    invitation = await api.invite(ws, invitee.email, Role.MEMBER)

    responses = await asyncio.gather(
        *(_accept(api, invitee, invitation["token"]) for _ in range(5))
    )

    assert sorted(r.status_code for r in responses) == [200, 409, 409, 409, 409]
    memberships = await db.scalar(
        "SELECT count(*) FROM memberships WHERE tenant_id = :t AND user_id = :u",
        t=uuid.UUID(ws.tenant_id),
        u=uuid.UUID(invitee.id),
    )
    assert memberships == 1


async def test_expired_invitation_is_gone(api: ApiDriver, db: AdminDb) -> None:
    ws = await api.create_workspace()
    invitee = await api.register()
    invitation = await api.invite(ws, invitee.email, Role.MEMBER)
    await db.execute(
        "UPDATE invitations SET expires_at = now() - interval '1 minute' WHERE id = :id",
        id=uuid.UUID(invitation["id"]),
    )

    assert_problem(await _accept(api, invitee, invitation["token"]), 410, "invitation_expired")


async def test_revoked_invitation_is_gone(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    invitee = await api.register()
    invitation = await api.invite(ws, invitee.email, Role.MEMBER)

    revoked = await api.client.delete(f"/v1/invitations/{invitation['id']}", headers=ws.headers())
    again = await api.client.delete(f"/v1/invitations/{invitation['id']}", headers=ws.headers())

    assert revoked.status_code == 204
    assert again.status_code == 204  # revoking is idempotent
    assert_problem(await _accept(api, invitee, invitation["token"]), 410, "invitation_revoked")


async def test_accepted_invitation_cannot_be_revoked(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    invitee = await api.register()
    invitation = await api.invite(ws, invitee.email, Role.MEMBER)
    await _accept(api, invitee, invitation["token"])

    response = await api.client.delete(f"/v1/invitations/{invitation['id']}", headers=ws.headers())

    assert_problem(response, 409, "invitation_already_accepted")


async def test_reinviting_replaces_the_open_invitation(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    invitee = await api.register()
    first = await api.invite(ws, invitee.email, Role.VIEWER)
    second = await api.invite(ws, invitee.email, Role.ADMIN)

    assert_problem(await _accept(api, invitee, first["token"]), 410, "invitation_revoked")
    accepted = await _accept(api, invitee, second["token"])
    assert accepted.json()["role"] == "admin"


async def test_unknown_token_is_not_found(api: ApiDriver) -> None:
    user = await api.register()

    assert_problem(await _accept(api, user, "tbi_no-such-token"), 404, "invitation_not_found")


async def test_existing_member_cannot_be_invited_again(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    member = await api.add_member(ws, Role.MEMBER)

    response = await api.client.post(
        "/v1/invitations", json={"email": member.email, "role": "admin"}, headers=ws.headers()
    )

    assert_problem(response, 409, "already_member")


async def test_inviting_does_not_reveal_whether_an_account_exists(api: ApiDriver) -> None:
    """Inviting a registered stranger and an unregistered address look the same."""
    ws = await api.create_workspace()
    stranger = await api.register()

    registered = await api.client.post(
        "/v1/invitations", json={"email": stranger.email}, headers=ws.headers()
    )
    unregistered = await api.client.post(
        "/v1/invitations", json={"email": f"{unique('nobody')}@example.com"}, headers=ws.headers()
    )

    assert registered.status_code == unregistered.status_code == 201
    assert set(registered.json()) == set(unregistered.json())


async def test_ownership_cannot_be_granted_by_invitation(api: ApiDriver) -> None:
    ws = await api.create_workspace()

    response = await api.client.post(
        "/v1/invitations", json={"email": "boss@example.com", "role": "owner"}, headers=ws.headers()
    )

    assert_problem(response, 422, "invalid_invitation_role")


async def test_invitations_respect_the_plan_seat_limit(api: ApiDriver) -> None:
    ws = await api.create_workspace()  # free plan: 5 members including the owner
    for _ in range(3):
        await api.add_member(ws, Role.VIEWER)
    last_seat = await api.register()
    one_too_many = await api.register()
    seat_five = await api.invite(ws, last_seat.email, Role.VIEWER)
    seat_six = await api.invite(ws, one_too_many.email, Role.VIEWER)

    assert (await _accept(api, last_seat, seat_five["token"])).status_code == 200
    # Full: the outstanding invitation can no longer be redeemed, nor new ones issued.
    assert_problem(
        await _accept(api, one_too_many, seat_six["token"]), 403, "member_quota_exceeded"
    )
    full = await api.client.post(
        "/v1/invitations", json={"email": "late@example.com"}, headers=ws.headers()
    )
    assert_problem(full, 403, "member_quota_exceeded")


async def test_api_keys_cannot_accept_or_manage_invitations(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    _, key = await api.create_api_key(ws, ["members:read"])
    invitation = await api.invite(ws, "someone@example.com", Role.VIEWER)

    accept = await api.client.post(
        "/v1/invitations/accept", json={"token": invitation["token"]}, headers=api_key_headers(key)
    )
    create = await api.client.post(
        "/v1/invitations", json={"email": "x@example.com"}, headers=api_key_headers(key)
    )

    assert_problem(accept, 403, "user_token_required")
    assert_problem(create, 403, "permission_denied")


async def test_unknown_invitation_cannot_be_revoked(api: ApiDriver) -> None:
    ws = await api.create_workspace()

    response = await api.client.delete(f"/v1/invitations/{uuid.uuid4()}", headers=ws.headers())

    assert_problem(response, 404, "not_found")
