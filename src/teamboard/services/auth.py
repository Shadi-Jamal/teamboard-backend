"""Registration, login and refresh-token rotation."""

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from teamboard.config import Settings
from teamboard.db.base import utcnow
from teamboard.db.errors import violated_constraint
from teamboard.db.models import RefreshToken, User
from teamboard.db.rls import set_rls_context
from teamboard.domain import UserPrincipal
from teamboard.errors import AuthenticationError, ConflictError, NotFoundError, RateLimitedError
from teamboard.infra.rate_limit import UsageCounter
from teamboard.repositories.refresh_tokens import RefreshTokenRepository
from teamboard.repositories.users import UserRepository
from teamboard.security.passwords import PasswordService
from teamboard.security.tokens import AccessTokenService, hash_token, new_refresh_token

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IssuedTokens:
    access_token: str
    refresh_token: str
    expires_in: int


def normalize_email(email: str) -> str:
    return email.strip().lower()


def _invalid_credentials() -> AuthenticationError:
    # One message for "no such account" and "wrong password": no account enumeration.
    return AuthenticationError("Invalid email or password.", code="invalid_credentials")


def _invalid_refresh_token() -> AuthenticationError:
    # Deliberately does not say why (unknown, expired, revoked or reused).
    return AuthenticationError(
        "The refresh token is invalid or expired.", code="invalid_refresh_token"
    )


class AuthService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        passwords: PasswordService,
        access_tokens: AccessTokenService,
        counter: UsageCounter,
    ) -> None:
        self._session = session
        self._settings = settings
        self._passwords = passwords
        self._access_tokens = access_tokens
        self._counter = counter
        self._users = UserRepository(session)
        self._refresh_tokens = RefreshTokenRepository(session)

    async def register(self, email: str, password: str, full_name: str) -> User:
        user = User(
            id=uuid.uuid4(),
            email=normalize_email(email),
            password_hash=await self._passwords.hash(password),
            full_name=full_name.strip(),
        )
        await set_rls_context(self._session, user_id=user.id)
        self._users.add(user)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            if violated_constraint(exc) == "uq_users_email":
                raise ConflictError(
                    "An account with this email already exists.", code="email_already_registered"
                ) from exc
            raise
        await self._session.commit()
        return user

    async def login(self, email: str, password: str) -> IssuedTokens:
        email = normalize_email(email)
        failures_key = self._failures_key(email)
        if await self._counter.current(failures_key) >= self._settings.login_failure_limit:
            raise RateLimitedError(
                "Too many failed login attempts. Try again later.",
                code="too_many_login_attempts",
                retry_after=await self._counter.ttl(failures_key),
            )

        await set_rls_context(self._session, login_email=email)
        user = await self._users.get_by_email(email)
        if user is None:
            # Spend the same effort as a real verification (timing side channel).
            await self._passwords.verify_decoy(password)
            await self._record_failure(failures_key)
            raise _invalid_credentials()
        if not await self._passwords.verify(user.password_hash, password):
            await self._record_failure(failures_key)
            raise _invalid_credentials()
        if not user.is_active:
            raise AuthenticationError("This account is disabled.", code="account_disabled")

        await set_rls_context(self._session, user_id=user.id)
        if self._passwords.needs_rehash(user.password_hash):
            user.password_hash = await self._passwords.hash(password)
        tokens = self._issue(user.id, family_id=uuid.uuid4())
        await self._session.commit()
        await self._counter.reset(failures_key)
        return tokens

    async def refresh(self, raw_token: str) -> IssuedTokens:
        """Exchange a refresh token for a new pair (rotation).

        Each refresh token is valid exactly once. Presenting one that was already
        exchanged means two parties hold it - the legitimate client and a thief, in
        unknown order - so the entire token family is revoked and the user has to
        log in again.
        """
        token_hash = hash_token(raw_token)
        await set_rls_context(self._session, token_hash=token_hash)
        # Row lock: concurrent requests with the same token are processed one by one.
        token = await self._refresh_tokens.get_by_hash_for_update(token_hash)
        now = utcnow()
        if token is None or token.revoked_at is not None or token.expires_at <= now:
            raise _invalid_refresh_token()

        await set_rls_context(self._session, user_id=token.user_id)
        if token.used_at is not None:
            await self._refresh_tokens.revoke_family(token.family_id, now)
            # Commit before raising: the revocation must survive the error response.
            await self._session.commit()
            logger.warning(
                "refresh token reuse detected, token family revoked",
                extra={"user_id": str(token.user_id), "family_id": str(token.family_id)},
            )
            raise _invalid_refresh_token()

        user = await self._users.get(token.user_id)
        if user is None or not user.is_active:
            raise _invalid_refresh_token()

        token.used_at = now
        tokens = self._issue(user.id, family_id=token.family_id)
        await self._session.commit()
        return tokens

    async def logout(self, raw_token: str) -> None:
        """Revoke the token's whole family. Unknown tokens are ignored (idempotent)."""
        token_hash = hash_token(raw_token)
        await set_rls_context(self._session, token_hash=token_hash)
        token = await self._refresh_tokens.get_by_hash_for_update(token_hash)
        if token is None:
            return
        await set_rls_context(self._session, user_id=token.user_id)
        await self._refresh_tokens.revoke_family(token.family_id, utcnow())
        await self._session.commit()

    async def get_profile(self, principal: UserPrincipal) -> User:
        user = await self._users.get(principal.user_id)
        if user is None:
            raise NotFoundError("User not found.")
        return user

    def _issue(self, user_id: uuid.UUID, *, family_id: uuid.UUID) -> IssuedTokens:
        raw_refresh_token = new_refresh_token()
        self._refresh_tokens.add(
            RefreshToken(
                user_id=user_id,
                family_id=family_id,
                token_hash=hash_token(raw_refresh_token),
                expires_at=utcnow() + timedelta(seconds=self._settings.refresh_token_ttl_seconds),
            )
        )
        return IssuedTokens(
            access_token=self._access_tokens.issue(user_id),
            refresh_token=raw_refresh_token,
            expires_in=self._access_tokens.ttl_seconds,
        )

    async def _record_failure(self, failures_key: str) -> None:
        await self._counter.increment(
            failures_key,
            limit=self._settings.login_failure_limit,
            window_seconds=self._settings.login_failure_window_seconds,
        )

    @staticmethod
    def _failures_key(email: str) -> str:
        # Hashed so that email addresses do not end up as Redis key names.
        return f"login-failures:{hashlib.sha256(email.encode()).hexdigest()[:32]}"
