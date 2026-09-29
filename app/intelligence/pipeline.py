"""Bounded production decisions around MPT's existing deterministic stages."""

from __future__ import annotations

import math
import re
import shutil
from pathlib import Path

from pydantic import BaseModel

from app.intelligence.contracts import (
    ProductionBrief,
    ProductionReview,
    RepairRecord,
    SceneMaterial,
    SceneVisual,
    VisualPlan,
    VisualType,
)
from app.intelligence.roles import (
    MaterialReviewer,
    PlanReviewer,
    ProductionDirector,
    RenderReviewer,
    RepairPlanner,
    ScriptEditor,
    VisualDirector,
)
from app.intelligence.runtime import CodexRuntime, IntelligenceError, redact_secrets
from app.intelligence.visual_contracts import BUILTIN_VISUAL_TYPES
from app.models.schema import VideoAspect, VideoConcatMode
from app.services import task_artifacts
from app.utils import utils


SOURCE_VISUAL_TYPES = {
    "pexels": VisualType.stock_video,
    "pixabay": VisualType.stock_video,
    "coverr": VisualType.stock_video,
    "wavespeed": VisualType.ai_video,
    "volcengine_seedance": VisualType.ai_video,
    "ofox": VisualType.ai_video,
    "metaso_minimax": VisualType.ai_video,
    "openai_image": VisualType.ai_image,
    "local": VisualType.local_asset,
}


def redact_artifact(value):
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", warnings=False)
    if isinstance(value, dict):
        return {key: redact_artifact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_artifact(item) for item in value]
    return redact_secrets(value) if isinstance(value, str) else value


