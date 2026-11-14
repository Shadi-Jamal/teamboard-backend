"""Unit tests for pure building blocks (no database, no HTTP)."""

import hashlib
import uuid
from typing import Any

import pytest
import uvicorn
from pydantic import ValidationError

from teamboard import __main__ as api_entrypoint
from teamboard.config import DEV_JWT_SECRET, Settings, get_settings
from teamboard.domain import (
    API_KEY_SCOPES,
    PLAN_LIMITS,
    ROLE_PERMISSIONS,
    Actor,
    ActorType,
    Permission,
    Plan,
    Role,
)
from teamboard.errors import AppError, AuthenticationError, RateLimitedError
from teamboard.security.passwords import PasswordService
from teamboard.security.tokens import (
    AccessTokenService,
    hash_token,
    new_api_key,
    new_invitation_token,
    new_refresh_token,
)
from teamboard.worker import __main__ as worker_entrypoint

BASE: dict[str, Any] = {
    "_env_file": None,
    "database_url": "postgresql+asyncpg://app:app@localhost/teamboard",
    "redis_url": "redis://localhost:6379/0",
    "jwt_secret": "s" * 40,
}


# --------------------------------------------------------------------- settings


def test_settings_have_safe_defaults() -> None:
    settings = Settings(**BASE)

    assert settings.environment == "development"
    assert settings.access_token_ttl_seconds == 900
    assert settings.refresh_token_ttl_seconds == 30 * 24 * 3600
    assert (settings.argon2_time_cost, settings.argon2_memory_cost_kib) == (3, 65_536)
    assert "s" * 40 not in repr(settings)  # secrets are masked in logs and tracebacks


def test_short_jwt_secret_is_rejected() -> None:
    with pytest.raises(ValidationError, match="at least 32 characters"):
        Settings(**{**BASE, "jwt_secret": "too-short"})


def test_development_placeholder_secret_is_refused_in_production() -> None:
    Settings(**{**BASE, "jwt_secret": DEV_JWT_SECRET})  # fine for local development

    with pytest.raises(ValidationError, match="development placeholder"):
        Settings(**{**BASE, "jwt_secret": DEV_JWT_SECRET, "environment": "production"})


