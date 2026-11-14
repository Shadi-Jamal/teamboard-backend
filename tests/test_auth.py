import asyncio
import base64
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest

from teamboard.api.deps import AppResources
from teamboard.config import Settings
from teamboard.security.passwords import PasswordService
from teamboard.security.tokens import hash_token
from tests.support import PASSWORD, AdminDb, ApiDriver, assert_problem, unique


def _email() -> str:
    return f"{unique('auth')}@example.com"


def _forge_token(settings: Settings, **overrides: object) -> str:
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "iss": settings.jwt_issuer,
        "sub": str(uuid.uuid4()),
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "typ": "access",
    }
    secret = str(overrides.pop("secret", settings.jwt_secret.get_secret_value()))
    claims.update(overrides)
    return jwt.encode(claims, secret, algorithm="HS256")


# ------------------------------------------------------------------ registration


async def test_register_creates_an_account(client: httpx.AsyncClient, db: AdminDb) -> None:
    email = _email()
    response = await client.post(
        "/v1/auth/register",
        json={"email": email, "password": PASSWORD, "full_name": "  Ada Lovelace "},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == email
    assert body["full_name"] == "Ada Lovelace"
    assert set(body) == {"id", "email", "full_name", "created_at"}  # no password material

    stored_hash = await db.scalar("SELECT password_hash FROM users WHERE email = :e", e=email)
    assert stored_hash.startswith("$argon2id$")
    assert PASSWORD not in stored_hash


async def test_register_normalizes_email_and_rejects_duplicates(client: httpx.AsyncClient) -> None:
    email = _email()
    payload = {"password": PASSWORD, "full_name": "First"}
    first = await client.post("/v1/auth/register", json={"email": email.upper(), **payload})
    second = await client.post("/v1/auth/register", json={"email": email, **payload})

    assert first.status_code == 201
    assert first.json()["email"] == email
    assert_problem(second, 409, "email_already_registered")


@pytest.mark.parametrize(
    ("payload", "invalid_field"),
    [
        ({"email": "a@example.com", "password": "too-short", "full_name": "A"}, "body.password"),
        ({"email": "not-an-email", "password": PASSWORD, "full_name": "A"}, "body.email"),
        ({"email": "a@example.com", "password": PASSWORD, "full_name": ""}, "body.full_name"),
        ({"email": "a@example.com", "password": "x" * 129, "full_name": "A"}, "body.password"),
        ({"email": "a@example.com", "password": PASSWORD}, "body.full_name"),
    ],
)
async def test_register_validates_input(
    client: httpx.AsyncClient, payload: dict[str, str], invalid_field: str
) -> None:
    response = await client.post("/v1/auth/register", json=payload)

    body = assert_problem(response, 422, "validation_error")
    assert [error["field"] for error in body["errors"]] == [invalid_field]


# ------------------------------------------------------------------------- login


async def test_login_returns_a_token_pair(api: ApiDriver, settings: Settings) -> None:
    user = await api.register()

    response = await api.client.post(
        "/v1/auth/login", json={"email": user.email.upper(), "password": PASSWORD}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == settings.access_token_ttl_seconds
    assert body["refresh_token"].startswith("tbr_")
    claims = jwt.decode(
        body["access_token"],
        settings.jwt_secret.get_secret_value(),
        algorithms=["HS256"],
        issuer=settings.jwt_issuer,
    )
    assert claims["sub"] == user.id
    assert claims["typ"] == "access"
    assert response.headers["Cache-Control"] == "no-store"


async def test_login_failure_does_not_reveal_whether_the_account_exists(api: ApiDriver) -> None:
    user = await api.register()

    wrong_password = await api.client.post(
        "/v1/auth/login", json={"email": user.email, "password": "definitely-wrong-password"}
    )
    unknown_email = await api.client.post(
        "/v1/auth/login", json={"email": _email(), "password": PASSWORD}
    )

    first = assert_problem(wrong_password, 401, "invalid_credentials")
    second = assert_problem(unknown_email, 401, "invalid_credentials")
    assert first["detail"] == second["detail"]
    assert wrong_password.headers["WWW-Authenticate"] == "Bearer"


async def test_refresh_tokens_are_stored_hashed(api: ApiDriver, db: AdminDb) -> None:
    user = await api.register()

    hashes = await db.column(
        "SELECT token_hash FROM refresh_tokens WHERE user_id = :u", u=uuid.UUID(user.id)
    )

    assert hashes == [hash_token(user.refresh_token)]
    assert user.refresh_token not in hashes


async def test_repeated_login_failures_lock_the_account_temporarily(
    api: ApiDriver, resources: AppResources, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(resources.settings, "login_failure_limit", 3)
    victim, bystander = await api.register(), await api.register()

    for _ in range(3):
        response = await api.client.post(
            "/v1/auth/login", json={"email": victim.email, "password": "wrong-password-123"}
        )
        assert_problem(response, 401, "invalid_credentials")

    # Locked: even the correct password is refused until the window expires.
    locked = await api.client.post(
        "/v1/auth/login", json={"email": victim.email, "password": PASSWORD}
    )
    assert_problem(locked, 429, "too_many_login_attempts")
    assert 0 < int(locked.headers["Retry-After"]) <= resources.settings.login_failure_window_seconds
    # The lock is per account, not global.
    await api.login(bystander.email)


async def test_successful_login_clears_the_failure_counter(
    api: ApiDriver, resources: AppResources, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(resources.settings, "login_failure_limit", 3)
    user = await api.register()
    wrong = {"email": user.email, "password": "wrong-password-123"}

    for _ in range(2):
        await api.client.post("/v1/auth/login", json=wrong)
    await api.login(user.email)
    for _ in range(2):
        response = await api.client.post("/v1/auth/login", json=wrong)
        assert_problem(response, 401, "invalid_credentials")

    await api.login(user.email)  # still not locked: the counter restarted


async def test_auth_endpoints_are_rate_limited_per_client(
    client: httpx.AsyncClient, resources: AppResources, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(resources.settings, "auth_rate_limit_per_minute", 3)
    payload = {"email": _email(), "password": PASSWORD}

    statuses = [(await client.post("/v1/auth/login", json=payload)).status_code for _ in range(3)]
    throttled = await client.post("/v1/auth/login", json=payload)

    assert statuses == [401, 401, 401]
    assert_problem(throttled, 429, "rate_limited")
    assert int(throttled.headers["Retry-After"]) >= 1


async def test_login_upgrades_password_hashes_created_with_weaker_parameters(
    api: ApiDriver,
    db: AdminDb,
    resources: AppResources,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await api.register()
    before = await db.scalar("SELECT password_hash FROM users WHERE email = :e", e=user.email)
    stronger = PasswordService(settings.model_copy(update={"argon2_time_cost": 2}))
    monkeypatch.setattr(resources, "passwords", stronger)

    await api.login(user.email)

    after = await db.scalar("SELECT password_hash FROM users WHERE email = :e", e=user.email)
    assert ",t=1," in before
    assert ",t=2," in after
    await api.login(user.email)  # the upgraded hash still verifies


async def test_disabled_account_cannot_log_in_or_use_existing_tokens(
    api: ApiDriver, db: AdminDb
) -> None:
    user = await api.register()
    await db.execute("UPDATE users SET is_active = false WHERE email = :e", e=user.email)

    login = await api.client.post(
        "/v1/auth/login", json={"email": user.email, "password": PASSWORD}
    )
    me = await api.client.get("/v1/auth/me", headers=user.auth)
    refresh = await api.client.post("/v1/auth/refresh", json={"refresh_token": user.refresh_token})

    assert_problem(login, 401, "account_disabled")
    assert_problem(me, 401, "invalid_token")  # takes effect before the JWT expires
    assert_problem(refresh, 401, "invalid_refresh_token")


# ----------------------------------------------------------------- access tokens


async def test_me_returns_the_current_user(api: ApiDriver) -> None:
    user = await api.register(full_name="Grace Hopper")

    response = await api.client.get("/v1/auth/me", headers=user.auth)

    assert response.status_code == 200
    assert response.json()["id"] == user.id
    assert response.json()["full_name"] == "Grace Hopper"


async def test_requests_without_credentials_are_rejected(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/auth/me")

    assert_problem(response, 401, "not_authenticated")
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "forgery",
    [
        {"exp": datetime.now(UTC) - timedelta(seconds=1)},  # expired
        {"secret": "another-secret-that-is-long-enough-123"},  # wrong signature
        {"iss": "someone-else"},  # wrong issuer
        {"typ": "refresh"},  # not an access token
        {"sub": "not-a-uuid"},
    ],
    ids=["expired", "wrong-signature", "wrong-issuer", "wrong-type", "malformed-subject"],
)
async def test_forged_or_stale_access_tokens_are_rejected(
    client: httpx.AsyncClient, settings: Settings, forgery: dict[str, object]
) -> None:
    token = _forge_token(settings, **forgery)

    response = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert_problem(response, 401, "invalid_token")


async def test_token_for_a_nonexistent_user_is_rejected(
    client: httpx.AsyncClient, settings: Settings
) -> None:
    token = _forge_token(settings)  # valid signature, but the subject does not exist

    response = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert_problem(response, 401, "invalid_token")


async def test_unsigned_alg_none_token_is_rejected(api: ApiDriver) -> None:
    """The classic JWT downgrade: a token claiming alg=none for a real user."""
    user = await api.register()

    def b64(data: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    header = b64({"alg": "none", "typ": "JWT"})
    payload = b64({"iss": "teamboard", "sub": user.id, "typ": "access", "exp": 9_999_999_999})
    response = await api.client.get(
        "/v1/auth/me", headers={"Authorization": f"Bearer {header}.{payload}."}
    )

    assert_problem(response, 401, "invalid_token")


async def test_garbage_bearer_token_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/auth/me", headers={"Authorization": "Bearer not.a.jwt"})

    assert_problem(response, 401, "invalid_token")


# ------------------------------------------------------------- refresh rotation


async def _refresh(client: httpx.AsyncClient, token: str) -> httpx.Response:
    return await client.post("/v1/auth/refresh", json={"refresh_token": token})


async def test_refresh_rotates_the_token(api: ApiDriver) -> None:
    user = await api.register()

    response = await _refresh(api.client, user.refresh_token)

    assert response.status_code == 200
    rotated = response.json()
    assert rotated["refresh_token"] != user.refresh_token
    me = await api.client.get(
        "/v1/auth/me", headers={"Authorization": f"Bearer {rotated['access_token']}"}
    )
    assert me.status_code == 200
    # The new token works, and can itself be rotated.
    assert (await _refresh(api.client, rotated["refresh_token"])).status_code == 200


async def test_reusing_a_rotated_refresh_token_revokes_the_whole_family(
    api: ApiDriver, db: AdminDb
) -> None:
    """Reuse detection: R1 -> R2 (legitimate). Whoever presents R1 again proves the
    token leaked, so R2 - possibly in the attacker's hands - must die with it."""
    user = await api.register()
    r1 = user.refresh_token
    r2 = (await _refresh(api.client, r1)).json()["refresh_token"]

    replay = await _refresh(api.client, r1)
    assert_problem(replay, 401, "invalid_refresh_token")

    assert_problem(await _refresh(api.client, r2), 401, "invalid_refresh_token")
    live_tokens = await db.scalar(
        "SELECT count(*) FROM refresh_tokens WHERE user_id = :u AND revoked_at IS NULL",
        u=uuid.UUID(user.id),
    )
    assert live_tokens == 0


async def test_reuse_detection_is_limited_to_the_affected_login_session(api: ApiDriver) -> None:
    """A second device (separate login = separate family) is not logged out."""
    phone = await api.register()
    laptop = await api.login(phone.email)

    rotated = (await _refresh(api.client, phone.refresh_token)).json()["refresh_token"]
    await _refresh(api.client, phone.refresh_token)  # reuse -> phone family revoked

    assert_problem(await _refresh(api.client, rotated), 401, "invalid_refresh_token")
    assert (await _refresh(api.client, laptop.refresh_token)).status_code == 200


async def test_concurrent_refresh_with_the_same_token_succeeds_exactly_once(
    api: ApiDriver, db: AdminDb
) -> None:
    """Race: ten requests present the same refresh token at once. The row lock
    serializes them, so exactly one rotates; the others arrive at a used token."""
    user = await api.register()

    responses = await asyncio.gather(*(_refresh(api.client, user.refresh_token) for _ in range(10)))

    statuses = sorted(r.status_code for r in responses)
    assert statuses == [200] + [401] * 9
    # Nine losers each presented a used token, which is indistinguishable from
    # theft: the family is revoked, including the winner's fresh token.
    winner = next(r for r in responses if r.status_code == 200).json()
    assert_problem(
        await _refresh(api.client, winner["refresh_token"]), 401, "invalid_refresh_token"
    )
    tokens = await db.column(
        "SELECT revoked_at IS NOT NULL FROM refresh_tokens WHERE user_id = :u",
        u=uuid.UUID(user.id),
    )
    assert tokens == [True, True]  # exactly one successor was ever issued


async def test_expired_refresh_token_is_rejected(api: ApiDriver, db: AdminDb) -> None:
    user = await api.register()
    await db.execute(
        "UPDATE refresh_tokens SET expires_at = now() - interval '1 second' WHERE user_id = :u",
        u=uuid.UUID(user.id),
    )

    assert_problem(await _refresh(api.client, user.refresh_token), 401, "invalid_refresh_token")


async def test_unknown_refresh_token_is_rejected(client: httpx.AsyncClient) -> None:
    assert_problem(await _refresh(client, "tbr_does-not-exist"), 401, "invalid_refresh_token")


async def test_logout_revokes_the_session(api: ApiDriver) -> None:
    user = await api.register()
    rotated = (await _refresh(api.client, user.refresh_token)).json()["refresh_token"]

    response = await api.client.post("/v1/auth/logout", json={"refresh_token": rotated})

    assert response.status_code == 204
    assert response.content == b""
    assert_problem(await _refresh(api.client, rotated), 401, "invalid_refresh_token")


async def test_logout_with_an_unknown_token_is_a_no_op(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/auth/logout", json={"refresh_token": "tbr_unknown"})

    assert response.status_code == 204
