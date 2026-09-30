"""Bounded media operations and immutable, content-addressed search artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from app.models.search import SearchError

PIPELINE_VERSION = "targeted-search-1.0"


def setting(service: Any, name: str, default: Any) -> Any:
    settings = getattr(service, "settings", None)
    return settings.get(name, default) if isinstance(settings, dict) else getattr(settings, name, default)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def executable(name: str) -> str:
    if name == "ffmpeg":
        # Keep this import lazy: importing application config is unnecessary for
        # database-only discovery and tests of the artifact store.
        try:
            from app.utils.utils import get_ffmpeg_binary

            result = get_ffmpeg_binary()
            if Path(result).is_file() or shutil.which(result):
                return result
        except ImportError:
            pass
    configured = os.environ.get("TARGETED_SEARCH_" + name.upper())
    result = configured or shutil.which(name)
    if not result:
        raise SearchError(f"{name} is required for this media operation", status_code=503)
    return str(result)


def run_command(args: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    """Never accept shell fragments; avoid exposing extractor URLs in errors."""
    try:
        result = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise SearchError("Required media executable is unavailable", status_code=503) from exc
    except subprocess.TimeoutExpired as exc:
        raise SearchError("Media processing exceeded its configured time limit", status_code=504) from exc
    if result.returncode:
        # Raw stderr can contain signed URLs, authorization headers, or private
        # paths. Persist only an actionable stable error category.
        raise SearchError(f"{Path(args[0]).name} failed (exit {result.returncode})", status_code=422)
    return result


def probe_media(path: Path, timeout: int = 30) -> dict:
    result = run_command([
        executable("ffprobe"), "-v", "error", "-show_format", "-show_streams",
        "-of", "json", str(path),
    ], timeout=timeout)
    try:
        raw = json.loads(result.stdout)
        videos = [s for s in raw.get("streams", []) if s.get("codec_type") == "video"]
        audios = [s for s in raw.get("streams", []) if s.get("codec_type") == "audio"]
        duration = float(raw.get("format", {}).get("duration", 0))
        if not videos or duration <= 0:
            raise ValueError("no playable video stream")
        video = videos[0]
        fps = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"
        numerator, denominator = (float(x) for x in fps.split("/"))
        fps_value = numerator / denominator if denominator else 0
        audio_duration = float(audios[0].get("duration", duration)) if audios else None
        return {
            "duration_ms": round(duration * 1000), "width": int(video["width"]),
            "height": int(video["height"]), "fps": fps, "fps_value": fps_value,
            "video_codec": video.get("codec_name"), "has_audio": bool(audios),
            "audio_stream_count": len(audios),
            "audio_streams": [{"codec": audio.get("codec_name"), "channels": audio.get("channels"),
                               "duration_ms": round(float(audio.get("duration", duration)) * 1000)} for audio in audios],
            "audio_codec": audios[0].get("codec_name") if audios else None,
            "audio_duration_ms": round(audio_duration * 1000) if audio_duration else None,
        }
    except (ValueError, KeyError, TypeError) as exc:
        raise SearchError("Media does not contain a valid playable video", status_code=422) from exc


def ffmpeg_version() -> str:
    result = run_command([executable("ffmpeg"), "-version"], timeout=10)
    return result.stdout.decode("utf-8", "replace").splitlines()[0][:250]


def validate_range(start_ms: int, end_ms: int, duration_ms: int, max_ms: int = 90_000) -> None:
    if isinstance(start_ms, bool) or isinstance(end_ms, bool):
        raise SearchError("Clip range must use integer milliseconds", status_code=422)
    if not isinstance(start_ms, int) or not isinstance(end_ms, int):
        raise SearchError("Clip range must use integer milliseconds", status_code=422)
    if not 0 <= start_ms < end_ms <= duration_ms:
        raise SearchError("Clip range must fall within the source duration", status_code=422)
    if end_ms - start_ms > max_ms:
        raise SearchError("Clip range exceeds the configured maximum duration", status_code=422)


def encode_clip(source: Path, output: Path, start_ms: int, end_ms: int, timeout: int = 900) -> dict:
    """Accurate seek + re-encoding, with source frame timing and original audio."""
    source_probe = probe_media(source)
    validate_range(start_ms, end_ms, source_probe["duration_ms"], max_ms=source_probe["duration_ms"])
    run_command([
        executable("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{start_ms / 1000:.3f}", "-i", str(source),
        "-t", f"{(end_ms - start_ms) / 1000:.3f}",
        "-map", "0:v:0", "-map", "0:a?", "-map_metadata", "-1",
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-filter_threads", "1",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
        "-fps_mode", "passthrough", "-threads", "2",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(output),
    ], timeout=timeout)
    output_probe = probe_media(output)
    tolerance = max(150, round(2000 / max(source_probe["fps_value"], 1)))
    if abs(output_probe["duration_ms"] - (end_ms - start_ms)) > tolerance:
        raise SearchError("Extracted clip duration failed verification", status_code=422)
    if source_probe["audio_stream_count"] != output_probe["audio_stream_count"]:
        raise SearchError("Extracted clip audio failed verification", status_code=422)
    for audio in output_probe["audio_streams"]:
        if abs(audio["duration_ms"] - output_probe["duration_ms"]) > 250:
            raise SearchError("Extracted clip audio/video duration failed verification", status_code=422)
    return output_probe


def encode_review_proxy(source: Path, output: Path, timeout: int = 900) -> dict:
    run_command([
        executable("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source), "-map", "0:v:0", "-map", "0:a?", "-map_metadata", "-1",
        "-vf", "scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
        "-filter_threads", "1", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-fps_mode", "passthrough", "-threads", "2",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(output),
    ], timeout=timeout)
    return probe_media(output)


def promote_artifact(repo: Any, staged_path: Path, **record: Any) -> dict:
    """Atomic promotion is recoverable: an interrupted DB insert leaves a CAS
    object which a retry verifies and reuses, never a partial visible artifact.
    """
    staged_path = Path(staged_path)
    digest = sha256_file(staged_path)
    suffix = staged_path.suffix.lower()
    if suffix not in {".mp4", ".mkv", ".webm", ".mov", ".json", ".jpg", ".png", ".wav", ".mp3", ".m4a", ".flac", ".ogg", ".pdf", ".txt", ".md", ".geojson", ".webp", ".otio"}:
        suffix = ".bin"
    target = Path(repo.root) / "artifacts" / digest[:2] / (digest + suffix)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if sha256_file(target) != digest:
            raise SearchError("Existing immutable artifact failed digest verification", status_code=409)
        staged_path.unlink()
    else:
        # Include crash-orphaned CAS objects in the quota as well as registered
        # rows. Immutable duplicates share one pathname and are counted once.
        used = sum(path.stat().st_size for path in (Path(repo.root) / "artifacts").rglob("*") if path.is_file() and not path.is_symlink())
        budget = getattr(repo.settings, "max_storage_bytes", 20_000_000_000)
        if used + staged_path.stat().st_size > budget:
            raise SearchError("Targeted-search storage budget is exhausted", status_code=507)
        # Staging is under repo.root, so os.replace stays on one filesystem.
        with staged_path.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(staged_path, target)
    record.update(path=str(target), sha256=digest, bytes=target.stat().st_size)
    return repo.insert_artifact(**record)


def verified_artifact_path(repo: Any, artifact: dict) -> Path:
    root = Path(repo.root).resolve()
    path = (root / artifact["path"]).resolve()
    try:
        path.relative_to(root / "artifacts")
    except ValueError as exc:
        raise SearchError("Artifact path is outside immutable storage", status_code=409) from exc
    if not path.is_file() or sha256_file(path) != artifact["sha256"]:
        raise SearchError("Artifact is missing or failed digest verification", status_code=409)
    return path


def extract_clip(service: Any, payload: dict) -> dict:
    from .acquisition import download_source
    from .policy import authorize
    from .provenance import create_manifest

    repo = service.repo
    source_id = payload["source_id"]
    start_ms, end_ms = payload["start_ms"], payload["end_ms"]
    requested_use = payload.get("requested_use", "internal_review")
    approval_id = payload.get("approval_id")
    if not approval_id:
        raise SearchError("Clip extraction requires a scoped download approval", status_code=403)
    authorize(repo, source_id, requested_use, approval_id, start_ms, end_ms)
    clip_id = payload.get("clip_id")
    if clip_id:
        existing = repo.get("artifacts", clip_id)
        if existing:
            _assert_clip_identity(existing, payload)
            verified_artifact_path(repo, existing)
            meta = existing.get("metadata", {})
            if not meta.get("manifest_artifact_id") or not meta.get("proxy_artifact_id"):
                # A crash after clip promotion must resume derivative creation.
                return _complete_clip(service, existing, payload)
            for key in ("manifest_artifact_id", "proxy_artifact_id"):
                derivative = repo.get("artifacts", meta[key])
                if not derivative:
                    return _complete_clip(service, existing, payload)
                verified_artifact_path(repo, derivative)
            return {"clip_id": existing["id"], "artifact_id": existing["id"], **meta}
    acquisition = download_source(service, payload)
    source_artifact = repo.get("artifacts", acquisition["artifact_id"])
    source_path = verified_artifact_path(repo, source_artifact)
    source_probe = probe_media(source_path)
    validate_range(start_ms, end_ms, source_probe["duration_ms"], setting(service, "max_clip_duration_ms", 90_000))
    if not clip_id:
        key = f"{source_id}|{source_artifact['sha256']}|{start_ms}|{end_ms}|{approval_id}|{requested_use}|{PIPELINE_VERSION}"
        clip_id = "clip_" + hashlib.sha256(key.encode()).hexdigest()[:32]
        payload = {**payload, "clip_id": clip_id}
        existing = repo.get("artifacts", clip_id)
        if existing:
            _assert_clip_identity(existing, payload)
            return _complete_clip(service, existing, payload)
    with tempfile.TemporaryDirectory(prefix="clip-", dir=_staging(repo)) as directory:
        staged = Path(directory) / "clip.mp4"
        metadata = encode_clip(source_path, staged, start_ms, end_ms, setting(service, "command_timeout_seconds", 1800))
        # Authorization may have changed while the media command was running.
        authorize(repo, source_id, requested_use, approval_id, start_ms, end_ms)
        metadata.update(pipeline_version=PIPELINE_VERSION, requested_use=requested_use,
                        candidate_id=payload.get("candidate_id"), source_probe=source_probe)
        record = dict(source_id=source_id, kind="clip", profile="exact-h264-aac",
                      parent_artifact_id=source_artifact["id"], start_ms=start_ms,
                      end_ms=end_ms, approval_id=approval_id, metadata=metadata)
        if clip_id:
            record["id"] = clip_id
        clip = promote_artifact(repo, staged, **record)
    return _complete_clip(service, clip, payload, manifest_factory=create_manifest)


def _assert_clip_identity(clip: dict, payload: dict) -> None:
    expected = {"source_id": payload["source_id"], "kind": "clip", "approval_id": payload.get("approval_id"),
                "start_ms": payload["start_ms"], "end_ms": payload["end_ms"]}
    if any(clip.get(key) != value for key, value in expected.items()):
        raise SearchError("Existing clip identity does not match this approved extraction", status_code=409)


def _staging(repo: Any) -> Path:
    path = Path(repo.root) / "staging"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _complete_clip(service: Any, clip: dict, payload: dict, manifest_factory=None) -> dict:
    from .policy import authorize
    from .provenance import create_manifest

    repo = service.repo
    manifest_factory = manifest_factory or create_manifest
    source_id = clip["source_id"]
    requested_use = payload.get("requested_use", "internal_review")
    authorize(repo, source_id, requested_use, payload.get("approval_id"), clip["start_ms"], clip["end_ms"])
    metadata = dict(clip.get("metadata", {}))
    with repo.connect() as connection:
        proxy_row = connection.execute("SELECT id FROM artifacts WHERE parent_artifact_id=? AND kind='review_proxy' ORDER BY created_at DESC LIMIT 1", (clip["id"],)).fetchone()
    proxy = repo.get("artifacts", proxy_row["id"]) if proxy_row else None
    if proxy:
        verified_artifact_path(repo, proxy)
    else:
        with tempfile.TemporaryDirectory(prefix="proxy-", dir=_staging(repo)) as directory:
            staged = Path(directory) / "review.mp4"
            proxy_metadata = encode_review_proxy(verified_artifact_path(repo, clip), staged, setting(service, "command_timeout_seconds", 1800))
            authorize(repo, source_id, requested_use, payload.get("approval_id"), clip["start_ms"], clip["end_ms"])
            proxy_metadata.update(pipeline_version=PIPELINE_VERSION, source_fps=metadata.get("fps"), requested_use=requested_use)
            proxy = promote_artifact(repo, staged, source_id=source_id, kind="review_proxy",
                                     id="proxy_" + hashlib.sha256((clip["id"] + "|review-720p-h264-v1").encode()).hexdigest()[:32],
                                     profile="review-720p-h264", parent_artifact_id=clip["id"],
                                     start_ms=clip["start_ms"], end_ms=clip["end_ms"],
                                     approval_id=clip.get("approval_id"), metadata=proxy_metadata)
    manifest = manifest_factory(service, clip, payload, proxy)
    metadata.update(proxy_artifact_id=proxy["id"], manifest_artifact_id=manifest["id"])
    with repo.connect() as connection:
        connection.execute("UPDATE artifacts SET metadata_json=? WHERE id=?", (json.dumps(metadata, sort_keys=True), clip["id"]))
    return {"clip_id": clip["id"], "artifact_id": clip["id"], **metadata}