class ProductionIntelligence:
    """One task's decisions and review artifacts; never changes global provider settings."""

    def __init__(self, task_id, params, *, runtime=None, task_dir=None, on_stage=None):
        self.task_id = task_id
        self.params = params.model_copy(deep=True)
        self.task_dir = Path(task_dir or utils.task_dir(task_id))
        self.task_dir.mkdir(parents=True, exist_ok=True)
        self.runtime = runtime or CodexRuntime(
            model=getattr(params, "codex_model_name", "") or None,
            reasoning_effort=getattr(params, "codex_reasoning_effort", "medium"),
            working_directory=str(self.task_dir),
        )
        self.threshold = float(getattr(params, "codex_quality_threshold", 8.5))
        self.max_passes = int(getattr(params, "codex_max_repair_passes", 2))
        # Defense in depth for programmatic callers that bypass request validation.
        if not 0 <= self.max_passes <= 10 or not 0 <= self.threshold <= 10:
            raise IntelligenceError(
                "production_plan",
                "Quality settings require 0–10 repair passes and a 0–10 threshold.",
            )
        self.on_stage = on_stage or (lambda stage: None)
        self.history = []
        self.materials = []
        self.timeline = []
        self.plan = None
        self.local_candidates = []
        self.screenshot_paths = []
        self.scene_fit_modes = {}
        self.render_backups = []
        self._plan_repair_passes = 0
        self._plan_review_count = 0
        self._write("repair-history.json", self.history)
        for filename, setting in (
            ("production-review.json", "codex_review_enabled"),
            ("material-review.json", "codex_material_review_enabled"),
            ("render-review.json", "codex_render_review_enabled"),
        ):
            self._write(
                filename,
                {"status": "pending" if getattr(params, setting, True) else "disabled"},
            )

    def _write(self, filename, value):
        task_artifacts._write_json_atomic(
            self.task_dir / filename, redact_artifact(value)
        )

    def _save_plan(self):
        self._write("production-plan.json", self.plan)
        self._write(
            "visual-plan.json",
            VisualPlan(
                scenes=[
                    SceneVisual.model_validate(
                        {
                            key: value
                            for key, value in scene.model_dump().items()
                            if key in SceneVisual.model_fields
                        }
                    )
                    for scene in self.plan.scenes
                ]
            ),
        )

    def _run(self, role, **context):
        self.on_stage(role.stage)
        return role(self.runtime).run(**context)

    def _validate_ids(self, result, stage):
        expected = [scene.scene_id for scene in self.plan.scenes]
        if [scene.scene_id for scene in result.scenes] != expected:
            raise IntelligenceError(
                stage,
                "Codex changed or reordered scene IDs; retry with stable scene IDs.",
            )

    def _validate_execution(self):
        for scene in self.plan.scenes:
            if scene.preferred_visual_type not in self.brief.supported_visual_types:
                raise IntelligenceError(
                    "visual_plan",
                    f"Scene {scene.scene_id} requests {scene.preferred_visual_type.value}, which source "
                    f"'{self.brief.selected_source}' cannot execute. Select a compatible source or revise the plan. "
                    "Select Built-in visuals for diagrams, charts, screenshot images, text cards and icon compositions.",
                )
            if scene.preferred_visual_type.value in BUILTIN_VISUAL_TYPES:
                try:
                    if scene.builtin_visual is None:
                        raise ValueError("A built-in visual specification is required.")
                    scene.builtin_visual.validate_for(scene.preferred_visual_type.value)
                    if scene.preferred_visual_type == VisualType.screenshot and (
                        scene.builtin_visual.screenshot_index is None
                        or scene.builtin_visual.screenshot_index
                        >= len(self.screenshot_paths)
                    ):
                        raise ValueError(
                            "Screenshot index must refer to an uploaded image."
                        )
                except ValueError as exc:
                    raise IntelligenceError(
                        "visual_plan", f"Scene {scene.scene_id}: {exc}"
                    ) from exc
            if scene.transition not in {"cut", "fade"}:
                raise IntelligenceError(
                    "visual_plan",
                    f"Scene {scene.scene_id} uses an unsupported transition. Use cut or fade.",
                )
            if (
                scene.preferred_visual_type == VisualType.stock_video
                and not scene.search_query.strip()
            ):
                raise IntelligenceError(
                    "visual_plan", f"Scene {scene.scene_id} needs a stock search query."
                )
            if (
                scene.preferred_visual_type
                in {VisualType.ai_image, VisualType.ai_video}
                and not scene.generation_prompt.strip()
            ):
                raise IntelligenceError(
                    "visual_plan",
                    f"Scene {scene.scene_id} needs a media generation prompt.",
                )

    def prepare(self):
        source = self.params.video_source or "pexels"
        if source not in SOURCE_VISUAL_TYPES and source != "builtin":
            raise IntelligenceError(
                "visual_plan",
                f"Source '{source}' has no scene-aware Codex execution adapter. "
                "Select built-in visuals, stock video, local assets, or a supported AI source; use Legacy for confirmed LoomLoom batches.",
            )
        if source == "builtin":
            from app.utils.file_security import resolve_path_within_directory

            try:
                self.screenshot_paths = [
                    resolve_path_within_directory(
                        utils.storage_dir("local_videos"), item.url
                    )
                    for item in (self.params.video_materials or [])
                ]
            except ValueError as exc:
                raise IntelligenceError(
                    "materials", "Screenshot assets must be uploaded local images."
                ) from exc
        supported_types = (
            [
                VisualType(value)
                for value in sorted(BUILTIN_VISUAL_TYPES)
                if value != "screenshot" or self.screenshot_paths
            ]
            if source == "builtin"
            else [SOURCE_VISUAL_TYPES[source]]
        )
        self.brief = ProductionBrief(
            subject=self.params.video_subject,
            language=self.params.video_language or "",
            supplied_script=self.params.video_script or "",
            instructions="\n".join(
                filter(
                    None,
                    [self.params.video_script_prompt, self.params.custom_system_prompt],
                )
            ),
            aspect_ratio=getattr(
                self.params.video_aspect, "value", self.params.video_aspect
            ),
            paragraph_number=self.params.paragraph_number,
            target_scene_duration=self.params.video_clip_duration,
            selected_source=source,
            supported_visual_types=supported_types,
            screenshot_count=len(self.screenshot_paths),
            quality_threshold=self.threshold,
        )
        self._write("production-brief.json", self.brief)
        self.plan = self._run(ProductionDirector, brief=self.brief)
        self._write("production-plan.json", self.plan)
        if getattr(self.params, "codex_review_enabled", True):
            self._review_plan()
        approved_plan = self.plan.model_dump()
        edited = self._run(ScriptEditor, brief=self.brief, plan=self.plan)
        self._validate_ids(edited, "production_plan")
        for scene, revision in zip(self.plan.scenes, edited.scenes):
            scene.narration = revision.narration
        visuals = self._run(VisualDirector, brief=self.brief, plan=self.plan)
        self._validate_ids(visuals, "visual_plan")
        for scene, visual in zip(self.plan.scenes, visuals.scenes):
            for field, value in visual.model_dump().items():
                if field != "scene_id":
                    setattr(scene, field, value)
        self._write("visual-plan.json", visuals)
        self._write("production-plan.json", self.plan)
        # A script/visual refinement can introduce a new defect. Re-review any
        # changed plan without resetting the shared plan repair budget.
        if (
            getattr(self.params, "codex_review_enabled", True)
            and self.plan.model_dump() != approved_plan
        ):
            self._review_plan()
        self._validate_execution()
        if self.brief.supplied_script and " ".join(
            self.brief.supplied_script.split()
        ) != " ".join(self.plan.narration.split()):
            raise IntelligenceError(
                "production_plan",
                "Codex changed the supplied script. Retry with instructions to preserve all supplied narration verbatim.",
            )
        self._save_plan()
        self.params.match_materials_to_script = True
        self.params.video_concat_mode = VideoConcatMode.sequential
        self.params.video_script = self.plan.narration
        self.params.video_terms = self.terms
        return self.plan.narration

    @property
    def terms(self):
        return [self._query(scene) for scene in self.plan.scenes]

    @staticmethod
    def _query(scene):
        if scene.preferred_visual_type in {VisualType.ai_image, VisualType.ai_video}:
            return scene.generation_prompt.strip()
        return scene.search_query.strip() or scene.visual_intent

    def _check_review(self, review, stage):
        if review.stage != stage:
            raise IntelligenceError(
                stage, "Codex returned a review for the wrong stage; retry the review."
            )
        valid = {scene.scene_id for scene in self.plan.scenes}
        if any(
            issue.scene_id is not None and issue.scene_id not in valid
            for issue in review.issues
        ):
            raise IntelligenceError(
                stage, "Codex review references an unknown scene; retry the review."
            )

    def _repair(self, review, pass_number):
        if pass_number > self.max_passes:
            raise IntelligenceError(
                review.stage,
                f"Quality score {review.score:g}/10 did not meet {self.threshold:g}/10 "
                f"after {self.max_passes} repair passes. Existing artifacts are preserved; inspect the review.",
            )
        proposal = self._run(
            RepairPlanner, brief=self.brief, plan=self.plan, review=review
        )
        ids = {scene.scene_id for scene in self.plan.scenes}
        rejected = {
            issue.scene_id for issue in review.issues if issue.severity != "info"
        }
        if None in rejected:
            rejected = ids
        if not rejected:
            raise IntelligenceError(
                "repair",
                "The rejected review has no actionable scene issues. Inspect the review and retry.",
            )
        touched = set()
        for action in proposal.actions:
            if action.scene_id not in ids or action.scene_id not in rejected:
                raise IntelligenceError(
                    "repair",
                    "A repair attempted to change an accepted or unknown scene; inspect the review.",
                )
            if action.scene_id in touched:
                raise IntelligenceError(
                    "repair",
                    "Codex proposed duplicate repairs for one scene. Combine them into one action.",
                )
            touched.add(action.scene_id)
            expected = (
                {"production_plan"}
                if review.stage == "plan_review"
                else {"materials", "video"}
            )
            if action.stage not in expected or (
                review.stage == "material_review" and action.stage != "materials"
            ):
                raise IntelligenceError(
                    "repair",
                    "The repair would invalidate an already completed stage; only targeted corrections are supported.",
                )
            scene = next(
                scene for scene in self.plan.scenes if scene.scene_id == action.scene_id
            )
            if action.stage == "production_plan":
                if (
                    action.action != "revise_scene"
                    or action.replacement_scene is None
                    or action.replacement_scene.scene_id != scene.scene_id
                ):
                    raise IntelligenceError(
                        "repair",
                        "Plan repair requires a complete replacement scene with the same ID.",
                    )
            elif action.stage == "materials":
                if action.action not in {
                    "replace_material",
                    "change_query",
                    "change_visual_type",
                    "revise_visual",
                }:
                    raise IntelligenceError(
                        "repair",
                        "Material repair requires a query, replacement asset, or supported modality change.",
                    )
                if (
                    action.visual_type is not None
                    and action.visual_type not in self.brief.supported_visual_types
                ):
                    raise IntelligenceError(
                        "repair",
                        "Requested visual modality is unavailable for the selected source.",
                    )
                if not (
                    action.search_query
                    or action.generation_prompt
                    or self.params.video_source == "local"
                    or action.builtin_visual is not None
                ):
                    raise IntelligenceError(
                        "repair",
                        "Material replacement needs a revised query or generation prompt.",
                    )
                if self.params.video_source == "builtin" and (
                    action.builtin_visual is None
                    or (
                        action.builtin_visual == scene.builtin_visual
                        and (
                            action.visual_type is None
                            or action.visual_type == scene.preferred_visual_type
                        )
                    )
                ):
                    raise IntelligenceError(
                        "repair",
                        "Built-in repair requires a changed visual specification.",
                    )
            elif (
                action.action != "rerender"
                or action.video_fit_mode is None
                or action.video_fit_mode
                == self.scene_fit_modes.get(scene.scene_id, self.params.video_fit_mode)
            ):
                raise IntelligenceError(
                    "repair",
                    "A render repair must change framing or replace rejected scene media; identical rerenders are disabled.",
                )
        self.history.append(
            RepairRecord(
                stage=review.stage,
                pass_number=pass_number,
                review_score=review.score,
                actions=proposal.actions,
            )
        )
        self._write("repair-history.json", self.history)
        return proposal.actions

    def _review_plan(self):
        for _ in range(self.max_passes - self._plan_repair_passes + 1):
            review = self._run(PlanReviewer, brief=self.brief, plan=self.plan)
            self._check_review(review, "plan_review")
            self._write("production-review.json", review)
            self._write(
                f"production-review-pass-{self._plan_review_count}.json", review
            )
            self._plan_review_count += 1
            if review.passes(self.threshold):
                return
            for action in self._repair(review, self._plan_repair_passes + 1):
                index = next(
                    i
                    for i, scene in enumerate(self.plan.scenes)
                    if scene.scene_id == action.scene_id
                )
                self.plan.scenes[index] = action.replacement_scene
            self._plan_repair_passes += 1
            self._write("production-plan.json", self.plan)

    def set_timeline(self, audio_duration, subtitle_path=""):
        """Prefer exact scene starts from narration subtitles, then documented timing estimates."""
        duration = float(audio_duration)
        if not math.isfinite(duration) or duration <= 0:
            raise IntelligenceError(
                "visual_plan", "Narration duration must be positive to align scenes."
            )
        starts = None
        timing_source = "target_duration_scaled_to_audio"
        if subtitle_path and Path(subtitle_path).is_file():
            from app.services import subtitle
            from difflib import SequenceMatcher

            try:
                cues = subtitle.file_to_subtitles(subtitle_path)

                def normalize(text):
                    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE).casefold()

                cue_text = "".join(normalize(cue[2]) for cue in cues)
                script_text = "".join(
                    normalize(scene.narration) for scene in self.plan.scenes
                )
                # Only trust alignment when subtitle text represents this narration.
                if (
                    cue_text == script_text
                    or SequenceMatcher(None, cue_text, script_text).ratio() >= 0.98
                ):
                    offsets, count = [], 0
                    for cue in cues:
                        timestamp = cue[1].split(" --> ")[0].replace(",", ".")
                        hours, minutes, seconds = map(float, timestamp.split(":"))
                        offsets.append((count, hours * 3600 + minutes * 60 + seconds))
                        count += len(normalize(cue[2]))
                    starts, position = [], 0
                    for scene in self.plan.scenes:
                        eligible = [
                            offset for offset in offsets if offset[0] <= position
                        ]
                        starts.append(eligible[-1][1] if eligible else 0)
                        position += len(normalize(scene.narration))
                    starts[0] = 0.0
                    if len(set(starts)) != len(starts) or starts[-1] >= duration:
                        starts = None
                    else:
                        timing_source = "narration_subtitles"
            except (ValueError, TypeError, IndexError, AttributeError):
                starts = None
        if starts is None:
            total = sum(scene.target_duration for scene in self.plan.scenes)
            elapsed, starts = 0.0, []
            for scene in self.plan.scenes:
                starts.append(elapsed)
                elapsed += duration * scene.target_duration / total
        # Quantize cumulative boundaries, not individual durations: rounding
        # scene lengths independently can accumulate seconds of drift.
        from app.services import video

        frame_rate = float(video.fps)
        frame_count = math.ceil(duration * frame_rate)
        scene_count = len(self.plan.scenes)
        if frame_count < scene_count:
            raise IntelligenceError(
                "visual_plan",
                "The narration is too short for the planned scenes. Reduce the scene count.",
            )
        boundaries = [0]
        for index, start in enumerate(starts[1:], 1):
            boundaries.append(
                min(
                    max(round(start * frame_rate), boundaries[-1] + 1),
                    frame_count - (scene_count - index),
                )
            )
        starts = [frame / frame_rate for frame in boundaries]
        duration = frame_count / frame_rate
        self.timeline = [
            {
                "scene_id": scene.scene_id,
                "start": start,
                "end": starts[index + 1] if index + 1 < len(starts) else duration,
            }
            for index, (scene, start) in enumerate(zip(self.plan.scenes, starts))
        ]
        self._write(
            "scene-timeline.json",
            {
                "timing_source": timing_source,
                "frame_rate": frame_rate,
                "scenes": self.timeline,
            },
        )

    def _acquire_scene(self, scene, acquire, exclude=()):
        if self.params.video_source == "builtin":
            from app.intelligence.builtin_visuals import render_builtin_visual

            try:
                paths = [
                    render_builtin_visual(
                        scene.scene_id,
                        scene.preferred_visual_type.value,
                        scene.builtin_visual,
                        self.task_dir / "builtin-visuals",
                        VideoAspect(self.params.video_aspect).to_resolution(),
                        self.params.font_name,
                        screenshot_paths=self.screenshot_paths,
                    )
                ]
            except (ValueError, OSError) as exc:
                raise IntelligenceError(
                    "materials",
                    f"Could not render built-in visual for scene {scene.scene_id}: {exc}",
                ) from exc
            paths = [item for item in paths if item not in exclude]
        elif self.params.video_source == "local":
            candidates = [item for item in self.local_candidates if item not in exclude]
            if not candidates:
                raise IntelligenceError(
                    "repair" if exclude else "materials",
                    f"Scene {scene.scene_id} has no alternative local material. Add another local asset.",
                )
            index = next(
                i
                for i, item in enumerate(self.plan.scenes)
                if item.scene_id == scene.scene_id
            )
            paths = [candidates[index % len(candidates)]]
        else:
            scene_params = self.params.model_copy(deep=True)
            scene_params.video_count = 1
            scene_params.video_terms = [self._query(scene)]
            # One source clip per scene is sufficient; deterministic preparation can loop it.
            scene_params.video_clip_duration = max(1, math.ceil(scene.target_duration))
            paths = acquire(
                scene_params, scene_params.video_terms, scene.target_duration
            )
            if paths and exclude:
                paths = [item for item in paths if item not in exclude]
        if not paths:
            raise IntelligenceError(
                "repair" if exclude else "materials",
                f"No new usable material for scene {scene.scene_id}. Change its query or supply a local asset.",
            )
        return SceneMaterial(
            scene_id=scene.scene_id,
            paths=list(paths),
            source=self.params.video_source,
            visual_type=scene.preferred_visual_type,
            target_duration=scene.target_duration,
        )

    def acquire_materials(self, acquire):
        self.on_stage("materials")
        if self.params.video_source == "local":
            self.local_candidates = list(
                acquire(self.params, self.terms, self.timeline[-1]["end"]) or []
            )
        self.materials = []
        for scene in self.plan.scenes:
            self.materials.append(self._acquire_scene(scene, acquire))
            self._write("scene-materials.json", self.materials)
        return self.review_materials(acquire)

    def _material_sheet(self):
        from app.intelligence.contact_sheets import create_material_contact_sheet

        try:
            return create_material_contact_sheet(
                self.materials, self.plan, self.task_dir / "materials-contact-sheet.jpg"
            )
        except Exception as exc:
            raise IntelligenceError(
                "material_review",
                "Could not create the material contact sheet. Check local media readability and FFmpeg.",
            ) from exc

    def review_materials(self, acquire):
        from app.intelligence.contact_sheets import contact_sheet_pages

        enabled = getattr(self.params, "codex_material_review_enabled", True)
        for pass_number in range(self.max_passes + 1):
            sheet = self._material_sheet()
            if not enabled:
                return self.materials
            review = self._run(
                MaterialReviewer,
                brief=self.brief,
                plan=self.plan,
                images=tuple(contact_sheet_pages(sheet)),
            )
            self._check_review(review, "material_review")
            self._write("material-review.json", review)
            self._write(f"material-review-pass-{pass_number}.json", review)
            if review.passes(self.threshold):
                return self.materials
            self._apply_media_repairs(self._repair(review, pass_number + 1), acquire)

    def _apply_media_repairs(self, actions, acquire):
        for action in actions:
            if action.stage == "video":
                self.scene_fit_modes[action.scene_id] = action.video_fit_mode
                continue
            index = next(
                i
                for i, scene in enumerate(self.plan.scenes)
                if scene.scene_id == action.scene_id
            )
            scene = self.plan.scenes[index]
            if action.search_query is not None:
                scene.search_query = action.search_query
            if action.generation_prompt is not None:
                scene.generation_prompt = action.generation_prompt
            if action.visual_type is not None:
                scene.preferred_visual_type = action.visual_type
            if action.builtin_visual is not None:
                scene.builtin_visual = action.builtin_visual
            self._validate_execution()
            self.materials[index] = self._acquire_scene(
                scene, acquire, self.materials[index].paths
            )
        self._save_plan()
        self._write("scene-materials.json", self.materials)
        self._write("scene-render-settings.json", {"fit_modes": self.scene_fit_modes})

    def prepare_render(self):
        from app.intelligence.execution import prepare_scene_clips

        self.on_stage("video")
        try:
            paths = prepare_scene_clips(
                self.materials,
                self.task_dir,
                self.params,
                self.timeline,
                plan=self.plan,
                fit_modes=self.scene_fit_modes,
            )
        except ValueError as exc:
            raise IntelligenceError(
                "repair" if self.render_backups else "video", str(exc)
            ) from exc
        render_params = self.params.model_copy(deep=True)
        render_params.video_clip_duration = (
            math.ceil(max(row["end"] - row["start"] for row in self.timeline)) + 1
        )
        render_params.video_clip_speed = 1.0
        # Scene transitions and text are already applied once by the deterministic adapter.
        render_params.video_transition_mode = None
        return paths, render_params

    def review_renders(
        self,
        final_paths,
        combined_paths,
        warnings,
        acquire,
        render,
        *,
        narration_path=None,
        expected_audio=None,
    ):
        from app.intelligence.contact_sheets import (
            create_render_contact_sheet,
            contact_sheet_pages,
        )
        from app.intelligence.media_qa import inspect_render
        from app.services import voice

        enabled = getattr(self.params, "codex_render_review_enabled", True)
        if expected_audio is None:
            expected_audio = (
                bool(self.params.custom_audio_file)
                or not voice.is_no_voice(self.params.voice_name)
            ) and (self.params.voice_volume or 0) > 0
        warnings = list(warnings or [])
        for pass_number in range(self.max_passes + 1):
            reviews = []
            material_by_id = {item.scene_id: item for item in self.materials}
            inspection_timeline = []
            for row in self.timeline:
                evidence = dict(row)
                material = material_by_id.get(row["scene_id"])
                if material is not None:
                    evidence["expected_static"] = all(
                        Path(asset).suffix.lower()
                        in {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
                        for asset in material.paths
                    )
                inspection_timeline.append(evidence)
            for index, video_path in enumerate(final_paths):
                self.on_stage("render_review")
                inspection = inspect_render(
                    video_path,
                    self.plan,
                    inspection_timeline,
                    narration_path=narration_path,
                    expected_audio=expected_audio,
                )
                self._write(
                    f"media-inspection-video-{index + 1}-pass-{pass_number}.json",
                    inspection,
                )
                if index == 0:
                    self._write("media-inspection.json", inspection)
                for issue in inspection.issues:
                    if (
                        issue.severity == "warning"
                        and issue.description not in warnings
                    ):
                        warnings.append(issue.description)
                if not inspection.passed:
                    technical_review = ProductionReview(
                        stage="render_review",
                        score=0,
                        approved=False,
                        summary=inspection.summary,
                        issues=inspection.issues,
                    )
                    self._write("render-review.json", technical_review)
                    self._write(
                        f"render-review-video-{index + 1}-pass-{pass_number}.json",
                        technical_review,
                    )
                    raise IntelligenceError(
                        "render_review",
                        "The full-file media inspection found a technical failure. The video and inspection report are preserved; "
                        + "; ".join(
                            issue.description
                            for issue in inspection.issues
                            if issue.severity == "error"
                        ),
                    )
                try:
                    sheet = create_render_contact_sheet(
                        video_path,
                        self.plan,
                        self.task_dir
                        / (
                            "render-contact-sheet.jpg"
                            if index == 0
                            else f"render-contact-sheet-{index + 1}.jpg"
                        ),
                        timeline=self.timeline,
                    )
                except Exception as exc:
                    raise IntelligenceError(
                        "render_review",
                        "Could not extract rendered frames. The video is preserved; check FFmpeg and retry QA.",
                    ) from exc
                if not enabled:
                    continue
                review = self._run(
                    RenderReviewer,
                    brief=self.brief,
                    plan=self.plan,
                    timeline=self.timeline,
                    media_inspection=inspection,
                    images=tuple(contact_sheet_pages(sheet)),
                )
                self._check_review(review, "render_review")
                self._write(
                    f"render-review-video-{index + 1}-pass-{pass_number}.json", review
                )
                reviews.append(review)
            if not enabled:
                return final_paths, combined_paths, warnings
            # The aggregate review fails if any output fails; preserve every per-video review.
            failed = [review for review in reviews if not review.passes(self.threshold)]
            aggregate = ProductionReview(
                stage="render_review",
                score=min(review.score for review in reviews),
                approved=not failed,
                summary="; ".join(review.summary for review in reviews),
                issues=[issue for review in failed for issue in review.issues],
            )
            self._write("render-review.json", aggregate)
            if not failed:
                return final_paths, combined_paths, warnings
            actions = self._repair(aggregate, pass_number + 1)
            # Preserve the last usable render before composition replaces final-N.mp4.
            self.render_backups = []
            for current in [*final_paths, *combined_paths]:
                source = Path(current)
                if source.is_file():
                    backup = source.with_name(
                        f"{source.stem}-before-repair-{pass_number + 1}{source.suffix}"
                    )
                    shutil.copy2(source, backup)
                    if current in final_paths:
                        self.render_backups.append(str(backup))
            self._write("render-backups.json", self.render_backups)
            self._apply_media_repairs(actions, acquire)
            self._material_sheet()
            prepared, render_params = self.prepare_render()
            final_paths, combined_paths, new_warnings = render(prepared, render_params)
            warnings = list(warnings or []) + list(new_warnings or [])
            if not final_paths:
                raise IntelligenceError(
                    "repair",
                    "Targeted rendering failed. Previous renders remain in before-repair files.",
                )

    @property
    def material_paths(self):
        return [item for scene in self.materials for item in scene.paths]
