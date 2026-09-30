"""Typed locators and contracts for a local, versioned case workspace."""

from __future__ import annotations

import math
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator


AssetKind = Literal[
    "document",
    "audio",
    "video",
    "image",
    "map",
    "script",
    "transcript",
    "other",
    "reference",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegionModel(StrictModel):
    bbox: tuple[float, float, float, float] | None = None

    @model_validator(mode="after")
    def check_bbox(self):
        if self.bbox is not None:
            x0, y0, x1, y1 = self.bbox
            if not all(math.isfinite(v) for v in self.bbox) or not (
                0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1
            ):
                raise ValueError(
                    "bbox uses normalized top-left coordinates within [0,1]"
                )
        return self


class PageLocator(RegionModel):
    kind: Literal["page"] = "page"
    page_index: int = Field(ge=0, strict=True)
    page_label: str | None = None
    text_start: int | None = Field(default=None, ge=0, strict=True)
    text_end: int | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def check_text(self):
        if (self.text_start is None) != (self.text_end is None) or (
            self.text_start is not None and self.text_end <= self.text_start
        ):
            raise ValueError("Supply an increasing text span")
        return self


class TimeLocator(StrictModel):
    kind: Literal["time"] = "time"
    start_ms: int = Field(ge=0, strict=True)
    end_ms: int = Field(gt=0, strict=True)
    speaker: str | None = None
    channel: str | int | None = None

    @model_validator(mode="after")
    def check_time(self):
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be after start_ms")
        return self


class WordLocator(StrictModel):
    kind: Literal["word"] = "word"
    transcript_artifact_id: str
    word_start_index: int = Field(ge=0, strict=True)
    word_end_index: int = Field(ge=0, strict=True)
    start_ms: int | None = Field(default=None, ge=0, strict=True)
    end_ms: int | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def check_words(self):
        if self.word_end_index <= self.word_start_index:
            raise ValueError("Word span must be increasing with an exclusive end index")
        if (self.start_ms is None) != (self.end_ms is None) or (
            self.start_ms is not None and self.end_ms < self.start_ms
        ):
            raise ValueError("Word timing is either unknown or an increasing range")
        return self


class ImageLocator(RegionModel):
    kind: Literal["image"] = "image"
    label: str | None = None


class ScriptLocator(StrictModel):
    kind: Literal["script"] = "script"
    char_start: int = Field(ge=0, strict=True)
    char_end: int = Field(gt=0, strict=True)
    scene_id: str | None = None

    @model_validator(mode="after")
    def check_script(self):
        if self.char_end <= self.char_start:
            raise ValueError("Script span must be increasing")
        return self


class MetadataLocator(StrictModel):
    kind: Literal["metadata"] = "metadata"
    field: str | None = None


Locator = Annotated[
    PageLocator
    | TimeLocator
    | WordLocator
    | ImageLocator
    | ScriptLocator
    | MetadataLocator,
    Field(discriminator="kind"),
]
LOCATOR_ADAPTER = TypeAdapter(Locator)


class Citation(StrictModel):
    unit_id: str | None = None
    asset_id: str | None = None
    locator: Locator | None = None
    quote: str | None = Field(default=None, max_length=50000)
    relation: Literal["supports", "contradicts", "mentions"] = "supports"

    @model_validator(mode="after")
    def check_reference(self):
        if not self.unit_id and (not self.asset_id or not self.locator):
            raise ValueError("A citation needs unit_id or asset_id and a typed locator")
        return self


class Scene(StrictModel):
    scene_id: str = Field(min_length=1, max_length=128)
    asset_id: str
    source_start_ms: int | None = Field(default=None, ge=0, strict=True)
    source_end_ms: int | None = Field(default=None, gt=0, strict=True)
    duration_ms: int = Field(gt=0, le=300000, strict=True)
    role: Literal["broll", "original_sound", "still", "document", "map"]
    locator: Locator | None = None
    citations: list[str] = Field(default_factory=list, max_length=100)
    claim_ids: list[str] = Field(default_factory=list, max_length=100)
    speed: float = Field(default=1, ge=0.25, le=4, allow_inf_nan=False)
    volume: float = Field(default=1, ge=0, le=4, allow_inf_nan=False)
    visual_asset_id: str | None = None
    visual_locator: Locator | None = None
    narration: str = Field(default="", max_length=10000)

    @model_validator(mode="after")
    def check_scene(self):
        if (self.source_start_ms is None) != (self.source_end_ms is None) or (
            self.source_start_ms is not None
            and self.source_end_ms <= self.source_start_ms
        ):
            raise ValueError("Supply a valid source time range")
        return self


class StoryboardRecord(StrictModel):
    id: str | None = None
    title: str = Field(min_length=1, max_length=1000)
    script: str = Field(default="", max_length=1000000)
    narration_asset_id: str | None = None
    scenes: list[Scene] = Field(default_factory=list, max_length=80)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CaseCreate(StrictModel):
    name: str = Field(min_length=1, max_length=500)
    topic: str = Field(default="", max_length=5000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class FolderImport(StrictModel):
    path: str = Field(min_length=1, max_length=4096)
    category: str | None = Field(default=None, max_length=200)
    index: bool = False
    max_files: int = Field(default=500, ge=1, le=10000)
    max_total_bytes: int = Field(default=20000000000, gt=0)


class SourceLink(StrictModel):
    source_id: str
    category: str = "Video"
    asset_kind: AssetKind = "video"


class CaseQuery(StrictModel):
    query: str = Field(min_length=1, max_length=2000)
    filters: dict = Field(default_factory=dict)
    top_k: int = Field(default=20, ge=1, le=100)


class RecordBody(StrictModel):
    record: dict[str, Any]


class RenderBody(StrictModel):
    requested_use: Literal["generated_export", "publication", "internal_review"] = (
        "generated_export"
    )
