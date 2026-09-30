"""Version-pinned case evidence, original-sound previews and optional alignment.

Originals stay byte-for-byte immutable. Source transcripts and production
narration alignment have separate scopes; neither asserts a speaker's identity.
Optional engines are loaded only for the requested operation.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import shutil
import tempfile
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from app.models.search import SearchError

from .media import (
    _staging,
    executable,
    promote_artifact,
    run_command,
    setting,
    sha256_file,
    validate_range,
    verified_artifact_path,
)
from .repository import json_text, now

VERSION = "case-media-1"
MAX_IMAGE_PIXELS = 40_000_000
MAX_TEXT_CHARACTERS = 2_000_000
MAX_PDF_PAGES = 500
MAX_TRANSCRIPT_WORDS = 100_000


def _runtime(package: str) -> str:
    try:
        return version(package)
    except PackageNotFoundError:
        return "unknown"


def capabilities(workspace=None) -> dict:
    """Report installed engines, separately from model availability."""
    packages = {
        name: importlib.util.find_spec(name) is not None
        for name in (
            "pypdf",
            "pypdfium2",
            "pytesseract",
            "faster_whisper",
            "whisperx",
            "open_clip",
        )
    }
    packages["ocr"] = packages["pytesseract"] and bool(shutil.which("tesseract"))
    packages["ffmpeg"] = bool(shutil.which("ffmpeg"))
    packages["alignment_checkpoint_configured"] = bool(
        workspace and setting(workspace, "whisperx_alignment_checkpoint", "")
    )
    return packages


def _snapshot(
    workspace, asset_id: str, requested_use: str = "analysis"
) -> tuple[dict, Path]:
    workspace.authorize_asset(asset_id, requested_use)
    asset = workspace.get_asset(asset_id)
    path = Path(workspace.asset_path(asset_id))
    if sha256_file(path) != asset["sha256"]:
        raise SearchError("Case original failed version digest verification", 409)
    return asset, path


def _current(workspace, asset: dict, requested_use: str = "analysis") -> None:
    workspace.authorize_asset(asset["id"], requested_use)
    current = workspace.get_asset(asset["id"])
    if (current["asset_version_id"], current["sha256"]) != (
        asset["asset_version_id"],
        asset["sha256"],
    ):
        raise SearchError(
            "Case asset changed during processing; index its current version", 409
        )
    workspace.asset_path(asset["id"])


def asset_content(
    workspace, asset_id: str, requested_use: str = "internal_review"
) -> Path:
    """Authorize current use and hash-check an immutable original."""
    return _snapshot(workspace, asset_id, requested_use)[1]


def preview_content(
    workspace, artifact_id: str, requested_use="internal_review"
) -> Path:
    """Read previews after current policy, input version and immutable CAS checks."""
    artifact = workspace.repo.get("artifacts", artifact_id)
    if not artifact:
        raise SearchError("Case preview artifact was not found", 404)
    if artifact["kind"] in {"case_render", "case_render_manifest"}:
        from .case_production import authorize_render_artifact

        return authorize_render_artifact(workspace, artifact_id, requested_use)
    metadata = artifact.get("metadata", {})
    asset_id = metadata.get("asset_id")
    if not asset_id and artifact["kind"] == "case_original":
        with workspace.repo.connect() as connection:
            row = connection.execute(
                "SELECT id FROM case_assets WHERE artifact_id=?", (artifact_id,)
            ).fetchone()
        asset_id = row["id"] if row else None
    if not asset_id:
        raise SearchError("Preview has no canonical case asset provenance", 409)
    asset, _ = _snapshot(workspace, asset_id, requested_use)
    if artifact["source_id"] != asset["source_id"]:
        raise SearchError("Preview does not belong to this case asset", 409)
    if artifact["kind"] != "case_original" and (
        metadata.get("asset_version_id") != asset["asset_version_id"]
        or metadata.get("input_sha256") != asset["sha256"]
    ):
        raise SearchError("Preview references a superseded asset version", 409)
    if artifact["kind"] == "narration_alignment":
        record = json.loads(
            verified_artifact_path(workspace.repo, artifact).read_text(encoding="utf-8")
        )
        script, _ = _snapshot(workspace, record["script_asset_id"], "internal_review")
        if (
            script["sha256"] != record["script_sha256"]
            or script["asset_version_id"] != record["script_asset_version_id"]
        ):
            raise SearchError("Narration alignment script was superseded", 409)
    return verified_artifact_path(workspace.repo, artifact)


def _unit(
    workspace, asset: dict, text, locator_type, locator, unit_kind, origin, **kwargs
):
    metadata = {
        **kwargs.pop("metadata", {}),
        "asset_version_id": asset["asset_version_id"],
        "input_sha256": asset["sha256"],
    }
    return workspace.add_evidence_unit(
        asset["id"],
        text,
        locator_type,
        locator,
        unit_kind,
        origin,
        metadata=metadata,
        **kwargs,
    )


def _artifact(
    workspace,
    asset: dict,
    staged: Path,
    kind: str,
    metadata: dict,
    requested_use: str = "analysis",
    start_ms=None,
    end_ms=None,
) -> dict:
    _current(workspace, asset, requested_use)
    metadata = {
        **metadata,
        "case_id": asset["case_id"],
        "asset_id": asset["id"],
        "asset_version_id": asset["asset_version_id"],
        "input_sha256": asset["sha256"],
        "pipeline_version": VERSION,
        "requested_use": requested_use,
    }
    identity = json_text(
        {
            "asset_version_id": asset["asset_version_id"],
            "kind": kind,
            "metadata": metadata,
            "sha256": sha256_file(staged),
        }
    )
    artifact_id = "case_" + hashlib.sha256(identity.encode()).hexdigest()[:32]
    result = promote_artifact(
        workspace.repo,
        staged,
        id=artifact_id,
        source_id=asset["source_id"],
        kind=kind,
        profile=VERSION,
        parent_artifact_id=asset["artifact_id"],
        start_ms=start_ms,
        end_ms=end_ms,
        metadata=metadata,
    )
    workspace.add_derivative(asset["id"], result["id"], kind, metadata=metadata)
    return result


def _json_artifact(
    workspace, asset: dict, record: dict, kind: str, requested_use="analysis"
) -> dict:
    with tempfile.TemporaryDirectory(
        prefix="case-json-", dir=_staging(workspace.repo)
    ) as directory:
        staged = Path(directory) / "record.json"
        staged.write_text(json_text(record), encoding="utf-8")
        return _artifact(
            workspace, asset, staged, kind, {"record_schema": VERSION}, requested_use
        )


def probe_asset(path: Path, timeout: int = 30) -> dict:
    """Probe standalone audio and video without requiring a video stream."""
    result = run_command(
        [
            executable("ffprobe"),
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ],
        timeout=timeout,
    )
    try:
        raw = json.loads(result.stdout)
        streams = raw.get("streams", [])
        duration = float(
            raw.get("format", {}).get("duration")
            or max(float(s.get("duration") or 0) for s in streams)
        )
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError()
        audios = [
            {
                "stream_index": int(s["index"]),
                "codec": s.get("codec_name"),
                "channels": int(s.get("channels", 0)),
                "channel_layout": s.get("channel_layout"),
                "sample_rate": int(s.get("sample_rate", 0)),
                "sample_format": s.get("sample_fmt"),
                "bits_per_sample": int(
                    s.get("bits_per_raw_sample") or s.get("bits_per_sample") or 0
                ),
                "duration_ms": round(float(s.get("duration") or duration) * 1000),
            }
            for s in streams
            if s.get("codec_type") == "audio"
        ]
        videos = [
            {
                "stream_index": int(s["index"]),
                "codec": s.get("codec_name"),
                "width": int(s["width"]),
                "height": int(s["height"]),
                "fps": s.get("avg_frame_rate"),
            }
            for s in streams
            if s.get("codec_type") == "video"
        ]
        if not audios and not videos:
            raise ValueError()
        return {
            "duration_ms": round(duration * 1000),
            "audio_streams": audios,
            "video_streams": videos,
            "has_audio": bool(audios),
            "has_video": bool(videos),
        }
    except (ValueError, KeyError, TypeError) as exc:
        raise SearchError(
            "Case media has no playable audio or video stream", 422
        ) from exc


def _channel_filter(probe: dict, channel: int | None) -> list[str]:
    if not probe["audio_streams"]:
        raise SearchError("This asset has no audio stream", 422)
    if channel is None:
        return []
    if (
        isinstance(channel, bool)
        or not isinstance(channel, int)
        or not 0 <= channel < probe["audio_streams"][0]["channels"]
    ):
        raise SearchError("Audio channel is outside the first source audio stream", 422)
    return ["-af", f"pan=mono|c0=c{channel}"]


def extract_audio_preview(
    workspace,
    asset_id: str,
    start_ms: int,
    end_ms: int,
    channel: int | None = None,
    requested_use="internal_review",
) -> dict:
    asset, path = _snapshot(workspace, asset_id, requested_use)
    probe = probe_asset(path)
    validate_range(
        start_ms,
        end_ms,
        probe["duration_ms"],
        setting(workspace, "max_clip_duration_ms", 90_000),
    )
    filters = _channel_filter(probe, channel)
    with tempfile.TemporaryDirectory(
        prefix="case-audio-", dir=_staging(workspace.repo)
    ) as directory:
        staged = Path(directory) / "excerpt.wav"
        run_command(
            [
                executable("ffmpeg"),
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-ss",
                f"{start_ms / 1000:.3f}",
                "-i",
                str(path),
                "-t",
                f"{(end_ms - start_ms) / 1000:.3f}",
                "-map",
                "0:a:0",
                "-vn",
                "-map_metadata",
                "-1",
                *filters,
                "-c:a",
                "pcm_s24le",
                str(staged),
            ],
            timeout=setting(workspace, "command_timeout_seconds", 1800),
        )
        output = probe_asset(staged)
        if abs(output["duration_ms"] - (end_ms - start_ms)) > 30:
            raise SearchError("Audio excerpt duration failed verification", 422)
        return _artifact(
            workspace,
            asset,
            staged,
            "audio_preview",
            {
                "source_start_ms": start_ms,
                "source_end_ms": end_ms,
                "source_audio_stream": 0,
                "source_channel": channel,
                "output_start_ms": 0,
                "probe": output,
                "transformation": "PCM24 excerpt; original retained",
            },
            requested_use,
            start_ms,
            end_ms,
        )


def _waveform(
    workspace, asset: dict, path: Path, requested_use="internal_review"
) -> dict:
    probe = probe_asset(path)
    _channel_filter(probe, None)
    with tempfile.TemporaryDirectory(
        prefix="case-waveform-", dir=_staging(workspace.repo)
    ) as directory:
        staged = Path(directory) / "waveform.png"
        run_command(
            [
                executable("ffmpeg"),
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-i",
                str(path),
                "-filter_complex",
                "[0:a:0]showwavespic=s=1200x240:split_channels=1:colors=0x0088cc|0xcc8800[out]",
                "-map",
                "[out]",
                "-frames:v",
                "1",
                "-threads",
                "1",
                str(staged),
            ],
            timeout=setting(workspace, "command_timeout_seconds", 1800),
        )
        return _artifact(
            workspace,
            asset,
            staged,
            "waveform",
            {
                "duration_ms": probe["duration_ms"],
                "channels": probe["audio_streams"][0]["channels"],
                "source_audio_stream": 0,
            },
            requested_use,
        )


def _reader(path: Path):
    if importlib.util.find_spec("pypdf") is None:
        raise SearchError(
            "PDF text indexing requires the case-workspace extra (pypdf)", 503
        )
    from pypdf import PdfReader

    try:
        reader = PdfReader(path)
        if reader.is_encrypted and not reader.decrypt(""):
            raise SearchError(
                "Encrypted PDF requires a separately decrypted review copy", 422
            )
        if len(reader.pages) > MAX_PDF_PAGES:
            raise SearchError("PDF exceeds the 500-page indexing budget", 422)
        return reader
    except SearchError:
        raise
    except Exception as exc:
        raise SearchError("PDF could not be read", 422) from exc


def _page_preview(
    workspace, asset: dict, path: Path, page_index: int, requested_use="internal_review"
) -> dict:
    if (
        isinstance(page_index, bool)
        or not isinstance(page_index, int)
        or page_index < 0
    ):
        raise SearchError("PDF preview needs a zero-based physical page index", 422)
    if importlib.util.find_spec("pypdfium2") is None:
        raise SearchError(
            "PDF rendering requires the case-workspace extra (pypdfium2)", 503
        )
    import pypdfium2 as pdfium

    document = page = bitmap = None
    try:
        document = pdfium.PdfDocument(str(path))
        if page_index >= len(document):
            raise SearchError("PDF page index is outside the document", 422)
        page = document[page_index]
        width, height = page.get_size()
        bitmap = page.render(scale=min(2, 1600 / max(width, height)))
        with tempfile.TemporaryDirectory(
            prefix="case-page-", dir=_staging(workspace.repo)
        ) as directory:
            staged = Path(directory) / "page.png"
            image = bitmap.to_pil()
            image.save(staged)
            meta = {
                "locator": {"kind": "page", "page_index": page_index},
                "width": image.width,
                "height": image.height,
                "pdf_width_points": width,
                "pdf_height_points": height,
                "renderer": "pypdfium2",
                "renderer_version": _runtime("pypdfium2"),
            }
            return _artifact(
                workspace, asset, staged, "document_page", meta, requested_use
            )
    except SearchError:
        raise
    except Exception as exc:
        raise SearchError("PDF page could not be rendered", 422) from exc
    finally:
        for item in (bitmap, page, document):
            if item is not None:
                item.close()


def _image(path: Path):
    from PIL import Image

    try:
        image = Image.open(path)
        if image.width * image.height > MAX_IMAGE_PIXELS:
            raise SearchError("Image exceeds the 40-megapixel review budget", 422)
        image.load()
        return image
    except SearchError:
        raise
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise SearchError("Image could not be decoded safely", 422) from exc


def _ocr(path: Path) -> list[dict]:
    if not capabilities()["ocr"]:
        raise SearchError("OCR requires pytesseract and the Tesseract executable", 503)
    import pytesseract
    from pytesseract import Output

    image = _image(path)
    try:
        data = pytesseract.image_to_data(image, output_type=Output.DICT, timeout=90)
    except RuntimeError as exc:
        raise SearchError("OCR exceeded its processing budget", 504) from exc
    result = []
    for index, text in enumerate(data["text"]):
        text = text.strip()
        confidence = float(data["conf"][index]) / 100
        if text and confidence >= 0.3:
            left, top = int(data["left"][index]), int(data["top"][index])
            result.append(
                {
                    "text": text,
                    "confidence": min(1, confidence),
                    "bbox": [
                        left,
                        top,
                        left + int(data["width"][index]),
                        top + int(data["height"][index]),
                    ],
                    "block": [
                        data["block_num"][index],
                        data["par_num"][index],
                        data["line_num"][index],
                    ],
                }
            )
    return result


def _ocr_lines(words: list[dict]) -> list[dict]:
    lines = {}
    for word in words:
        lines.setdefault(tuple(word["block"]), []).append(word)
    return [
        {
            "text": " ".join(w["text"] for w in line),
            "bbox": [
                min(w["bbox"][0] for w in line),
                min(w["bbox"][1] for w in line),
                max(w["bbox"][2] for w in line),
                max(w["bbox"][3] for w in line),
            ],
            "confidence": sum(w["confidence"] for w in line) / len(line),
        }
        for line in lines.values()
    ]


def preview_asset(
    workspace,
    asset_id: str,
    locator: dict | None = None,
    requested_use="internal_review",
) -> dict:
    """Return an authorized artifact for a physical page, audio interval or image.

    A bare audio asset returns its channel-separated waveform. A time locator
    on video returns original sound if present, otherwise a source-time frame.
    """
    asset, path = _snapshot(workspace, asset_id, requested_use)
    locator = locator or {}
    if not isinstance(locator, dict):
        raise SearchError("Preview locator must be an object", 422)
    kind = asset["asset_kind"]
    if kind == "document":
        return _page_preview(
            workspace, asset, path, locator.get("page_index", 0), requested_use
        )
    if kind in {"audio", "video"}:
        probe = probe_asset(path)
        if "start_ms" in locator and probe["has_audio"]:
            return extract_audio_preview(
                workspace,
                asset_id,
                locator["start_ms"],
                locator["end_ms"],
                locator.get("channel"),
                requested_use,
            )
        if kind == "audio":
            return _waveform(workspace, asset, path, requested_use)
        timestamp = locator.get("start_ms", 0)
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, int)
            or not 0 <= timestamp < probe["duration_ms"]
        ):
            raise SearchError("Video preview time is outside the source", 422)
        with tempfile.TemporaryDirectory(
            prefix="case-frame-", dir=_staging(workspace.repo)
        ) as directory:
            staged = Path(directory) / "frame.jpg"
            run_command(
                [
                    executable("ffmpeg"),
                    "-nostdin",
                    "-v",
                    "error",
                    "-y",
                    "-ss",
                    f"{timestamp / 1000:.3f}",
                    "-i",
                    str(path),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=w='min(1600,iw)':h='min(1600,ih)':force_original_aspect_ratio=decrease",
                    "-filter_threads",
                    "1",
                    "-q:v",
                    "3",
                    str(staged),
                ],
                timeout=setting(workspace, "command_timeout_seconds", 1800),
            )
            return _artifact(
                workspace,
                asset,
                staged,
                "case_frame",
                {"source_timestamp_ms": timestamp},
                requested_use,
            )
    if kind in {"image", "map"} and "bbox" in locator:
        image = _image(path)
        bbox = locator["bbox"]
        if (
            not isinstance(bbox, list)
            or len(bbox) != 4
            or any(
                isinstance(v, bool)
                or not isinstance(v, (int, float))
                or not math.isfinite(v)
                for v in bbox
            )
            or not 0 <= bbox[0] < bbox[2] <= 1
            or not 0 <= bbox[1] < bbox[3] <= 1
        ):
            raise SearchError("Image region is outside source pixel coordinates", 422)
        with tempfile.TemporaryDirectory(
            prefix="case-region-", dir=_staging(workspace.repo)
        ) as directory:
            staged = Path(directory) / "region.png"
            image.crop(
                [
                    round(bbox[0] * image.width),
                    round(bbox[1] * image.height),
                    round(bbox[2] * image.width),
                    round(bbox[3] * image.height),
                ]
            ).save(staged)
            return _artifact(
                workspace,
                asset,
                staged,
                "image_region",
                {"locator": locator},
                requested_use,
            )
    return workspace.repo.get("artifacts", asset["artifact_id"])


def _index_document(workspace, asset: dict, path: Path) -> dict:
    reader = _reader(path)
    labels = reader.page_labels
    count, missing, errors, characters = 0, [], [], 0
    for page_index, page in enumerate(reader.pages):
        _current(workspace, asset)
        try:
            text = (page.extract_text() or "").strip()
        except Exception as exc:
            raise SearchError("PDF native text extraction failed", 422) from exc
        characters += len(text)
        if characters > MAX_TEXT_CHARACTERS:
            raise SearchError("PDF text exceeds the indexing character budget", 422)
        label = str(labels[page_index])
        origin, confidence, rendered = "native_pdf", None, None
        dimensions = [0, 0, float(page.mediabox.width), float(page.mediabox.height)]
        if not text and setting(workspace, "ocr_enabled", False):
            try:
                rendered = _page_preview(workspace, asset, path, page_index, "analysis")
                words = _ocr(verified_artifact_path(workspace.repo, rendered))
                lines = _ocr_lines(words)
                text = "\n".join(line["text"] for line in lines)
                origin = "ocr"
                confidence = (
                    sum(w["confidence"] for w in words) / len(words) if words else None
                )
                for line in lines:
                    # Normalize top-left region coordinates independently of render resolution.
                    box = line["bbox"]
                    meta = rendered["metadata"]
                    bbox = [
                        box[0] / meta["width"],
                        box[1] / meta["height"],
                        box[2] / meta["width"],
                        box[3] / meta["height"],
                    ]
                    _unit(
                        workspace,
                        asset,
                        line["text"],
                        "page",
                        {
                            "kind": "page",
                            "page_index": page_index,
                            "page_label": label,
                            "bbox": bbox,
                        },
                        "document_region",
                        origin,
                        confidence=line["confidence"],
                        artifact_id=rendered["id"],
                        metadata={
                            "coordinate_units": "normalized_top_left",
                            "ocr_model": "tesseract",
                            "reviewed": False,
                        },
                    )
                    count += 1
            except SearchError as exc:
                if exc.status_code not in {503, 504}:
                    raise
                errors.append(
                    {"page_index": page_index, "feature": "ocr", "reason": str(exc)}
                )
        if not text:
            missing.append(page_index)
        else:
            count += 1
            if origin == "native_pdf":
                for start, end in _text_spans(text, 1200):
                    _current(workspace, asset)
                    _unit(
                        workspace,
                        asset,
                        text[start:end],
                        "page",
                        {
                            "kind": "page",
                            "page_index": page_index,
                            "page_label": label,
                            "text_start": start,
                            "text_end": end,
                        },
                        "document_passage",
                        origin,
                        metadata={
                            "page_label_origin": "pdf_page_labels"
                            if "/PageLabels" in reader.trailer["/Root"]
                            else "physical_page_number"
                        },
                    )
                    count += 1
        _current(workspace, asset)
        workspace.record_document_page(
            asset["id"],
            page_index,
            label,
            text,
            origin,
            confidence=confidence,
            render_artifact_id=rendered["id"] if rendered else None,
            metadata={
                "physical_page_number": page_index + 1,
                "asset_version_id": asset["asset_version_id"],
                "input_sha256": asset["sha256"],
                "bbox": dimensions,
                "coordinate_units": "pdf_points",
                "page_label_origin": "pdf_page_labels"
                if "/PageLabels" in reader.trailer["/Root"]
                else "physical_page_number",
                "needs_ocr": not bool(text),
            },
        )
    return {
        "pages": len(reader.pages),
        "evidence_units": count,
        "pages_needing_ocr": missing,
        "state": "partial" if missing else "indexed",
        "unavailable": errors,
    }


def _text_spans(text: str, maximum: int):
    """Bounded contiguous passages with exact offsets in canonical page text."""
    start = 0
    while start < len(text):
        end = min(start + maximum, len(text))
        if end < len(text):
            boundary = max(
                text.rfind("\n", start + maximum // 2, end),
                text.rfind(" ", start + maximum // 2, end),
            )
            if boundary > start:
                end = boundary
        left, right = start, end
        while left < right and text[left].isspace():
            left += 1
        while right > left and text[right - 1].isspace():
            right -= 1
        if right > left:
            yield left, right
        start = end
        while start < len(text) and text[start].isspace():
            start += 1


def _confidence(value, name="confidence"):
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= 1
    ):
        raise SearchError(
            f"Transcript {name} must be a finite value between zero and one", 422
        )
    return value


def _times(
    record: dict, duration_ms: int, allow_null=False
) -> tuple[int | None, int | None]:
    start, end = record.get("start_ms"), record.get("end_ms")
    if "start_ms" not in record:
        start = record.get("start")
        end = record.get("end")
        if start is not None and end is not None:
            if any(
                isinstance(v, bool)
                or not isinstance(v, (int, float))
                or not math.isfinite(v)
                for v in (start, end)
            ):
                raise SearchError("Transcript times must be finite seconds", 422)
            start, end = round(start * 1000), round(end * 1000)
    if start is None and end is None and allow_null:
        return None, None
    if (
        any(isinstance(v, bool) or not isinstance(v, int) for v in (start, end))
        or not 0 <= start <= end <= duration_ms
        or (not allow_null and start == end)
    ):
        raise SearchError(
            "Transcript times must fall within the immutable source audio", 422
        )
    return start, end


def _label(value, field):
    if value is None:
        return None
    if (
        field == "channel"
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    ):
        return value
    if not isinstance(value, str) or len(value) > 250:
        raise SearchError(f"Transcript {field} must be a bounded label", 422)
    return value


def import_whisperx(
    workspace, asset_id: str, payload: dict, scope="source", script_asset_id=None
) -> dict:
    """Import ASR/alignment JSON, preserving unaligned words and version pins.

    Required audio_sha256 is the immutable original's digest. For narration,
    script_sha256 and a same-case script asset are also required. Speaker labels
    from diarization are retained as unverified labels, never named identities.
    """
    asset, path = _snapshot(workspace, asset_id)
    if scope not in {"source", "narration"} or not isinstance(payload, dict):
        raise SearchError("Transcript scope must be source or narration", 422)
    try:
        raw_input = json.loads(json_text(payload))
        if len(json_text(raw_input)) > 16_000_000:
            raise SearchError("Transcript JSON exceeds the import budget", 422)
    except (TypeError, ValueError) as exc:
        raise SearchError("Transcript import must be finite JSON data", 422) from exc
    if payload.get("audio_sha256") != asset["sha256"]:
        raise SearchError(
            "Transcript audio hash does not match this asset version", 409
        )
    if (
        payload.get("asset_version_id", asset["asset_version_id"])
        != asset["asset_version_id"]
    ):
        raise SearchError("Transcript references a superseded audio version", 409)
    probe = probe_asset(path)
    _channel_filter(probe, None)
    script = None
    if scope == "narration":
        if not script_asset_id:
            raise SearchError("Narration alignment requires a script asset", 422)
        script, _ = _snapshot(workspace, script_asset_id)
        if script["case_id"] != asset["case_id"] or script["asset_kind"] != "script":
            raise SearchError("Narration script must belong to this case", 422)
        if payload.get("script_sha256") != script["sha256"]:
            raise SearchError(
                "Alignment script hash does not match the current script", 409
            )
    raw_segments = payload.get("segments", [])
    if not isinstance(raw_segments, list) or len(raw_segments) > 20_000:
        raise SearchError("Transcript segment count exceeds the indexing budget", 422)
    segments, nested_words = [], []
    for segment in raw_segments:
        if not isinstance(segment, dict) or not isinstance(
            segment.get("text", ""), str
        ):
            raise SearchError(
                "Transcript segments must contain text and source times", 422
            )
        start, end = _times(segment, probe["duration_ms"])
        segments.append(
            {
                "text": segment.get("text", "").strip(),
                "start_ms": start,
                "end_ms": end,
                "speaker": _label(segment.get("speaker"), "speaker"),
                "channel": _label(segment.get("channel"), "channel"),
            }
        )
        nested_words.extend(segment.get("words", []))
    raw_words = payload.get("word_segments", payload.get("words", nested_words))
    if not isinstance(raw_words, list) or len(raw_words) > MAX_TRANSCRIPT_WORDS:
        raise SearchError("Transcript word count exceeds the indexing budget", 422)
    words = []
    for index, word in enumerate(raw_words):
        if not isinstance(word, dict) or not isinstance(
            word.get("word", word.get("text")), str
        ):
            raise SearchError("Transcript words must contain text", 422)
        if not word.get("word", word.get("text")).strip():
            raise SearchError("Transcript words must contain nonempty text", 422)
        start, end = _times(word, probe["duration_ms"], allow_null=True)
        words.append(
            {
                "word_index": index,
                "text": word.get("word", word.get("text")),
                "start_ms": start,
                "end_ms": end,
                "speaker": _label(word.get("speaker"), "speaker"),
                "channel": _label(word.get("channel"), "channel"),
                "confidence": _confidence(
                    word.get("confidence", word.get("probability"))
                ),
                "alignment_confidence": _confidence(
                    word.get("alignment_confidence", word.get("score")),
                    "alignment confidence",
                ),
                "metadata": {
                    "alignment_status": "unaligned" if start is None else "aligned",
                    "speaker_identity_verified": False,
                },
            }
        )
    if not segments and not words:
        raise SearchError("Transcript import contains no segments or words", 422)
    if (
        sum(len(s["text"]) for s in segments) + sum(len(w["text"]) for w in words)
        > MAX_TEXT_CHARACTERS
    ):
        raise SearchError("Transcript text exceeds the indexing budget", 422)
    origin = payload.get("origin", "whisperx_import")
    reviewed_by = payload.get("reviewed_by")
    if origin == "reviewed_transcript" and (
        not isinstance(reviewed_by, str) or not reviewed_by.strip()
    ):
        raise SearchError("Reviewed transcript requires an explicit reviewer", 422)
    record = {
        "schema_version": VERSION,
        "scope": scope,
        "audio_sha256": asset["sha256"],
        "asset_version_id": asset["asset_version_id"],
        "segments": segments,
        "words": words,
        "origin": origin,
        "reviewed_by": reviewed_by,
        "model_name": payload.get("model_name", "unspecified"),
        "model_revision": payload.get("model_revision", "unspecified"),
        "runtime_version": payload.get("runtime_version", "unspecified"),
        "language": payload.get("language"),
        "speaker_identity_verified": False,
        "unaligned_word_count": sum(w["start_ms"] is None for w in words),
        "raw_input": raw_input,
    }
    if script:
        record.update(
            script_asset_id=script["id"],
            script_asset_version_id=script["asset_version_id"],
            script_sha256=script["sha256"],
        )
        _current(workspace, script)
    artifact = _json_artifact(
        workspace,
        asset,
        record,
        "narration_alignment" if scope == "narration" else "source_transcript",
    )
    record["transcript_artifact_id"] = artifact["id"]
    _current(workspace, asset)
    workspace.record_transcript_words(asset_id, artifact["id"], words)
    workspace.store_transcript(asset_id, record)
    if scope == "source":
        for segment in segments:
            if segment["text"]:
                _unit(
                    workspace,
                    asset,
                    segment["text"],
                    "time",
                    {
                        "kind": "time",
                        "start_ms": segment["start_ms"],
                        "end_ms": segment["end_ms"],
                        "speaker": segment["speaker"],
                        "channel": segment["channel"],
                    },
                    "transcript_segment",
                    origin,
                    artifact_id=artifact["id"],
                    metadata={
                        "transcript_artifact_id": artifact["id"],
                        "reviewed_by": reviewed_by,
                        "speaker_identity_verified": False,
                    },
                )
        for word in words:
            locator = {
                "kind": "word",
                "word_start_index": word["word_index"],
                "word_end_index": word["word_index"] + 1,
                "transcript_artifact_id": artifact["id"],
            }
            if word["start_ms"] is not None:
                locator.update(start_ms=word["start_ms"], end_ms=word["end_ms"])
            _unit(
                workspace,
                asset,
                word["text"],
                "word",
                locator,
                "transcript_word",
                origin,
                confidence=word["confidence"],
                artifact_id=artifact["id"],
                metadata=word["metadata"],
            )
        if origin == "reviewed_transcript":
            _current(workspace, asset)
            # Retain original transcripts while excluding superseded ASR from retrieval.
            with workspace.repo.connect() as connection:
                connection.execute(
                    "UPDATE evidence_units SET is_active=0 WHERE asset_id=? AND asset_version_id=? AND unit_kind IN ('transcript_segment','transcript_word') AND artifact_id!=?",
                    (asset_id, asset["asset_version_id"], artifact["id"]),
                )
    return {
        "asset_id": asset_id,
        "scope": scope,
        "artifact_id": artifact["id"],
        "transcript_artifact_id": artifact["id"],
        "segments": len(segments),
        "words": len(words),
        "unaligned_words": record["unaligned_word_count"],
        "audio_sha256": asset["sha256"],
        "script_sha256": script["sha256"] if script else None,
    }


def import_reviewed_transcript(
    workspace, asset_id: str, payload: dict, reviewed_by: str
) -> dict:
    return import_whisperx(
        workspace,
        asset_id,
        {**payload, "origin": "reviewed_transcript", "reviewed_by": reviewed_by},
    )


def _analysis_audio(workspace, asset: dict, path: Path) -> dict:
    with tempfile.TemporaryDirectory(
        prefix="case-asr-", dir=_staging(workspace.repo)
    ) as directory:
        staged = Path(directory) / "analysis.wav"
        run_command(
            [
                executable("ffmpeg"),
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-i",
                str(path),
                "-map",
                "0:a:0",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-map_metadata",
                "-1",
                str(staged),
            ],
            timeout=setting(workspace, "command_timeout_seconds", 1800),
        )
        return _artifact(
            workspace,
            asset,
            staged,
            "analysis_audio",
            {
                "sample_rate": 16000,
                "channels": 1,
                "source_audio_stream": 0,
                "transformation": "mono downmix for speech analysis; original channels retained",
            },
        )


def _model_identity(model_name, revision):
    path = Path(model_name)
    if path.is_dir():
        digest = hashlib.sha256()
        files = [
            path / name
            for name in ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt")
            if (path / name).is_file()
        ]
        for file in files:
            digest.update(file.name.encode())
            digest.update(sha256_file(file).encode())
        return (
            path.name,
            "sha256:" + digest.hexdigest() if files else "unverified-local-model",
        )
    return path.name if path.is_absolute() else model_name, revision


def transcribe_audio(workspace, asset_id: str) -> dict:
    asset, path = _snapshot(workspace, asset_id)
    if asset.get("metadata", {}).get("role") == "production":
        raise SearchError(
            "Production narration requires explicit script alignment, not source evidence indexing",
            422,
        )
    probe = probe_asset(path)
    _channel_filter(probe, None)
    if importlib.util.find_spec("faster_whisper") is None:
        raise SearchError("Source speech indexing requires faster-whisper", 503)
    from faster_whisper import WhisperModel

    model_name = setting(workspace, "asr_model", "small")
    try:
        model = WhisperModel(
            model_name,
            device="cpu",
            compute_type="int8",
            local_files_only=setting(workspace, "local_models_only", True),
        )
    except (ValueError, OSError, RuntimeError) as exc:
        raise SearchError(
            "Source ASR model is unavailable locally; configure the selected model", 503
        ) from exc
    display_model, model_revision = _model_identity(
        model_name, setting(workspace, "asr_revision", "main")
    )
    analysis = _analysis_audio(workspace, asset, path)
    raw, info = model.transcribe(
        str(verified_artifact_path(workspace.repo, analysis)),
        vad_filter=True,
        beam_size=5,
        word_timestamps=True,
    )
    segments = []
    for segment in raw:
        if len(segments) >= 20_000:
            raise SearchError("ASR exceeds the transcript segment budget", 422)
        segments.append(
            {
                "start": segment.start,
                "end": segment.end,
                "text": segment.text,
                "words": [
                    {
                        "word": w.word,
                        "start": w.start,
                        "end": w.end,
                        "probability": w.probability,
                    }
                    for w in (segment.words or [])
                ],
            }
        )
    if not segments:
        return {
            "asset_id": asset_id,
            "segments": 0,
            "words": 0,
            "state": "no_speech",
            "analysis_artifact_id": analysis["id"],
        }
    result = import_whisperx(
        workspace,
        asset_id,
        {
            "audio_sha256": asset["sha256"],
            "asset_version_id": asset["asset_version_id"],
            "segments": segments,
            "language": info.language,
            "origin": "asr",
            "model_name": display_model,
            "model_revision": model_revision,
            "runtime_version": _runtime("faster-whisper"),
            "analysis_artifact_id": analysis["id"],
            "analysis_sha256": analysis["sha256"],
        },
    )
    if asset["asset_kind"] == "video":
        from .captions import ingest_captions

        _current(workspace, asset)
        caption = ingest_captions(
            workspace.repo,
            asset["source_id"],
            [
                {
                    "start_ms": round(s["start"] * 1000),
                    "end_ms": round(s["end"] * 1000),
                    "text": s["text"],
                }
                for s in segments
            ],
            language=info.language,
            kind="asr",
            provider="faster-whisper:" + display_model,
        )
        caption_metadata = {
            "model_name": display_model,
            "model_revision": model_revision,
            "runtime_version": _runtime("faster-whisper"),
            "source_sha256": asset["sha256"],
            "source_artifact_id": asset["artifact_id"],
            "case_asset_version_id": asset["asset_version_id"],
            "transcript_artifact_id": result["artifact_id"],
        }
        with workspace.repo.connect() as connection:
            connection.execute(
                "UPDATE captions SET metadata_json=? WHERE id=?",
                (json_text(caption_metadata), caption["id"]),
            )
        result["caption_id"] = caption["id"]
    return {**result, "analysis_artifact_id": analysis["id"]}


def align_audio(
    workspace,
    asset_id: str,
    script_asset_id=None,
    scope="source",
    payload=None,
    case_id=None,
    asset_version_id=None,
    input_sha256=None,
    script_asset_version_id=None,
    input_script_sha256=None,
) -> dict:
    """Import existing alignment, or run optional local WhisperX forced alignment."""
    asset, path = _snapshot(workspace, asset_id)
    if (
        (case_id is not None and case_id != asset["case_id"])
        or (
            asset_version_id is not None
            and asset_version_id != asset["asset_version_id"]
        )
        or (input_sha256 is not None and input_sha256 != asset["sha256"])
    ):
        raise SearchError("Alignment input was superseded", 409)
    if script_asset_id:
        pinned_script, _ = _snapshot(workspace, script_asset_id)
        if (
            script_asset_version_id is not None
            and script_asset_version_id != pinned_script["asset_version_id"]
        ) or (
            input_script_sha256 is not None
            and input_script_sha256 != pinned_script["sha256"]
        ):
            raise SearchError("Alignment script input was superseded", 409)
    elif script_asset_version_id is not None or input_script_sha256 is not None:
        raise SearchError("Alignment script pins require a script asset", 422)
    if payload is not None:
        return import_whisperx(workspace, asset_id, payload, scope, script_asset_id)
    if importlib.util.find_spec("whisperx") is None:
        raise SearchError(
            "WhisperX runtime is unavailable; import reviewed WhisperX JSON or install an isolated runtime",
            503,
        )
    checkpoint = setting(workspace, "whisperx_alignment_checkpoint", "")
    if not checkpoint or not Path(checkpoint).is_dir():
        raise SearchError(
            "WhisperX alignment requires an explicitly configured local checkpoint directory",
            503,
        )
    language = setting(workspace, "whisperx_language", "en")
    probe = probe_asset(path)
    if scope == "narration":
        if not script_asset_id:
            raise SearchError("Narration alignment requires a script asset", 422)
        script, script_path = _snapshot(workspace, script_asset_id)
        if script["asset_kind"] != "script" or script["case_id"] != asset["case_id"]:
            raise SearchError("Narration script must belong to this case", 422)
        text = _read_text(script_path)
        segments = [{"start": 0, "end": probe["duration_ms"] / 1000, "text": text}]
    elif scope == "source":
        result = transcribe_audio(workspace, asset_id)
        if not result.get("artifact_id"):
            raise SearchError("No source speech is available for alignment", 422)
        raw = json.loads(
            verified_artifact_path(
                workspace.repo, workspace.repo.get("artifacts", result["artifact_id"])
            ).read_text()
        )
        language = raw.get("language") or language
        segments = [
            {
                "start": s["start_ms"] / 1000,
                "end": s["end_ms"] / 1000,
                "text": s["text"],
            }
            for s in raw["segments"]
        ]
    else:
        raise SearchError("Alignment scope must be source or narration", 422)
    analysis = _analysis_audio(workspace, asset, path)
    import whisperx

    try:
        model, metadata = whisperx.load_align_model(
            language_code=language,
            device="cpu",
            model_name=str(Path(checkpoint).resolve()),
        )
        audio = whisperx.load_audio(
            str(verified_artifact_path(workspace.repo, analysis))
        )
        result = whisperx.align(
            segments, model, metadata, audio, "cpu", return_char_alignments=False
        )
    except Exception as exc:
        raise SearchError(
            "Local WhisperX alignment failed; verify checkpoint and language support",
            422,
        ) from exc
    model_hash = hashlib.sha256()
    for file in sorted(p for p in Path(checkpoint).rglob("*") if p.is_file()):
        model_hash.update(str(file.relative_to(checkpoint)).encode())
        model_hash.update(sha256_file(file).encode())
    imported = {
        **result,
        "audio_sha256": asset["sha256"],
        "asset_version_id": asset["asset_version_id"],
        "model_name": "WhisperX:" + Path(checkpoint).name,
        "model_revision": "sha256:" + model_hash.hexdigest(),
        "runtime_version": _runtime("whisperx"),
        "language": language,
        "origin": "forced_alignment",
    }
    if scope == "narration":
        _current(workspace, script)
        imported["script_sha256"] = script["sha256"]
    return import_whisperx(workspace, asset_id, imported, scope, script_asset_id)


def _read_text(path: Path) -> str:
    if path.stat().st_size > MAX_TEXT_CHARACTERS * 4:
        raise SearchError("Text asset exceeds the indexing budget", 422)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeError as exc:
        raise SearchError("Text assets must use UTF-8 encoding", 422) from exc
    if len(text) > MAX_TEXT_CHARACTERS:
        raise SearchError("Text asset exceeds the indexing character budget", 422)
    return text


def _index_image(workspace, asset: dict, path: Path) -> dict:
    image = _image(path)
    _unit(
        workspace,
        asset,
        f"Image {image.width} × {image.height} pixels",
        "metadata",
        {"kind": "metadata", "field": "image_dimensions"},
        "asset_metadata",
        "image_header",
        metadata={"width": image.width, "height": image.height, "mode": image.mode},
    )
    unavailable, units, vectors = [], 1, 0
    if setting(workspace, "ocr_enabled", False):
        try:
            for line in _ocr_lines(_ocr(path)):
                _current(workspace, asset)
                _unit(
                    workspace,
                    asset,
                    line["text"],
                    "image",
                    {
                        "kind": "image",
                        "bbox": [
                            line["bbox"][0] / image.width,
                            line["bbox"][1] / image.height,
                            line["bbox"][2] / image.width,
                            line["bbox"][3] / image.height,
                        ],
                    },
                    "image_region",
                    "ocr",
                    confidence=line["confidence"],
                    metadata={
                        "coordinate_units": "normalized_top_left",
                        "model_name": "tesseract",
                        "model_revision": _runtime("pytesseract"),
                        "reviewed": False,
                    },
                )
                units += 1
        except SearchError as exc:
            if exc.status_code not in {503, 504}:
                raise
            unavailable.append({"feature": "ocr", "reason": str(exc)})
    if setting(workspace, "visual_enabled", False):
        try:
            from .visual import _vision_model
            import torch

            model, preprocess, _, model_name, revision = _vision_model(workspace)
            with torch.no_grad():
                vector = model.encode_image(
                    preprocess(image.convert("RGB")).unsqueeze(0)
                )
                vector = vector / vector.norm(dim=-1, keepdim=True)
                values = vector[0].tolist()
            _current(workspace, asset)
            identity = (
                "casevision_"
                + hashlib.sha256(
                    f"{asset['asset_version_id']}|{model_name}|{revision}".encode()
                ).hexdigest()[:32]
            )
            with workspace.repo.connect() as connection:
                connection.execute(
                    "INSERT OR IGNORE INTO embeddings(id,entity_type,entity_id,source_id,modality,model_name,model_revision,dimensions,vector_json,input_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        identity,
                        "case_asset_version",
                        asset["asset_version_id"],
                        asset["source_id"],
                        "image",
                        model_name,
                        revision,
                        len(values),
                        json_text(values),
                        asset["sha256"],
                        now(),
                    ),
                )
            vectors = 1
        except SearchError as exc:
            if exc.status_code != 503:
                raise
            unavailable.append({"feature": "visual_embedding", "reason": str(exc)})
    return {
        "evidence_units": units,
        "vectors": vectors,
        "state": "partial" if unavailable else "indexed",
        "unavailable": unavailable,
    }


def _validate_geometry(geometry: dict | None) -> None:
    if geometry is None:
        return
    if not isinstance(geometry, dict) or geometry.get("type") not in {
        "Point",
        "MultiPoint",
        "LineString",
        "MultiLineString",
        "Polygon",
        "MultiPolygon",
        "GeometryCollection",
    }:
        raise SearchError("Map geometry must be a typed GeoJSON geometry", 422)

    def position(value):
        if (
            not isinstance(value, list)
            or len(value) < 2
            or any(
                isinstance(v, bool)
                or not isinstance(v, (int, float))
                or not math.isfinite(v)
                for v in value
            )
        ):
            raise SearchError(
                "Map positions need at least two finite supplied coordinates", 422
            )

    def sequence(value, validator, minimum=1):
        if not isinstance(value, list) or len(value) < minimum:
            raise SearchError(
                "Map coordinate shape does not match its geometry type", 422
            )
        for item in value:
            validator(item)

    def line(value):
        sequence(value, position, 2)

    def ring(value):
        sequence(value, position, 4)
        if value[0] != value[-1]:
            raise SearchError("GeoJSON polygon rings must be closed", 422)

    def polygon(value):
        sequence(value, ring)

    kind, coordinates = geometry["type"], geometry.get("coordinates")
    if kind == "GeometryCollection":
        geometries = geometry.get("geometries")
        if not isinstance(geometries, list) or len(geometries) > 10_000:
            raise SearchError("Map geometry collection exceeds its budget", 422)
        # Avoid unbounded nested collections before recursive validation.
        if len(json_text(geometry)) > MAX_TEXT_CHARACTERS or any(
            g.get("type") == "GeometryCollection"
            for g in geometries
            if isinstance(g, dict)
        ):
            raise SearchError(
                "Nested map geometry collections exceed the supported review depth", 422
            )
        for item in geometries:
            _validate_geometry(item)
    elif kind == "Point":
        position(coordinates)
    elif kind == "MultiPoint":
        sequence(coordinates, position)
    elif kind == "LineString":
        line(coordinates)
    elif kind == "MultiLineString":
        sequence(coordinates, line)
    elif kind == "Polygon":
        polygon(coordinates)
    elif kind == "MultiPolygon":
        sequence(coordinates, polygon)


def _index_map(workspace, asset: dict, path: Path) -> dict:
    if Path(asset["filename"]).suffix.lower() in {
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".tif",
        ".tiff",
    }:
        result = _index_image(workspace, asset, path)
        return {
            **result,
            "map_coordinates_available": False,
            "coordinates_inferred": False,
        }
    try:
        document = json.loads(_read_text(path))
    except ValueError as exc:
        raise SearchError("Map asset must contain GeoJSON", 422) from exc
    if not isinstance(document, dict):
        raise SearchError("Map asset must contain a typed GeoJSON object", 422)
    features = (
        document.get("features")
        if document.get("type") == "FeatureCollection"
        else [document]
    )
    if not isinstance(features, list) or len(features) > 10_000:
        raise SearchError("Map exceeds the 10000-feature indexing budget", 422)
    normalized = []
    for index, feature in enumerate(features):
        if not isinstance(feature, dict):
            raise SearchError("Map features must be objects", 422)
        geometry = (
            feature.get("geometry") if feature.get("type") == "Feature" else feature
        )
        _validate_geometry(geometry)
        properties = feature.get("properties") or {}
        if not isinstance(properties, dict):
            raise SearchError("Map properties must be objects", 422)
        normalized.append((index, feature, geometry, properties))
    # Validate the complete document before registering any units.
    for index, feature, geometry, properties in normalized:
        _current(workspace, asset)
        text = (
            "; ".join(
                f"{k}: {v}"
                for k, v in properties.items()
                if isinstance(v, (str, int, float, bool))
            )
            or f"GeoJSON {geometry['type'] if geometry else 'null geometry'} feature {index}"
        )
        _unit(
            workspace,
            asset,
            text,
            "metadata",
            {"kind": "metadata", "field": f"features/{index}"},
            "map_feature",
            "geojson",
            metadata={
                "feature_index": index,
                "feature_id": feature.get("id"),
                "geometry": geometry,
                "properties": properties,
                "supplied_crs": document.get("crs"),
                "coordinates_inferred": False,
            },
        )
    return {
        "features": len(features),
        "evidence_units": len(features),
        "state": "indexed",
        "unavailable": [],
    }


def index_asset(workspace, asset_id: str) -> dict:
    asset, path = _snapshot(workspace, asset_id)
    kind = asset["asset_kind"]
    if kind == "document":
        result = _index_document(workspace, asset, path)
    elif kind == "image":
        result = _index_image(workspace, asset, path)
    elif kind == "map":
        result = _index_map(workspace, asset, path)
    elif kind in {"audio", "video"}:
        probe = probe_asset(path)
        if probe["duration_ms"] > setting(
            workspace, "max_source_duration_ms", 14_400_000
        ):
            raise SearchError(
                "Case audio/video exceeds the indexing duration budget", 422
            )
        from .discovery import persist_metadata

        source = workspace.repo.get("sources", asset["source_id"])
        if source.get("duration_ms") != probe["duration_ms"]:
            _current(workspace, asset)
            persist_metadata(
                workspace.repo,
                asset["source_id"],
                {
                    **source.get("metadata", {}),
                    "title": source["title"],
                    "description": source["description"],
                    "creator_name": source["creator_name"],
                    "creator_id": source["creator_id"],
                    "asset_kind": kind,
                    "duration_ms": probe["duration_ms"],
                },
                extractor_name="case-ffprobe",
            )
        _unit(
            workspace,
            asset,
            json_text(probe),
            "metadata",
            {"kind": "metadata", "field": "media_probe"},
            "asset_metadata",
            "ffprobe",
            metadata=probe,
        )
        result = {
            "probe": probe,
            "evidence_units": 1,
            "state": "indexed",
            "unavailable": [],
        }
        if probe["has_audio"]:
            waveform = _waveform(workspace, asset, path, "analysis")
            result["waveform_artifact_id"] = waveform["id"]
            with workspace.repo.connect() as connection:
                captions = [
                    row["id"]
                    for row in connection.execute(
                        "SELECT id FROM captions WHERE source_id=? AND is_active=1",
                        (asset["source_id"],),
                    )
                ]
                reviewed = connection.execute(
                    "SELECT 1 FROM case_transcripts WHERE asset_version_id=? AND scope='source' AND json_extract(record_json,'$.origin')='reviewed_transcript' LIMIT 1",
                    (asset["asset_version_id"],),
                ).fetchone()
            if asset.get("metadata", {}).get("role") == "production":
                result["narration_alignment_required"] = True
            elif reviewed:
                result["reviewed_transcript_retained"] = True
            elif kind == "video" and captions:
                result["existing_caption_ids"] = captions
            else:
                try:
                    result["transcript"] = transcribe_audio(workspace, asset_id)
                except SearchError as exc:
                    if exc.status_code != 503:
                        raise
                    result["unavailable"].append({"feature": "asr", "reason": str(exc)})
                    result["state"] = "partial"
        if probe["has_video"]:
            result["preview_artifact_id"] = preview_asset(
                workspace, asset_id, requested_use="analysis"
            )["id"]
    elif kind in {"script", "transcript", "production"}:
        text = _read_text(path)
        count, offset = 0, 0
        for paragraph in text.splitlines(keepends=True):
            if paragraph.strip():
                _current(workspace, asset)
                _unit(
                    workspace,
                    asset,
                    paragraph.strip(),
                    "script",
                    {
                        "kind": "script",
                        "char_start": offset,
                        "char_end": offset + len(paragraph),
                    },
                    "production_text"
                    if kind != "transcript"
                    else "unbound_transcript_text",
                    "utf8_text",
                    metadata={
                        "timestamps_available": False,
                        "assertions_reviewed": False,
                    },
                )
                count += 1
            offset += len(paragraph)
        result = {"evidence_units": count, "state": "indexed", "unavailable": []}
    else:
        raise SearchError("This asset type has no case indexing adapter", 422)
    _current(workspace, asset)
    result.update(
        asset_id=asset_id,
        asset_version_id=asset["asset_version_id"],
        input_sha256=asset["sha256"],
        asset_kind=kind,
        capabilities=capabilities(workspace),
        pipeline_version=VERSION,
    )
    metadata = {"indexing": result}
    if "probe" in result:
        metadata["duration_ms"] = result["probe"]["duration_ms"]
    workspace.set_asset_state(asset_id, result["state"], metadata=metadata)
    return result
