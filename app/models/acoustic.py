"""Word-anchored cinematic sound design contracts."""

from typing import Literal

from pydantic import Field

from app.models.case_workspace import StrictModel


SoundCategory = Literal[
    "impact", "riser", "drone", "pulse", "ambience", "transition", "foley", "sting"
]
SOUND_CATEGORIES = (
    "impact",
    "riser",
    "drone",
    "pulse",
    "ambience",
    "transition",
    "foley",
    "sting",
)


class AcousticOptions(StrictModel):
    narration_asset_id: str
    script_asset_id: str
    transcript_artifact_id: str | None = None
    title: str = Field(default="Cinematic narration mix", min_length=1, max_length=500)
    style: str = Field(default="Restrained documentary sound design", max_length=2000)
    max_cues: int = Field(default=20, ge=1, le=60, strict=True)
    auto_align: bool = False


class TensionCue(StrictModel):
    cue_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    anchor_word_index: int = Field(ge=0, strict=True)
    anchor: Literal["start", "end"] = "start"
    offset_ms: int = Field(default=0, ge=-5000, le=5000, strict=True)
    category: SoundCategory
    tension: float = Field(ge=0, le=1, allow_inf_nan=False)
    reason: str = Field(min_length=1, max_length=2000)
    query: str = Field(min_length=1, max_length=1000)
    duration_ms: int = Field(default=2000, ge=20, le=15000, strict=True)
    gain_db: float = Field(default=-18, ge=-40, le=-4, allow_inf_nan=False)


class TensionAnalysis(StrictModel):
    cues: list[TensionCue] = Field(default_factory=list, max_length=60)
    notes: list[str] = Field(default_factory=list, max_length=20)


class CueEdit(TensionCue):
    asset_id: str | None = None
    enabled: bool = True
    fade_in_ms: int = Field(default=20, ge=0, le=5000, strict=True)
    fade_out_ms: int = Field(default=100, ge=0, le=5000, strict=True)


class MixOptions(StrictModel):
    narration_gain_db: float = Field(default=0, ge=-12, le=6, allow_inf_nan=False)
    duck_db: float = Field(default=-8, ge=-24, le=0, allow_inf_nan=False)
    headroom_db: float = Field(default=-1, ge=-6, le=-0.1, allow_inf_nan=False)


class AcousticPlanEdit(StrictModel):
    cues: list[CueEdit] = Field(default_factory=list, max_length=60)
    mix: MixOptions = Field(default_factory=MixOptions)


class SoundRegistration(StrictModel):
    asset_id: str
    tags: list[str] = Field(default_factory=list, max_length=40)
    description: str = Field(default="", max_length=2000)
    category: SoundCategory = "impact"
