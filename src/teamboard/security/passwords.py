"""Password hashing with Argon2id."""

import asyncio

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from teamboard.config import Settings


class PasswordService:
    """Hashes and verifies passwords off the event loop.

    Argon2 is deliberately expensive (tens of milliseconds and megabytes), so every
    call runs in a worker thread instead of blocking other requests.
    """

    def __init__(self, settings: Settings) -> None:
        self._hasher = PasswordHasher(
            time_cost=settings.argon2_time_cost,
            memory_cost=settings.argon2_memory_cost_kib,
            parallelism=settings.argon2_parallelism,
        )
        # Verified against when the account does not exist, so that a login attempt
        # costs the same whether or not the email is registered.
        self._decoy_hash = self._hasher.hash("teamboard-decoy-password")

    async def hash(self, password: str) -> str:
        return await asyncio.to_thread(self._hasher.hash, password)

    async def verify(self, password_hash: str, password: str) -> bool:
        return await asyncio.to_thread(self._verify, password_hash, password)

    async def verify_decoy(self, password: str) -> None:
        await asyncio.to_thread(self._verify, self._decoy_hash, password)

    def needs_rehash(self, password_hash: str) -> bool:
        """True if the hash was created with weaker parameters than configured now."""
        return self._hasher.check_needs_rehash(password_hash)

    def _verify(self, password_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(password_hash, password)
        except (VerificationError, InvalidHashError):
            return False
