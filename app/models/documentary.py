"""Strict contracts for reviewed-evidence documentary narration."""

from __future__ import annotations

import math
from typing import Literal

from pydantic import Field

from app.models.case_workspace import StrictModel

DOCUMENTARY_MIN_MINUTES = 22
DOCUMENTARY_MAX_MINUTES = 28
DOCUMENTARY_DEFAULT_MINUTES = 25
DOCUMENTARY_PLANNING_WORDS_PER_MINUTE = 145


def normalize_documentary_minutes(value) -> float:
    """Use the current default for old creation preferences outside the range."""
    try:
        minutes = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(DOCUMENTARY_DEFAULT_MINUTES)
    if (
        math.isfinite(minutes)
        and DOCUMENTARY_MIN_MINUTES <= minutes <= DOCUMENTARY_MAX_MINUTES
    ):
        return minutes
    return float(DOCUMENTARY_DEFAULT_MINUTES)


class DocumentaryOptions(StrictModel):
    title: str = Field(min_length=1, max_length=500)
    target_minutes: float = Field(
        default=DOCUMENTARY_DEFAULT_MINUTES,
        ge=DOCUMENTARY_MIN_MINUTES,
        le=DOCUMENTARY_MAX_MINUTES,
        allow_inf_nan=False,
    )
    language: str = Field(default="English", min_length=1, max_length=100)
    claim_ids: list[str] = Field(default_factory=list, max_length=100)
    instructions: str = Field(default="", max_length=10000)
    stage: Literal["outline", "draft", "factual_review"] = "outline"
    document_id: str | None = Field(default=None, max_length=128)


class DocumentaryQuote(StrictModel):
    citation_id: str
    text: str = Field(min_length=1, max_length=6000)


class DocumentaryPassage(StrictModel):
    passage_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    text: str = Field(min_length=1, max_length=10000)
    claim_ids: list[str] = Field(min_length=1, max_length=30)
    citation_ids: list[str] = Field(min_length=1, max_length=60)
    quotes: list[DocumentaryQuote] = Field(default_factory=list, max_length=20)


class DocumentaryOutlineScene(StrictModel):
    scene_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(min_length=1, max_length=500)
    purpose: str = Field(min_length=1, max_length=3000)
    claim_ids: list[str] = Field(min_length=1, max_length=30)
    citation_ids: list[str] = Field(min_length=1, max_length=60)
    footage_queries: list[str] = Field(default_factory=list, max_length=20)
    evidence_gaps: list[str] = Field(default_factory=list, max_length=20)


class DocumentaryScene(StrictModel):
    scene_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(min_length=1, max_length=500)
    passages: list[DocumentaryPassage] = Field(min_length=1, max_length=30)
    footage_queries: list[str] = Field(default_factory=list, max_length=20)
    evidence_gaps: list[str] = Field(default_factory=list, max_length=20)


class DocumentaryOutlineChapter(StrictModel):
    chapter_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(min_length=1, max_length=500)
    scenes: list[DocumentaryOutlineScene] = Field(min_length=1, max_length=40)


class DocumentaryChapter(StrictModel):
    chapter_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(min_length=1, max_length=500)
    scenes: list[DocumentaryScene] = Field(min_length=1, max_length=40)


class DocumentaryOutline(StrictModel):
    chapters: list[DocumentaryOutlineChapter] = Field(min_length=1, max_length=20)


class DocumentaryDraft(StrictModel):
    chapters: list[DocumentaryChapter] = Field(min_length=1, max_length=20)


class DocumentaryPassageReview(StrictModel):
    passage_id: str
    status: Literal["supported", "contradicted", "insufficient"]
    reason: str = Field(min_length=1, max_length=3000)
    citation_ids: list[str] = Field(default_factory=list, max_length=60)


class DocumentaryFactualReview(StrictModel):
    passages: list[DocumentaryPassageReview] = Field(min_length=1, max_length=500)
    notes: list[str] = Field(default_factory=list, max_length=30)
