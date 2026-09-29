"""Local, labeled visual evidence for Codex; never fetch remote media here."""

from __future__ import annotations

import io
import json
import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFont, ImageOps

from app.utils import utils

_TILE_WIDTH = 480
_MAX_TILE_HEIGHT = 1920
_LABEL_HEIGHT = 80
_MAX_PAGE_WIDTH = 1440
_MAX_PAGE_HEIGHT = 3000
_MAX_PAGE_PIXELS = _MAX_PAGE_WIDTH * _MAX_PAGE_HEIGHT
_MAX_TILES = 480
_IMAGES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}


def _field(item, key, default=None):
    return (
        item.get(key, default)
        if isinstance(item, dict)
        else getattr(item, key, default)
    )


def media_duration(path) -> float:
    """Read local metadata using the same FFmpeg installation as MoviePy/MPT."""
    from moviepy.video.io.ffmpeg_reader import ffmpeg_parse_infos

    local = Path(path).expanduser().resolve(strict=True)
    value = float(ffmpeg_parse_infos(str(local))["duration"])
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Media must have a finite positive duration")
    return value


def extract_thumbnail(path, timestamp=0.0) -> Image.Image:
    local = Path(path).expanduser().resolve(strict=True)
    if not local.is_file():
        raise ValueError("Contact sheets require local media files")
    if local.suffix.lower() in _IMAGES:
        with Image.open(local) as source:
            return ImageOps.exif_transpose(source).convert("RGB").copy()
    if not math.isfinite(timestamp) or timestamp < 0:
        raise ValueError("Invalid frame timestamp")
    result = subprocess.run(
        [
            utils.get_ffmpeg_binary(),
            "-v",
            "error",
            "-nostdin",
            "-ss",
            f"{timestamp:.6f}",
            "-i",
            str(local),
            "-frames:v",
            "1",
            "-vf",
            "scale=1080:1080:force_original_aspect_ratio=decrease",
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "pipe:1",
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode or not result.stdout:
        # FFmpeg stderr may contain paths or embedded metadata. Do not copy it
        # into task artifacts, UI errors, or logs.
        raise ValueError("Could not decode a representative frame from local media")
    with Image.open(io.BytesIO(result.stdout)) as source:
        return source.convert("RGB").copy()


def _manifest_path(first_path: Path) -> Path:
    return first_path.with_name(f"{first_path.stem}.pages.json")


def contact_sheet_pages(first_path) -> list[str]:
    """Resolve all ordered evidence pages from one atomic generation manifest.

    Older single-image sheets and mocked callers keep working without a manifest.
    Invalid manifests fail explicitly instead of silently omitting review evidence.
    """
    first = Path(first_path).expanduser().resolve()
    manifest = _manifest_path(first)
    if not manifest.exists():
        return [str(first)]
    try:
        metadata = json.loads(manifest.read_text(encoding="utf-8"))
        generation = metadata["generation"]
        pages = metadata["pages"]
        if (
            metadata.get("version") != 1
            or not isinstance(generation, str)
            or len(generation) != 32
            or any(character not in "0123456789abcdef" for character in generation)
            or not isinstance(pages, list)
            or not 1 <= len(pages) <= _MAX_TILES
        ):
            raise ValueError("Invalid contact sheet manifest")
        paths = []
        covered = 0
        for index, page in enumerate(pages, start=1):
            filename = f"{first.stem}-{generation}-page-{index:04d}.jpg"
            labels = page["labels"]
            if (
                page["file"] != filename
                or page["sample_start"] != covered
                or not isinstance(labels, list)
                or not labels
                or any(not isinstance(label, str) for label in labels)
            ):
                raise ValueError("Invalid contact sheet page sequence")
            local = (first.parent / filename).resolve(strict=True)
            if local.parent != first.parent or not local.is_file():
                raise ValueError("Contact sheet page is not a local generation file")
            paths.append(str(local))
            covered += len(labels)
        if covered != metadata["sample_count"] or not 1 <= covered <= _MAX_TILES:
            raise ValueError("Incomplete contact sheet evidence")
        return paths
    except (OSError, TypeError, KeyError, ValueError) as exc:
        raise ValueError("Could not resolve complete contact sheet evidence") from exc


def _label_lines(draw, label, font):
    """Wrap identifiers and timestamps inside their tile, including long IDs."""
    lines = []
    line = ""
    for character in str(label):
        if (
            character == "\n"
            or draw.textlength(line + character, font=font) > _TILE_WIDTH - 16
        ):
            lines.append(line)
            line = "" if character == "\n" else character
        else:
            line += character
    if line:
        lines.append(line)
    if len(lines) > 3:
        lines = lines[:3]
        lines[-1] = lines[-1][:-3] + "..."
    return lines


def _evidence_row(samples) -> Image.Image:
    thumbnails = []
    for path, timestamp, _ in samples:
        try:
            thumb = extract_thumbnail(path, timestamp)
            height = max(
                1,
                min(_MAX_TILE_HEIGHT, round(_TILE_WIDTH * thumb.height / thumb.width)),
            )
            # Retain only one bounded row of thumbnails, even for 480 samples.
            thumbnails.append(ImageOps.contain(thumb, (_TILE_WIDTH, height)))
        except (OSError, ValueError, subprocess.SubprocessError):
            thumbnails.append(None)
    content_height = max(
        (thumb.height for thumb in thumbnails if thumb is not None), default=270
    )
    row = Image.new(
        "RGB", (len(samples) * _TILE_WIDTH, content_height + _LABEL_HEIGHT), "#161a20"
    )
    draw = ImageDraw.Draw(row)
    font = ImageFont.load_default(size=18)
    for index, (thumb, (_, _, label)) in enumerate(zip(thumbnails, samples)):
        x = index * _TILE_WIDTH
        if thumb is None:
            draw.rectangle(
                (x, 0, x + _TILE_WIDTH - 1, content_height - 1), fill="#4a2020"
            )
            draw.text((x + 16, 20), "UNREADABLE MEDIA", font=font, fill="white")
            draw.text((x + 16, 46), "Reject this asset", font=font, fill="white")
        else:
            draw.rectangle(
                (x, 0, x + _TILE_WIDTH - 1, content_height - 1), fill="#0c0e12"
            )
            row.paste(
                thumb,
                (
                    x + (_TILE_WIDTH - thumb.width) // 2,
                    (content_height - thumb.height) // 2,
                ),
            )
        for line_index, line in enumerate(_label_lines(draw, label, font)):
            draw.text(
                (x + 8, content_height + 6 + line_index * 23),
                line,
                font=font,
                fill="white",
            )
    return row


def _save_page(rows, path):
    size = (max(row.width for row in rows), sum(row.height for row in rows))
    if (
        size[0] > _MAX_PAGE_WIDTH
        or size[1] > _MAX_PAGE_HEIGHT
        or size[0] * size[1] > _MAX_PAGE_PIXELS
    ):
        raise ValueError("Contact sheet page exceeds evidence image limits")
    page = Image.new("RGB", size, "#161a20")
    y = 0
    for row in rows:
        page.paste(row, (0, y))
        y += row.height
    page.save(path, "JPEG", quality=95, subsampling=0)


def _sheet(samples, output_path) -> str:
    if not samples or len(samples) > _MAX_TILES:
        raise ValueError(f"Contact sheet needs 1–{_MAX_TILES} evidence tiles")
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    generation = uuid4().hex
    pages = []
    generated_paths = []
    temporary_paths = []
    previous_preview = None
    preview_replaced = False
    rows, row_labels, height = [], [], 0

    def flush_page():
        filename = f"{target.stem}-{generation}-page-{len(pages) + 1:04d}.jpg"
        path = target.parent / filename
        generated_paths.append(path)
        _save_page(rows, path)
        pages.append(
            {
                "file": filename,
                "sample_start": sum(len(page["labels"]) for page in pages),
                "labels": list(row_labels),
            }
        )

    try:
        for offset in range(0, len(samples), 3):
            batch = samples[offset : offset + 3]
            row = _evidence_row(batch)
            if rows and height + row.height > _MAX_PAGE_HEIGHT:
                flush_page()
                rows, row_labels, height = [], [], 0
            rows.append(row)
            row_labels.extend(str(sample[2]) for sample in batch)
            height += row.height
        flush_page()

        metadata = {
            "version": 1,
            "generation": generation,
            "sample_count": len(samples),
            "pages": pages,
        }
        for suffix in (".jpg", ".json"):
            with tempfile.NamedTemporaryFile(
                dir=target.parent, suffix=suffix, delete=False
            ) as handle:
                temporary_paths.append(Path(handle.name))
        shutil.copyfile(generated_paths[0], temporary_paths[0])
        temporary_paths[1].write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if target.exists():
            with tempfile.NamedTemporaryFile(
                dir=target.parent, suffix=".jpg", delete=False
            ) as handle:
                previous_preview = Path(handle.name)
                temporary_paths.append(previous_preview)
            shutil.copyfile(target, previous_preview)
        # All uniquely named pages are complete before publishing the manifest.
        # A previous manifest keeps referring to its own immutable generation.
        os.replace(temporary_paths[0], target)
        preview_replaced = True
        os.replace(temporary_paths[1], _manifest_path(target))
    except Exception:
        if preview_replaced:
            if previous_preview is not None:
                os.replace(previous_preview, target)
            else:
                target.unlink(missing_ok=True)
        for path in generated_paths:
            path.unlink(missing_ok=True)
        raise
    finally:
        for path in temporary_paths:
            path.unlink(missing_ok=True)
    return str(target.resolve())


def create_material_contact_sheet(scene_materials, plan, output_path) -> str:
    """One representative frame per chosen asset, in planned scene order."""
    by_scene = {_field(item, "scene_id"): item for item in scene_materials}
    samples = []
    for scene in plan.scenes:
        material = by_scene.get(scene.scene_id)
        paths = _field(material, "paths", []) or []
        if not paths:
            samples.append(("", 0.0, f"{scene.scene_id} | MISSING ASSET"))
        for index, path in enumerate(paths):
            timestamp = 0.0
            if Path(path).suffix.lower() not in _IMAGES:
                try:
                    timestamp = media_duration(path) / 2
                except Exception:
                    # The tile will clearly show unreadable/missing evidence.
                    pass
            samples.append(
                (
                    path,
                    timestamp,
                    f"{scene.scene_id} | asset {index + 1} | {timestamp:.2f}s",
                )
            )
    return _sheet(samples, output_path)


def scene_timeline(plan, duration=None) -> list[dict]:
    total = sum(scene.target_duration for scene in plan.scenes)
    duration = total if duration is None else float(duration)
    if not math.isfinite(duration) or duration <= 0 or total <= 0:
        raise ValueError("Scene timeline requires a finite positive duration")
    cursor = 0.0
    timeline = []
    for scene in plan.scenes:
        end = cursor + scene.target_duration * duration / total
        timeline.append({"scene_id": scene.scene_id, "start": cursor, "end": end})
        cursor = end
    return timeline


def create_render_contact_sheet(
    video_path, plan, output_path, duration=None, timeline=None
) -> str:
    """Sample each actual scene near its start, midpoint and end."""
    if timeline is None:
        timeline = scene_timeline(
            plan, duration if duration is not None else media_duration(video_path)
        )
    samples = []
    for scene in timeline:
        start, end = float(_field(scene, "start")), float(_field(scene, "end"))
        if not all(math.isfinite(t) for t in (start, end)) or start < 0 or end <= start:
            raise ValueError("Invalid render scene boundaries")
        margin = min(0.08, (end - start) / 8)
        for position, timestamp in (
            ("start", start + margin),
            ("middle", (start + end) / 2),
            ("end", end - margin),
        ):
            samples.append(
                (
                    video_path,
                    timestamp,
                    f"{_field(scene, 'scene_id')} | {position} | {timestamp:.2f}s",
                )
            )
    return _sheet(samples, output_path)
