"""Validated production decisions, independent of any media provider or SDK."""

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.intelligence.visual_contracts import BuiltinVisualSpec


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class VisualType(str, Enum):
    stock_video = "stock_video"
    ai_video = "ai_video"
    ai_image = "ai_image"
    local_asset = "local_asset"
    diagram = "diagram"
    chart = "chart"
    screenshot = "screenshot"
    text_card = "text_card"
    icon_composition = "icon_composition"


class ProductionBrief(Contract):
    subject: str = Field(min_length=1)
    language: str = ""
    supplied_script: str = ""
    instructions: str = ""
    aspect_ratio: str = "9:16"
    paragraph_number: int = Field(default=1, ge=1, le=10)
    target_scene_duration: float = Field(default=5, gt=0, le=300)
    selected_source: str = "pexels"
    supported_visual_types: list[VisualType] = Field(min_length=1)
    screenshot_count: int = Field(default=0, ge=0)
    quality_threshold: float = Field(default=8.5, ge=0, le=10)


class ScenePlan(Contract):
    scene_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    narration: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    target_duration: float = Field(gt=0, le=300)
    visual_intent: str = Field(min_length=1)
    preferred_visual_type: VisualType
    search_query: str = ""
    generation_prompt: str = ""
    on_screen_text: str = ""
    transition: str = "cut"
    continuity: str = ""
    builtin_visual: BuiltinVisualSpec | None = None


class ProductionPlan(Contract):
    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    scenes: list[ScenePlan] = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def unique_scene_ids(self):
        ids = [scene.scene_id for scene in self.scenes]
        if len(set(ids)) != len(ids):
            raise ValueError("Production scene IDs must be unique")
        return self

    @property
    def narration(self) -> str:
        return "\n\n".join(scene.narration.strip() for scene in self.scenes)


class SceneVisual(Contract):
    scene_id: str
    visual_intent: str = Field(min_length=1)
    preferred_visual_type: VisualType
    search_query: str = ""
    generation_prompt: str = ""
    on_screen_text: str = ""
    transition: str = "cut"
    continuity: str = ""
    builtin_visual: BuiltinVisualSpec | None = None


class VisualPlan(Contract):
    scenes: list[SceneVisual] = Field(min_length=1, max_length=80)


class EditedScene(Contract):
    scene_id: str
    narration: str = Field(min_length=1)


class EditedScript(Contract):
    scenes: list[EditedScene] = Field(min_length=1, max_length=80)


class ReviewIssue(Contract):
    scene_id: str | None = None
    severity: Literal["info", "warning", "error"]
    category: str = Field(min_length=1)
    description: str = Field(min_length=1)
    suggested_fix: str = ""


class ProductionReview(Contract):
    stage: Literal["plan_review", "material_review", "render_review"]
    score: float = Field(ge=0, le=10)
    approved: bool
    summary: str = Field(min_length=1)
    issues: list[ReviewIssue] = Field(default_factory=list)

    def passes(self, threshold: float) -> bool:
        return (
            self.approved
            and self.score >= threshold
            and not any(issue.severity == "error" for issue in self.issues)
        )


class RepairAction(Contract):
    stage: Literal["production_plan", "materials", "video"]
    scene_id: str | None = None
    action: Literal[
        "revise_scene",
        "replace_material",
        "change_query",
        "change_visual_type",
        "revise_visual",
        "rerender",
    ]
    reason: str = Field(min_length=1)
    replacement_scene: ScenePlan | None = None
    search_query: str | None = None
    generation_prompt: str | None = None
    visual_type: VisualType | None = None
    video_fit_mode: Literal["cover", "contain"] | None = None
    builtin_visual: BuiltinVisualSpec | None = None


class RepairProposal(Contract):
    actions: list[RepairAction] = Field(min_length=1, max_length=80)


class SceneMaterial(Contract):
    scene_id: str
    paths: list[str] = Field(min_length=1)
    source: str
    visual_type: VisualType
    target_duration: float = Field(gt=0)


class RepairRecord(Contract):
    stage: str
    pass_number: int = Field(ge=1)
    review_score: float
    actions: list[RepairAction]
