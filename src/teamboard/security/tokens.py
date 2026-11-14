"""Token primitives: JWT access tokens and opaque secrets.

Opaque secrets (refresh tokens, API keys, invitation tokens) are 256 bits of
randomness. Only their SHA-256 digest is stored: a fast hash is appropriate here
because, unlike a password, the input has too much entropy to brute-force, and a
deterministic digest lets the row be found by an indexed equality lookup.
"""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import jwt

from teamboard.config import Settings
from teamboard.errors import AuthenticationError

_ALGORITHM = "HS256"

REFRESH_TOKEN_PREFIX = "tbr_"  # noqa: S105 - a public prefix, not a secret
API_KEY_PREFIX = "tbk_"
INVITATION_TOKEN_PREFIX = "tbi_"  # noqa: S105

# Number of leading characters of an API key kept in clear text for identification.
API_KEY_DISPLAY_LENGTH = 12


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def new_refresh_token() -> str:
    return REFRESH_TOKEN_PREFIX + secrets.token_urlsafe(32)


def new_api_key() -> str:
    return API_KEY_PREFIX + secrets.token_urlsafe(32)


def new_invitation_token() -> str:
    return INVITATION_TOKEN_PREFIX + secrets.token_urlsafe(32)


class AccessTokenService:
    """Issues and verifies short-lived, stateless JWT access tokens."""

    def __init__(self, settings: Settings) -> None:
        self._secret = settings.jwt_secret.get_secret_value()
        self._issuer = settings.jwt_issuer
        self._ttl = timedelta(seconds=settings.access_token_ttl_seconds)

    @property
    def ttl_seconds(self) -> int:
        return int(self._ttl.total_seconds())

    def issue(self, user_id: uuid.UUID) -> str:
        now = datetime.now(UTC)
        claims = {
            "iss": self._issuer,
            "sub": str(user_id),
            "iat": now,
            "exp": now + self._ttl,
            "jti": uuid.uuid4().hex,
            "typ": "access",
        }
        return jwt.encode(claims, self._secret, algorithm=_ALGORITHM)

    def verify(self, token: str) -> uuid.UUID:
        """Return the user ID the token was issued for, or raise AuthenticationError."""
        try:
            claims = jwt.decode(
                token,
                self._secret,
                # Pinning the algorithm list prevents "alg" confusion attacks.
                algorithms=[_ALGORITHM],
                issuer=self._issuer,
                options={"require": ["exp", "iat", "sub", "iss"]},
            )
            if claims.get("typ") != "access":
                raise jwt.InvalidTokenError("not an access token")
            return uuid.UUID(claims["sub"])
        except (jwt.InvalidTokenError, ValueError) as exc:
            raise AuthenticationError(
                "The access token is invalid or expired.", code="invalid_token"
            ) from exc
