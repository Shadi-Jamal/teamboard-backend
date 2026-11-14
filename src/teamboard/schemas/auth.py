import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field

from teamboard.schemas.common import Schema

# The upper bound stops oversized inputs from turning Argon2 into a DoS vector.
PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 128


class RegisterRequest(BaseModel):
    email: EmailStr = Field(max_length=320, examples=["ada@example.com"])
    password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)
    full_name: str = Field(min_length=1, max_length=120, examples=["Ada Lovelace"])


class LoginRequest(BaseModel):
    email: EmailStr = Field(max_length=320, examples=["ada@example.com"])
    password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=512)


class TokenPair(BaseModel):
    access_token: str = Field(description="JWT. Send as `Authorization: Bearer <token>`.")
    refresh_token: str = Field(
        description="Single-use. Exchange it at `/v1/auth/refresh` for a new pair."
    )
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 - OAuth token type, not a secret
    expires_in: int = Field(description="Lifetime of the access token in seconds.")


class UserRead(Schema):
    id: uuid.UUID
    email: str
    full_name: str
    created_at: datetime
