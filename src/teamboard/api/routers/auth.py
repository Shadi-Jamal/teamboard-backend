from fastapi import APIRouter, Depends, status

from teamboard.api.deps import AuthServiceDep, CurrentUser, auth_rate_limit
from teamboard.api.problems import problem_responses
from teamboard.schemas.auth import (
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenPair,
    UserRead,
)

router = APIRouter(prefix="/v1/auth", tags=["Auth"])

_throttled = [Depends(auth_rate_limit)]


@router.post(
    "/register",
    summary="Register a user account",
    status_code=status.HTTP_201_CREATED,
    response_model=UserRead,
    dependencies=_throttled,
    responses=problem_responses(409, 429),
)
async def register(payload: RegisterRequest, auth: AuthServiceDep) -> UserRead:
    """Create a user account. The account belongs to no tenant yet: create one with
    `POST /v1/tenants` or accept an invitation."""
    user = await auth.register(payload.email, payload.password, payload.full_name)
    return UserRead.model_validate(user)


@router.post(
    "/login",
    summary="Log in",
    response_model=TokenPair,
    dependencies=_throttled,
    responses=problem_responses(401, 429),
)
async def login(payload: LoginRequest, auth: AuthServiceDep) -> TokenPair:
    """Exchange email and password for a short-lived access token and a refresh token."""
    tokens = await auth.login(payload.email, payload.password)
    return TokenPair(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in,
    )


@router.post(
    "/refresh",
    summary="Rotate the refresh token",
    response_model=TokenPair,
    dependencies=_throttled,
    responses=problem_responses(401, 429),
)
async def refresh(payload: RefreshRequest, auth: AuthServiceDep) -> TokenPair:
    """Exchange a refresh token for a new access/refresh pair.

    A refresh token can be used **once**. Using it a second time is treated as
    token theft: every token descended from the same login is revoked.
    """
    tokens = await auth.refresh(payload.refresh_token)
    return TokenPair(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in,
    )


@router.post(
    "/logout",
    summary="Log out",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=_throttled,
    responses=problem_responses(429),
)
async def logout(payload: RefreshRequest, auth: AuthServiceDep) -> None:
    """Revoke the refresh token and all tokens of the same login session."""
    await auth.logout(payload.refresh_token)


@router.get(
    "/me",
    summary="Get the current user",
    response_model=UserRead,
    responses=problem_responses(401, 403),
)
async def get_me(user: CurrentUser, auth: AuthServiceDep) -> UserRead:
    return UserRead.model_validate(await auth.get_profile(user))
