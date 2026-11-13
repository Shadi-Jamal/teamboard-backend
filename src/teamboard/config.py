"""Application settings, loaded from environment variables (12-factor)."""

from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Placeholder used by docker-compose for local development only. It is public by
# definition, so the application refuses to start with it in production.
DEV_JWT_SECRET = "dev-only-jwt-secret-never-use-in-production"  # noqa: S105

MIN_JWT_SECRET_LENGTH = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    docs_enabled: bool = True

    # HTTP server
    host: str = "0.0.0.0"  # noqa: S104 - the service runs inside a container
    port: int = 8000
    forwarded_allow_ips: str = "127.0.0.1"

    # PostgreSQL. The runtime role must be subject to row-level security; migrations
    # use a separate owner role configured through MIGRATION_DATABASE_URL.
    database_url: str
    db_pool_size: int = Field(default=10, ge=1)
    db_max_overflow: int = Field(default=10, ge=0)
    db_statement_timeout_ms: int = Field(default=30_000, ge=0)

    redis_url: str

    # Authentication
    jwt_secret: SecretStr
    jwt_issuer: str = "teamboard"
    access_token_ttl_seconds: int = Field(default=15 * 60, gt=0)
    refresh_token_ttl_seconds: int = Field(default=30 * 24 * 3600, gt=0)
    invitation_ttl_seconds: int = Field(default=7 * 24 * 3600, gt=0)

    # Argon2id cost parameters (defaults follow the RFC 9106 low-memory profile).
    argon2_time_cost: int = Field(default=3, ge=1)
    argon2_memory_cost_kib: int = Field(default=65_536, ge=8)
    argon2_parallelism: int = Field(default=4, ge=1)

    # Abuse protection for unauthenticated endpoints
    auth_rate_limit_per_minute: int = Field(default=30, ge=1)
    login_failure_limit: int = Field(default=10, ge=1)
    login_failure_window_seconds: int = Field(default=15 * 60, gt=0)

    idempotency_ttl_seconds: int = Field(default=24 * 3600, gt=0)

    # Background export job
    export_max_attempts: int = Field(default=4, ge=1)
    export_retry_base_seconds: float = Field(default=5.0, gt=0)

    @model_validator(mode="after")
    def _validate_secrets(self) -> Self:
        secret = self.jwt_secret.get_secret_value()
        if len(secret) < MIN_JWT_SECRET_LENGTH:
            raise ValueError(f"JWT_SECRET must be at least {MIN_JWT_SECRET_LENGTH} characters")
        if self.environment == "production" and secret == DEV_JWT_SECRET:
            raise ValueError("JWT_SECRET must not be the development placeholder in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