@pytest.mark.parametrize("missing", ["database_url", "redis_url", "jwt_secret"])
def test_required_settings_have_no_fallback(missing: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(missing.upper(), raising=False)

    with pytest.raises(ValidationError, match=missing):
        Settings(**{k: v for k, v in BASE.items() if k != missing})


def test_settings_are_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", BASE["database_url"])
    monkeypatch.setenv("REDIS_URL", "redis://cache:6379/1")
    monkeypatch.setenv("JWT_SECRET", "e" * 48)
    monkeypatch.setenv("ACCESS_TOKEN_TTL_SECONDS", "60")
    monkeypatch.chdir("/")  # no .env file here
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.redis_url == "redis://cache:6379/1"
        assert settings.access_token_ttl_seconds == 60
        assert get_settings() is settings
    finally:
        get_settings.cache_clear()


# ----------------------------------------------------------------------- tokens


def test_opaque_tokens_are_random_prefixed_and_hashed_with_sha256() -> None:
    tokens = [new_refresh_token(), new_api_key(), new_invitation_token()]

    assert [t[:4] for t in tokens] == ["tbr_", "tbk_", "tbi_"]
    assert all(len(t) >= 40 for t in tokens)
    assert new_api_key() != new_api_key()
    assert hash_token("abc") == hashlib.sha256(b"abc").hexdigest()
    assert len(hash_token(tokens[0])) == 64


def test_access_token_round_trip_and_tampering() -> None:
    service = AccessTokenService(Settings(**BASE))
    user_id = uuid.uuid4()
    token = service.issue(user_id)

    assert service.verify(token) == user_id
    assert service.ttl_seconds == 900
    header, payload, signature = token.split(".")
    with pytest.raises(AuthenticationError):
        service.verify(f"{header}.{payload}.{signature[:-2]}xx")
    other = AccessTokenService(Settings(**{**BASE, "jwt_secret": "o" * 40}))
    with pytest.raises(AuthenticationError):
        other.verify(token)


async def test_password_hashing() -> None:
    cheap: dict[str, Any] = {
        "argon2_time_cost": 1,
        "argon2_memory_cost_kib": 8,
        "argon2_parallelism": 1,
    }
    passwords = PasswordService(Settings(**BASE, **cheap))

    hashed = await passwords.hash("correct horse")

    assert hashed.startswith("$argon2id$")
    assert hashed != await passwords.hash("correct horse")  # salted
    assert await passwords.verify(hashed, "correct horse") is True
    assert await passwords.verify(hashed, "wrong horse") is False
    assert await passwords.verify("not-a-hash", "anything") is False
    assert passwords.needs_rehash(hashed) is False
    stronger = PasswordService(Settings(**BASE, **{**cheap, "argon2_time_cost": 2}))
    assert stronger.needs_rehash(hashed) is True
    await passwords.verify_decoy("anything")  # must not raise


# ----------------------------------------------------------------------- domain


def test_roles_form_a_strict_hierarchy() -> None:
    ordered = [Role.VIEWER, Role.MEMBER, Role.ADMIN, Role.OWNER]

    assert [role.rank for role in ordered] == [1, 2, 3, 4]
    assert ROLE_PERMISSIONS[Role.VIEWER] < ROLE_PERMISSIONS[Role.MEMBER]
    assert ROLE_PERMISSIONS[Role.MEMBER] < ROLE_PERMISSIONS[Role.ADMIN]
    assert ROLE_PERMISSIONS[Role.ADMIN] == ROLE_PERMISSIONS[Role.OWNER] == frozenset(Permission)
    assert not any(
        "write" in p or "manage" in p or "delete" in p for p in ROLE_PERMISSIONS[Role.VIEWER]
    )


def test_api_keys_can_never_hold_administrative_permissions() -> None:
    forbidden = {
        Permission.MEMBERS_MANAGE,
        Permission.INVITATIONS_MANAGE,
        Permission.API_KEYS_MANAGE,
        Permission.TENANT_WRITE,
    }

    assert frozenset(Permission) - forbidden == API_KEY_SCOPES


def test_plans_grow_monotonically() -> None:
    free, pro, enterprise = (PLAN_LIMITS[plan] for plan in (Plan.FREE, Plan.PRO, Plan.ENTERPRISE))

    for field in ("requests_per_minute", "max_projects", "max_members", "max_api_keys"):
        assert getattr(free, field) < getattr(pro, field) < getattr(enterprise, field)


def test_actor_and_error_representations() -> None:
    user = uuid.uuid4()

    assert str(Actor(ActorType.USER, user)) == f"user:{user}"
    assert str(Actor(ActorType.SYSTEM, None)) == "system"
    assert RateLimitedError("slow down", retry_after=0).headers == {"Retry-After": "1"}
    assert AuthenticationError().headers == {"WWW-Authenticate": "Bearer"}
    assert str(AppError()) == "Internal Server Error"
    assert AppError("x", code="custom").code == "custom"


# ------------------------------------------------------------------ entrypoints


def test_api_entrypoint_starts_uvicorn_with_the_app_factory(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: dict[str, Any] = {}
    monkeypatch.setattr(api_entrypoint, "get_settings", lambda: settings)
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: started.update(app=app, **options))

    api_entrypoint.main()

    assert started["app"] == "teamboard.main:create_app"
    assert started["factory"] is True
    assert (started["host"], started["port"]) == (settings.host, settings.port)
    # Logging is owned by the application (JSON), not by uvicorn's defaults.
    assert (started["log_config"], started["access_log"]) == (None, False)


def test_worker_entrypoint_runs_the_export_worker(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[Any] = []

    class FakeWorker:
        def run(self) -> None:
            created.append("ran")

    def fake_create_worker(passed: Settings) -> FakeWorker:
        created.append(passed)
        return FakeWorker()

    monkeypatch.setattr(worker_entrypoint, "get_settings", lambda: settings)
    monkeypatch.setattr(worker_entrypoint, "configure_logging", lambda level: None)
    monkeypatch.setattr(worker_entrypoint, "create_worker", fake_create_worker)

    worker_entrypoint.main()

    assert created == [settings, "ran"]
