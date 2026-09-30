"""Scene-aware keyframes, versioned OCR, and optional local OpenCLIP vectors."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from app.models.search import SearchError

from .acquisition import download_source
from .media import PIPELINE_VERSION, _staging, executable, probe_media, promote_artifact, run_command, setting, verified_artifact_path
from .policy import authorize


def _identifier(prefix: str, *parts) -> str:
    return prefix + hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:32]


def scene_sample_times(path: Path, duration_ms: int, interval_seconds: int = 15, maximum: int = 240, timeout: int = 1800) -> list[tuple[int, int, int]]:
    """Use FFmpeg scene scores plus sparse static-scene coverage.

    Each tuple is (representative timestamp, scene start, scene end). The
    entire set is capped before frame extraction, OCR, and vector inference.
    """
    result = run_command([
        executable("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "info", "-threads", "2",
        "-i", str(path), "-vf", "scale=320:-2,select='gt(scene,0.35)',showinfo", "-filter_threads", "1",
        "-an", "-f", "null", "-",
    ], timeout=timeout)
    boundaries = {0, duration_ms}
    for match in re.finditer(r"pts_time:([0-9.]+)", result.stderr.decode("utf-8", "replace")):
        stamp = round(float(match.group(1)) * 1000)
        if 0 < stamp < duration_ms:
            boundaries.add(stamp)
    ordered = sorted(boundaries)
    candidates = []
    for start, end in zip(ordered, ordered[1:]):
        # A frame comfortably inside each scene avoids transition blends.
        midpoint = min(end - 1, start + min(500, (end - start) // 2))
        candidates.append((midpoint, start, end))
        interval = max(1000, interval_seconds * 1000)
        for timestamp in range(start + interval, end, interval):
            candidates.append((timestamp, start, end))
    candidates = sorted(set(candidates))
    if len(candidates) > maximum:
        # Keep uniform coverage across the source rather than truncating early.
        indices = {round(i * (len(candidates) - 1) / max(maximum - 1, 1)) for i in range(maximum)}
        candidates = [candidates[i] for i in sorted(indices)]
    return candidates


def _perceptual_hash(path: Path) -> str:
    from PIL import Image

    with Image.open(path) as frame:
        values = list(frame.convert("L").resize((9, 8)).getdata())
        average = frame.convert("RGB").resize((1, 1)).getpixel((0, 0))
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | int(values[row * 9 + col] > values[row * 9 + col + 1])
    # dHash alone collapses uniform slides of different colors. Append coarse
    # mean color so scene changes remain distinct even without edges.
    color = (average[0] // 16 << 8) | (average[1] // 16 << 4) | (average[2] // 16)
    return f"{bits:016x}{color:03x}"


def _ocr(path: Path, service) -> tuple[str, float, str, str]:
    if importlib.util.find_spec("pytesseract") is None or not shutil.which("tesseract"):
        raise SearchError("OCR requires pytesseract and the Tesseract system executable", status_code=503)
    import pytesseract
    from PIL import Image

    with Image.open(path) as image:
        result = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT,
                                          timeout=min(60, setting(service, "command_timeout_seconds", 1800)))
    words, confidence = [], []
    for text, value in zip(result.get("text", []), result.get("conf", [])):
        text = text.strip()
        if text and float(value) >= 30:
            words.append(text)
            confidence.append(float(value) / 100)
    return " ".join(words), sum(confidence) / len(confidence) if confidence else 0, "tesseract", str(pytesseract.get_tesseract_version()).splitlines()[0]


_vision_cache_lock = threading.Lock()


def _vision_model(service):
    if importlib.util.find_spec("open_clip") is None:
        raise SearchError("Visual embeddings require the search-visual extra (open_clip_torch)", status_code=503)
    name = setting(service, "vision_model", "ViT-B-32")
    checkpoint = setting(service, "vision_checkpoint", "") or os.environ.get("TARGETED_SEARCH_VISION_CHECKPOINT", "")
    if setting(service, "local_models_only", True):
        if not checkpoint or not Path(checkpoint).is_file():
            raise SearchError("Visual embeddings need a local OpenCLIP checkpoint configured in targeted_search_vision_checkpoint", status_code=503)
        pretrained = str(Path(checkpoint).resolve())
    else:
        pretrained = checkpoint or setting(service, "vision_pretrained", "laion2b_s34b_b79k")
    path = Path(pretrained)
    if path.is_file():
        path = path.resolve()
        info = path.stat()
        key = (name, str(path), info.st_mtime_ns, info.st_size, True)
    else:
        key = (name, pretrained, 0, 0, False)
    # Prevent concurrent calls from loading two copies of a large CPU model.
    with _vision_cache_lock:
        return _cached_vision_model(*key)


@lru_cache(maxsize=2)
def _cached_vision_model(name, pretrained, mtime_ns, byte_size, local_checkpoint):
    import open_clip

    revision = "sha256:" + _file_digest(Path(pretrained)) if local_checkpoint else "checkpoint:" + pretrained
    model, _, preprocess = open_clip.create_model_and_transforms(name, pretrained=pretrained, device="cpu")
    model.eval()
    return model, preprocess, open_clip.get_tokenizer(name), name, revision


def _file_digest(path: Path) -> str:
    from .media import sha256_file

    return sha256_file(path)


def visual_index(service, payload: dict) -> dict:
    repo = service.repo
    source_id = payload["source_id"]
    requested_use = payload.get("requested_use", "analysis")
    approval_id = payload.get("approval_id")
    if not approval_id:
        raise SearchError("Visual indexing requires an approved source acquisition", status_code=403)
    authorize(repo, source_id, requested_use, approval_id, payload.get("start_ms"), payload.get("end_ms"))
    authorize(repo, source_id, "analysis")
    ocr_enabled = payload.get("ocr", setting(service, "ocr_enabled", False))
    vectors_enabled = payload.get("embeddings", setting(service, "visual_enabled", False))
    # Fail an explicitly requested feature before doing expensive sampling.
    if ocr_enabled and (importlib.util.find_spec("pytesseract") is None or not shutil.which("tesseract")):
        raise SearchError("OCR requires pytesseract and the Tesseract executable", status_code=503)
    vision = _vision_model(service) if vectors_enabled else None
    acquisition = download_source(service, payload)
    source_artifact = repo.get("artifacts", acquisition["artifact_id"])
    source_path = verified_artifact_path(repo, source_artifact)
    probe = probe_media(source_path)
    maximum = setting(service, "max_visual_frames", 240)
    samples = scene_sample_times(source_path, probe["duration_ms"], setting(service, "visual_sample_seconds", 15), maximum, setting(service, "command_timeout_seconds", 1800))
    seen, frame_count, ocr_count, vector_count = [], 0, 0, 0
    with tempfile.TemporaryDirectory(prefix="visual-", dir=_staging(repo)) as directory:
        for timestamp, start_ms, end_ms in samples:
            authorize(repo, source_id, requested_use, approval_id, payload.get("start_ms"), payload.get("end_ms"))
            authorize(repo, source_id, "analysis")
            staged = Path(directory) / "frame.jpg"
            run_command([
                executable("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{timestamp / 1000:.3f}", "-i", str(source_path), "-frames:v", "1",
                "-vf", "scale=w='min(720,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease",
                "-filter_threads", "1", "-q:v", "3", str(staged),
            ], timeout=setting(service, "command_timeout_seconds", 1800))
            perceptual = _perceptual_hash(staged)
            # Nearby duplicate images should not dominate visual retrieval.
            if any((int(perceptual, 16) ^ value).bit_count() <= 3 for value in seen[-20:]):
                staged.unlink()
                continue
            seen.append(int(perceptual, 16))
            frame_id = _identifier("frame_", source_artifact["id"], timestamp, PIPELINE_VERSION)
            artifact_id = _identifier("art_frame_", frame_id)
            artifact = promote_artifact(repo, staged, id=artifact_id, source_id=source_id, kind="keyframe", profile="analysis-jpeg-720",
                                        parent_artifact_id=source_artifact["id"], start_ms=timestamp,
                                        end_ms=min(timestamp + 1, probe["duration_ms"]), approval_id=approval_id,
                                        metadata={"requested_use": requested_use, "pipeline_version": PIPELINE_VERSION, "timestamp_ms": timestamp})
            metadata = {"sampling": "ffmpeg-scene-score-0.35+sparse", "source_sha256": source_artifact["sha256"], "pipeline_version": PIPELINE_VERSION}
            with repo.connect() as connection:
                connection.execute("INSERT OR IGNORE INTO visual_frames(id,source_id,artifact_id,timestamp_ms,start_ms,end_ms,path,sha256,perceptual_hash,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (frame_id, source_id, artifact["id"], timestamp, start_ms, end_ms, artifact["path"], artifact["sha256"], perceptual, json.dumps(metadata, sort_keys=True), datetime.now(timezone.utc).isoformat()))
            frame_count += 1
            path = verified_artifact_path(repo, artifact)
            if ocr_enabled:
                text, confidence, model, revision = _ocr(path, service)
                if text:
                    with repo.connect() as connection:
                        connection.execute("INSERT OR IGNORE INTO ocr_blocks(id,source_id,frame_id,start_ms,end_ms,text,confidence,model_name,model_revision,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (_identifier("ocr_", frame_id, model, revision, text), source_id, frame_id, start_ms, end_ms, text, confidence, model, revision, datetime.now(timezone.utc).isoformat()))
                    ocr_count += 1
            if vision:
                import torch
                from PIL import Image

                model, preprocess, _, name, revision = vision
                with Image.open(path) as image, torch.inference_mode():
                    vector = model.encode_image(preprocess(image).unsqueeze(0)).float()
                    vector = vector / vector.norm(dim=-1, keepdim=True)
                    values = vector[0].tolist()
                if not all(math.isfinite(value) for value in values):
                    raise SearchError("Vision model returned an invalid vector", status_code=422)
                with repo.connect() as connection:
                    connection.execute("INSERT OR IGNORE INTO embeddings(id,entity_type,entity_id,source_id,modality,model_name,model_revision,dimensions,vector_json,input_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (_identifier("emb_", frame_id, name, revision, artifact["sha256"]), "visual_frame", frame_id, source_id, "visual", name, revision, len(values), json.dumps(values), artifact["sha256"], datetime.now(timezone.utc).isoformat()))
                vector_count += 1
    return {"source_id": source_id, "frames": frame_count, "ocr_blocks": ocr_count, "vectors": vector_count,
            "sample_budget": maximum, "sampling_version": "ffmpeg-scene-score-0.35+sparse", "ocr_enabled": bool(ocr_enabled), "visual_embeddings_enabled": bool(vectors_enabled)}


def visual_search(service, query: str, source_ids: set[str] | None = None, limit: int = 100) -> list[dict]:
    """Text-to-image retrieval hook; callers fuse with lexical and transcript hits."""
    if source_ids is not None and not source_ids:
        return []
    # Caption-only libraries do not load a large vision model simply because
    # the optional visual capability is enabled globally.
    with service.repo.connect() as connection:
        sql = "SELECT e.*,f.start_ms,f.end_ms,f.timestamp_ms FROM embeddings e JOIN visual_frames f ON f.id=e.entity_id WHERE e.modality='visual'"
        params = []
        if source_ids is not None:
            sql += " AND e.source_id IN (" + ",".join("?" for _ in source_ids) + ")"
            params = sorted(source_ids)
        rows = connection.execute(sql, params).fetchall()
    if not rows:
        return []
    model, _, tokenizer, name, revision = _vision_model(service)
    rows = [row for row in rows if row["model_name"] == name and row["model_revision"] == revision]
    if not rows:
        return []
    import torch

    with torch.inference_mode():
        query_vector = model.encode_text(tokenizer([query])).float()
        query_vector = query_vector / query_vector.norm(dim=-1, keepdim=True)
        query_values = query_vector[0].tolist()
    results = []
    for row in rows:
        if source_ids is not None and row["source_id"] not in source_ids:
            continue
        values = json.loads(row["vector_json"])
        if len(values) != len(query_values):
            continue
        score = sum(a * b for a, b in zip(query_values, values))
        results.append({"source_id": row["source_id"], "entity_id": row["entity_id"], "start_ms": row["start_ms"], "end_ms": row["end_ms"], "timestamp_ms": row["timestamp_ms"], "evidence_type": "visual", "evidence": "Visual similarity at source timestamp", "score": score, "model_name": name, "model_revision": revision})
    return sorted(results, key=lambda result: result["score"], reverse=True)[:limit]
