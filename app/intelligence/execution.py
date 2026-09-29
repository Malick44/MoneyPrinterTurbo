"""Deterministic scene preparation using MoneyPrinterTurbo's media tooling."""

from __future__ import annotations

import hashlib
import json
import math
import os
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

import numpy as np
from moviepy import CompositeVideoClip, ImageClip, concatenate_videoclips, vfx
from PIL import Image, ImageDraw, ImageFont

from app.models.schema import VideoAspect
from app.services import video
from app.utils import utils

SUPPORTED_TRANSITIONS = ("cut", "fade")
_IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}


def _text_overlay(text, size, font_name):
    width, height = size
    font_path = Path(utils.font_dir(font_name or "STHeitiMedium.ttc"))
    if not font_path.is_file():
        raise ValueError(
            "Scene text requires an installed font; select a valid font in MoneyPrinterTurbo"
        )
    font = ImageFont.truetype(str(font_path), max(16, width // 24))
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    # Character-based wrapping also works for languages without spaces.
    lines = []
    for paragraph in text.splitlines():
        line = ""
        for character in paragraph:
            if line and draw.textlength(line + character, font=font) > width * 0.8:
                lines.append(line.rstrip())
                line = character.lstrip()
            else:
                line += character
        lines.append(line)
    line_height = max(22, int(font.size * 1.4))
    if len(lines) * line_height > height * 0.45:
        raise ValueError(
            "Scene on-screen text is too long; shorten it before rendering"
        )
    y = int(height * 0.10)
    draw.rounded_rectangle(
        (
            int(width * 0.06),
            y - 12,
            int(width * 0.94),
            y + len(lines) * line_height + 12,
        ),
        radius=12,
        fill=(0, 0, 0, 175),
    )
    for line in lines:
        x = (width - draw.textlength(line, font=font)) / 2
        draw.text(
            (x, y), line, font=font, fill="white", stroke_width=1, stroke_fill="black"
        )
        y += line_height
    return np.array(canvas)


def prepare_scene_clips(
    materials, task_dir, video_params, timeline, plan=None, fit_modes=None
) -> list[str]:
    """Trim/loop each scene exactly once and reuse unchanged normalized clips.

    Returned files remain ordered for the existing sequential compositor. No
    intelligence, network requests, TTS, subtitles or provider selection happens
    here. Changing one scene invalidates only that scene's cache entry.
    """
    target_dir = Path(task_dir) / "scene-clips"
    target_dir.mkdir(parents=True, exist_ok=True)
    by_id = {item.scene_id: item for item in materials}
    plans = {scene.scene_id: scene for scene in plan.scenes} if plan else {}
    size = VideoAspect(video_params.video_aspect).to_resolution()
    speed = utils.normalize_clip_speed(video_params.video_clip_speed)
    result = []
    for segment in timeline:
        scene_id = segment["scene_id"]
        item = by_id[scene_id]
        duration = float(segment["end"]) - float(segment["start"])
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("Scene duration must be finite and positive")
        frames = round(duration * video.fps)
        if frames < 1 or abs(frames - duration * video.fps) > 1e-6:
            raise ValueError(
                "Scene boundaries must be aligned to output frames before preparation"
            )
        # MoviePy floors duration*fps. One ULP protects exact intended frame
        # counts from floating-point subtraction at later scene boundaries.
        duration = math.nextafter(frames / video.fps, math.inf)
        scene = plans.get(scene_id)
        fit_mode = (fit_modes or {}).get(scene_id, video_params.video_fit_mode)
        transition = scene.transition if scene else "cut"
        text = scene.on_screen_text if scene else ""
        if transition not in SUPPORTED_TRANSITIONS:
            raise ValueError("Unsupported scene transition; choose cut or fade")
        paths = [Path(path).expanduser().resolve(strict=True) for path in item.paths]
        source_keys = [
            (str(path), path.stat().st_size, path.stat().st_mtime_ns) for path in paths
        ]
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "version": 1,
                    "sources": source_keys,
                    "duration": duration,
                    "size": size,
                    "speed": speed,
                    "fit": fit_mode,
                    "text": text,
                    "font": video_params.font_name,
                    "transition": transition,
                    "fps": video.fps,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()[:24]
        # Hash IDs as well, so even non-contract callers cannot supply a path.
        scene_key = hashlib.sha256(scene_id.encode()).hexdigest()[:12]
        output = target_dir / f"{scene_key}-{fingerprint}.mp4"
        if output.is_file() and output.stat().st_size:
            result.append(str(output))
            continue
        temporary = target_dir / f".{scene_key}-{uuid4().hex}.mp4"
        try:
            with ExitStack() as stack:
                clips = []
                for path in paths:
                    if path.suffix.lower() in _IMAGES:
                        clip = ImageClip(str(path)).with_duration(duration / len(paths))
                    else:
                        clip = video._open_video_clip_quietly(str(path))
                    stack.callback(video.close_clip, clip)
                    if not clip.duration or clip.duration <= 0:
                        raise ValueError("Scene contains empty media")
                    if speed != 1 and path.suffix.lower() not in _IMAGES:
                        clip = clip.with_speed_scaled(speed)
                    clip = video._fit_clip_to_canvas(
                        clip,
                        target_width=size[0],
                        target_height=size[1],
                        fit_mode=fit_mode,
                    )
                    clips.append(clip.without_audio())
                combined = concatenate_videoclips(clips, method="chain")
                stack.callback(video.close_clip, combined)
                combined = combined.with_effects(
                    [vfx.Loop(duration=duration)]
                ).with_duration(duration)
                if text:
                    overlay = ImageClip(
                        _text_overlay(text, size, video_params.font_name)
                    ).with_duration(duration)
                    stack.callback(video.close_clip, overlay)
                    combined = CompositeVideoClip([combined, overlay], size=size)
                    stack.callback(video.close_clip, combined)
                if transition == "fade":
                    fade_duration = min(0.25, duration / 4)
                    combined = combined.with_effects(
                        [vfx.FadeIn(fade_duration), vfx.FadeOut(fade_duration)]
                    )
                video._write_videofile_with_codec_fallback(
                    combined,
                    str(temporary),
                    codec=video._get_configured_video_codec(),
                    fps=video.fps,
                    audio=False,
                    logger=None,
                    threads=video_params.n_threads,
                )
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
        result.append(str(output))
    return result
