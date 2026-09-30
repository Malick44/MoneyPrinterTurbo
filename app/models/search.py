"""Portable contracts for evidence-backed material discovery."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


RightsStatus = Literal[
    "unknown",
    "allowed_internal",
    "allowed_export",
    "review_required",
    "blocked",
    "expired",
]
RequestedUse = Literal[
    "internal_review",
    "analysis",
    "generated_export",
    "publication",
    "clip_export",
    "archival",
]
REQUESTED_USES = frozenset(
    {
        "internal_review",
        "analysis",
        "generated_export",
        "publication",
        "clip_export",
        "archival",
    }
)
RIGHTS_STATUSES = frozenset(
    {
        "unknown",
        "allowed_internal",
        "allowed_export",
        "review_required",
        "blocked",
        "expired",
    }
)


class SearchError(ValueError):
    """A safe operational error suitable for an API or Streamlit response."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class DiscoverRequest(BaseModel):
    url: str = Field(min_length=1, max_length=4096)
    collection_id: str | None = None
    metadata: dict[str, Any] | None = None


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    filters: dict[str, Any] = Field(default_factory=dict)
    top_k: int = Field(default=20, ge=1, le=100)


class PolicyRequest(BaseModel):
    rights_status: RightsStatus
    permitted_use: str
    reason: str = Field(min_length=1, max_length=2000)
    reviewed_by: str = Field(min_length=1, max_length=200)
    expires_at: str | None = None


class ApprovalRequest(BaseModel):
    requested_use: RequestedUse = "internal_review"
    reviewed_by: str = "local-user"
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, gt=0)


class ClipRequest(ApprovalRequest):
    candidate_id: str


class CollectionRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    topic: str = Field(default="", max_length=2000)
    queries: list[str] = Field(default_factory=list)
