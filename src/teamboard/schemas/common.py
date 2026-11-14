"""Schemas shared across the API."""

from pydantic import BaseModel, ConfigDict, Field


class Schema(BaseModel):
    """Base for response models built from ORM objects."""

    model_config = ConfigDict(from_attributes=True)


class PageResponse[T](BaseModel):
    items: list[T]
    next_cursor: str | None = Field(
        description="Pass as `cursor` to fetch the next page. `null` on the last page."
    )


class FieldError(BaseModel):
    field: str = Field(description="Location of the invalid value, e.g. `body.email`.")
    message: str
    type: str


class Problem(BaseModel):
    """RFC 7807 problem details (`application/problem+json`)."""

    type: str = Field(description="URI identifying the problem type.")
    title: str = Field(description="Short, human-readable summary of the problem type.")
    status: int = Field(description="HTTP status code.")
    detail: str | None = Field(default=None, description="Explanation specific to this occurrence.")
    instance: str | None = Field(default=None, description="Path of the request that failed.")
    code: str = Field(description="Stable machine-readable error code.")
    request_id: str | None = Field(
        default=None, description="Correlates the response with server logs."
    )
    errors: list[FieldError] | None = Field(
        default=None, description="Per-field details for validation failures."
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "type": "urn:teamboard:problem:not_found",
                "title": "Not Found",
                "status": 404,
                "detail": "Project not found.",
                "instance": "/v1/projects/7b0c5d4e-3a53-4c0f-9f2b-0e9d7c1a2b3c",
                "code": "not_found",
                "request_id": "0c3f1a6e5b7d4e8f9a1b2c3d4e5f6a7b",
            }
        }
    )
