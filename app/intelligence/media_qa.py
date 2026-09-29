"""Bounded, local, full-stream technical checks complement sampled visual QA.

FFmpeg filter semantics: https://ffmpeg.org/ffmpeg-filters.html (blackdetect,
freezedetect, ebur128). No shell, network media, raw decoder logs, or retained PCM.
"""

from __future__ import annotations

import math
import re
import subprocess
import threading
from pathlib import Path

import numpy as np

from app.intelligence.contracts import ReviewIssue
from app.intelligence.qa_contracts import MediaCoverage, MediaInspection
from app.utils import utils

_SAMPLE_RATE = 48000
_WINDOW = _SAMPLE_RATE // 10
_MAX_DURATION = 24000  # The plan contract permits at most 80 scenes of 300s.
_MAX_EVENTS = 10000
_STATIC_TYPES = {
    "ai_image",
    "local_asset",
    "diagram",
    "chart",
    "screenshot",
    "text_card",
    "icon_composition",
}
_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"


def _field(item, key, default=None):
    return (
        item.get(key, default)
        if isinstance(item, dict)
        else getattr(item, key, default)
    )


def _db(value):
    # None represents negative infinity (digital silence), keeping strict JSON finite.
    return round(20 * math.log10(value), 3) if value > 0 else None


def _local(path):
    local = Path(path).expanduser().resolve(strict=True)
    if not local.is_file():
        raise ValueError("A local media file is required")
    return str(local)


def _lines(pipe, consume):
    """Discard oversized lines, without ever retaining or exposing metadata logs."""
    oversized = False
    while line := pipe.readline(65536):
        if oversized or len(line) >= 65536:
            oversized = not line.endswith(b"\n")
            continue
        consume(line.decode("utf-8", errors="replace").strip())


def _run(args, timeout, stderr_line, stdout_line=None, stdout_binary=None):
    """Drain both pipes concurrently with bounded consumers and a wall-clock cap."""
    failures = []

    def drain(pipe, binary, callback):
        try:
            if binary:
                while data := pipe.read(65536):
                    callback(data)
            else:
                _lines(pipe, callback or (lambda _: None))
        except Exception:
            failures.append(True)
            try:
                process.kill()
            except OSError:
                pass
        finally:
            pipe.close()

    try:
        process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError:
        return False, "unavailable"
    readers = [
        threading.Thread(
            target=drain, args=(process.stderr, False, stderr_line), daemon=True
        ),
        threading.Thread(
            target=drain,
            args=(
                process.stdout,
                stdout_binary is not None,
                stdout_binary or stdout_line,
            ),
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()
    status = "completed"
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        status = "timeout"
        process.kill()
        process.wait(timeout=5)
    finally:
        for reader in readers:
            reader.join(timeout=5)
    return process.returncode == 0 and not failures and status == "completed", status


def _base(path):
    return [
        utils.get_ffmpeg_binary(),
        "-hide_banner",
        "-nostdin",
        "-nostats",
        "-loglevel",
        "info",
        "-xerror",
        "-err_detect",
        "explode",
        "-protocol_whitelist",
        "file,pipe",
        "-i",
        path,
    ]


def _probe(path):
    info = {"video": False, "audio": False, "duration": None}

    def line(value):
        if re.match(r"Stream #0:\d+.*: Video:", value):
            info["video"] = True
        if re.match(r"Stream #0:\d+.*: Audio:", value):
            info["audio"] = True
        found = re.match(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?),", value)
        if found:
            hours, minutes, seconds = map(float, found.groups())
            info["duration"] = hours * 3600 + minutes * 60 + seconds

    # No output is deliberate: FFmpeg reports headers then exits nonzero.
    _, status = _run(_base(path), 30, line)
    info["status"] = status
    return info


def _timeout(duration):
    return min(900, max(45, duration * 4))


def _scan_video(path, duration):
    result = {
        "black_intervals": [],
        "freeze_intervals": [],
        "duration_seconds": 0.0,
        "frames": 0,
        "complete": False,
        "events_truncated": False,
    }
    freeze_start = None

    def add(kind, start, end):
        if len(result[kind]) >= _MAX_EVENTS:
            result["events_truncated"] = True
        else:
            result[kind].append({"start": max(0.0, start), "end": max(start, end)})

    def stderr(value):
        nonlocal freeze_start
        found = re.search(rf"black_start:({_NUMBER}) black_end:({_NUMBER})", value)
        if found:
            add("black_intervals", *map(float, found.groups()))
        found = re.search(rf"freeze_start:\s*({_NUMBER})", value)
        if found:
            freeze_start = float(found.group(1))
        found = re.search(rf"freeze_end:\s*({_NUMBER})", value)
        if found and freeze_start is not None:
            add("freeze_intervals", freeze_start, float(found.group(1)))
            freeze_start = None

    def progress(value):
        key, _, raw = value.partition("=")
        if key == "out_time_us" and raw.lstrip("-").isdigit():
            result["duration_seconds"] = max(0.0, int(raw) / 1_000_000)
        if key == "frame" and raw.strip().isdigit():
            result["frames"] = int(raw)
        if value == "progress=end":
            result["complete"] = True

    ok, status = _run(
        _base(path)
        + [
            "-map",
            "0:v:0",
            "-an",
            "-vf",
            "blackdetect=d=0.25:pix_th=0.05:pic_th=0.98,freezedetect=n=-60dB:d=1",
            "-progress",
            "pipe:1",
            "-f",
            "null",
            "-",
        ],
        _timeout(duration),
        stderr,
        stdout_line=progress,
    )
    result["complete"] = ok and result["complete"] and result["frames"] > 0
    result["status"] = status
    if freeze_start is not None:
        add("freeze_intervals", freeze_start, result["duration_seconds"])
    return result


def _scan_audio(path, duration):
    """Consume stereo float PCM incrementally; retain only 100ms energy bins."""
    result = {
        "complete": False,
        "integrated_loudness_lufs": None,
        "true_peak_dbfs": None,
    }
    pending = bytearray()
    windows = []
    samples = 0
    clipped = 0
    total_energy = 0.0
    peak = 0.0
    true_peak_section = False

    def consume_window(data):
        nonlocal samples, clipped, total_energy, peak
        values = np.frombuffer(data, dtype="<f4")
        if not np.isfinite(values).all():
            raise ValueError("Nonfinite decoded audio")
        absolute = np.abs(values)
        energy = float(np.dot(values.astype(np.float64), values.astype(np.float64)))
        samples += len(values)
        clipped += int(np.count_nonzero(absolute >= 0.999))
        total_energy += energy
        peak = max(peak, float(absolute.max(initial=0)))
        windows.append(math.sqrt(energy / len(values)))
        if len(windows) > _MAX_DURATION * 10 + 20:
            raise ValueError("Audio exceeds inspection limit")

    def pcm(data):
        pending.extend(data)
        chunk = _WINDOW * 2 * 4
        while len(pending) >= chunk:
            consume_window(bytes(pending[:chunk]))
            del pending[:chunk]

    def stderr(value):
        nonlocal true_peak_section
        found = re.match(rf"I:\s*({_NUMBER}) LUFS", value)
        if found:
            result["integrated_loudness_lufs"] = float(found.group(1))
        if value == "True peak:":
            true_peak_section = True
        found = re.match(rf"Peak:\s*({_NUMBER}) dBFS", value)
        if true_peak_section and found:
            result["true_peak_dbfs"] = float(found.group(1))

    ok, status = _run(
        _base(path)
        + [
            "-map",
            "0:a:0",
            "-vn",
            "-af",
            "ebur128=framelog=verbose:peak=true",
            "-ac",
            "2",
            "-ar",
            str(_SAMPLE_RATE),
            "-f",
            "f32le",
            "pipe:1",
        ],
        _timeout(duration),
        stderr,
        stdout_binary=pcm,
    )
    if pending and len(pending) % 8 == 0:
        try:
            consume_window(bytes(pending))
        except ValueError:
            ok = False
    result.update(
        complete=ok and samples > 0,
        status=status,
        duration_seconds=samples / (2 * _SAMPLE_RATE),
        rms_dbfs=_db(math.sqrt(total_energy / samples)) if samples else None,
        sample_peak_dbfs=_db(peak),
        near_full_scale_sample_ratio=clipped / samples if samples else 0.0,
        energy_windows=windows,
    )
    intervals = []
    start = None
    for index, energy in enumerate(windows + [1.0]):
        if energy < 10 ** (-50 / 20):
            if start is None:
                start = index / 10
        elif start is not None:
            end = min(index / 10, result["duration_seconds"])
            if end - start >= 0.95:
                intervals.append({"start": start, "end": end})
            start = None
    result["silence_intervals"] = intervals
    return result


def _segments(plan, timeline):
    scenes = {_field(scene, "scene_id"): scene for scene in _field(plan, "scenes", [])}
    result = []
    cursor = 0.0
    for segment in timeline:
        start, end = float(_field(segment, "start")), float(_field(segment, "end"))
        scene_id = _field(segment, "scene_id")
        if (
            not math.isfinite(start)
            or not math.isfinite(end)
            or abs(start - cursor) > 0.05
            or end <= start
            or scene_id not in scenes
        ):
            raise ValueError("Timeline must be contiguous and match the plan")
        visual_type = _field(scenes[scene_id], "preferred_visual_type", "")
        expected_static = (
            str(getattr(visual_type, "value", visual_type)) in _STATIC_TYPES
        )
        # Callers with material-path evidence may distinguish local movies from
        # local still images. Without it, local stillness is deliberately allowed.
        if isinstance(_field(segment, "expected_static"), bool):
            expected_static = _field(segment, "expected_static")
        result.append(
            {
                "scene_id": scene_id,
                "start": start,
                "end": end,
                "expected_static": expected_static,
            }
        )
        cursor = end
    if (
        not result
        or len(result) != len(scenes)
        or len({r["scene_id"] for r in result}) != len(scenes)
    ):
        raise ValueError("Timeline must cover every scene exactly once")
    return result


def _mapped(intervals, segments):
    return [
        {
            "scene_id": segment["scene_id"],
            "start": round(max(interval["start"], segment["start"]), 4),
            "end": round(min(interval["end"], segment["end"]), 4),
            "expected_static": segment["expected_static"],
        }
        for interval in intervals
        for segment in segments
        if min(interval["end"], segment["end"])
        > max(interval["start"], segment["start"])
    ]


def _compare_audio(rendered, narration):
    left = np.asarray(rendered["energy_windows"], dtype=float)
    right = np.asarray(narration["energy_windows"], dtype=float)
    size = min(len(left), len(right))
    correlation = None
    if size >= 5 and np.std(left[:size]) > 1e-6 and np.std(right[:size]) > 1e-6:
        correlation = float(np.corrcoef(left[:size], right[:size])[0, 1])
    active = right[:size] >= 10 ** (-45 / 20)
    lost = active & (left[:size] < 10 ** (-50 / 20))
    return {
        "duration_difference_seconds": rendered["duration_seconds"]
        - narration["duration_seconds"],
        "energy_envelope_correlation": correlation,
        "reference_active_windows": int(active.sum()),
        "reference_activity_missing_ratio": float(lost.sum() / active.sum())
        if active.any()
        else 0.0,
        "reference_is_silent": narration["rms_dbfs"] is None
        or narration["rms_dbfs"] < -50,
    }


def inspect_render(
    video_path, plan, timeline, narration_path=None, expected_audio=True
) -> MediaInspection:
    """Inspect local media without changing it; errors block, warnings inform review."""
    issues = []
    coverage = MediaCoverage()
    metrics = {}

    def issue(category, description, suggested_fix, severity="error", scene_id=None):
        issues.append(
            ReviewIssue(
                scene_id=scene_id,
                severity=severity,
                category=category,
                description=description,
                suggested_fix=suggested_fix,
            )
        )

    def finish():
        passed = not any(item.severity == "error" for item in issues)
        return MediaInspection(
            passed=passed,
            issues=issues,
            coverage=coverage,
            metrics=metrics,
            summary=(
                "Technical media inspection passed"
                if passed
                else "Technical media inspection requires attention"
            )
            + f" ({sum(i.severity == 'error' for i in issues)} errors, "
            + f"{sum(i.severity == 'warning' for i in issues)} warnings).",
        )

    try:
        path = _local(video_path)
        segments = _segments(plan, timeline)
    except (OSError, ValueError, TypeError):
        issue(
            "media_input",
            "The local render or scene timeline is unavailable or invalid.",
            "Restore the render and its matching complete scene timeline, then run QA again.",
        )
        return finish()
    expected_duration = segments[-1]["end"]
    probe = _probe(path)
    duration = probe["duration"] or expected_duration
    metrics.update(
        expected_duration_seconds=expected_duration,
        container_duration_seconds=probe["duration"],
    )
    if duration <= 0 or duration > _MAX_DURATION or expected_duration > _MAX_DURATION:
        issue(
            "media_duration",
            "The render exceeds the supported inspection duration.",
            "Split the production into shorter videos.",
        )
        return finish()
    if not probe["video"]:
        issue(
            "video_stream",
            "No readable video stream was found in the render.",
            "Re-render the video from the preserved scene assets.",
        )
        return finish()
    video = _scan_video(path, duration)
    coverage.full_video_scan = video["complete"]
    metrics["video"] = video
    if not video["complete"]:
        issue(
            "video_decode",
            "The complete video stream could not be decoded or the scan timed out.",
            "Check the source clips and FFmpeg, then re-render and retry QA.",
        )
    tolerance = max(0.25, min(1.0, expected_duration * 0.01))
    if (
        video["complete"]
        and abs(video["duration_seconds"] - expected_duration) > tolerance
    ):
        issue(
            "video_timing",
            "The decoded video duration does not match the complete scene timeline.",
            "Re-render with the accepted scene boundaries and narration duration.",
        )
    if video["events_truncated"]:
        issue(
            "video_events",
            "The interval report exceeded its event limit; all frames were still scanned.",
            "Inspect the rapidly alternating source material.",
            "warning",
        )
    for kind, category in (
        ("black_intervals", "black_frames"),
        ("freeze_intervals", "frozen_video"),
    ):
        mapped = _mapped(video[kind], segments)
        metrics[kind] = mapped
        for segment in segments:
            overlap = sum(
                r["end"] - r["start"]
                for r in mapped
                if r["scene_id"] == segment["scene_id"]
            )
            scene_duration = segment["end"] - segment["start"]
            if kind == "freeze_intervals":
                if segment["expected_static"] or overlap < max(
                    1.0, scene_duration * 0.5
                ):
                    continue
                description = f"The scene has {overlap:.1f}s of little or no frame change where video motion is expected."
                fix = "Review the scene motion and replace a stalled clip if the stillness is unintended."
            else:
                if overlap < max(0.5, scene_duration * 0.25):
                    continue
                description = f"The scene contains {overlap:.1f}s of nearly black frames; a dark graphic or intentional fade can produce this result."
                fix = "Review the scene for unintended black gaps or an unreadable graphic."
            issue(category, description, fix, "warning", segment["scene_id"])

    narration = None
    if narration_path:
        try:
            narration = _scan_audio(_local(narration_path), duration)
            if not narration["complete"]:
                narration = None
        except (OSError, ValueError, TypeError):
            narration = None
        if narration is None:
            issue(
                "narration_reference",
                "The supplied narration reference could not be fully decoded.",
                "Restore the narration reference to compare it with the render.",
                "warning",
            )
        else:
            metrics["narration"] = {
                key: value
                for key, value in narration.items()
                if key != "energy_windows"
            }
            if narration["rms_dbfs"] is None or narration["rms_dbfs"] < -50:
                expected_audio = False
    metrics["expected_audio"] = bool(expected_audio)
    if not probe["audio"]:
        if expected_audio:
            issue(
                "audio_missing",
                "The render has no audio stream although audible narration is expected.",
                "Restore narration and re-render with voice volume above zero.",
            )
        coverage.full_file_decode = coverage.full_video_scan
        return finish()

    audio = _scan_audio(path, duration)
    coverage.full_audio_scan = audio["complete"]
    coverage.full_file_decode = coverage.full_video_scan and coverage.full_audio_scan
    metrics["audio"] = {
        key: value for key, value in audio.items() if key != "energy_windows"
    }
    metrics["silence_intervals"] = _mapped(audio["silence_intervals"], segments)
    if not audio["complete"]:
        issue(
            "audio_decode",
            "The complete audio stream could not be decoded or the scan timed out.",
            "Check the narration and audio source files, then re-render and retry QA.",
        )
        return finish()
    if expected_audio and audio["duration_seconds"] < expected_duration - tolerance:
        issue(
            "audio_timing",
            "The rendered audio ends before the scene timeline is complete.",
            "Re-render with the complete narration track and matching video duration.",
        )
    if expected_audio and (audio["rms_dbfs"] is None or audio["rms_dbfs"] < -50):
        issue(
            "audio_silent",
            "The rendered audio is effectively silent although audible narration is expected.",
            "Restore narration volume and confirm that the correct voice track is mixed.",
        )
    elif expected_audio:
        for segment in segments:
            silence = sum(
                r["end"] - r["start"]
                for r in metrics["silence_intervals"]
                if r["scene_id"] == segment["scene_id"]
            )
            if silence >= max(1.0, (segment["end"] - segment["start"]) * 0.5):
                issue(
                    "audio_silence",
                    f"The scene has {silence:.1f}s of silence below -50 dBFS.",
                    "Check that this pause is intentional and narration has not been dropped.",
                    "warning",
                    segment["scene_id"],
                )
    if (
        audio["near_full_scale_sample_ratio"] > 0.001
        or (audio["true_peak_dbfs"] or -100) > 0
    ):
        issue(
            "audio_clipping",
            "Audio repeatedly approaches full scale or exceeds 0 dBFS true peak; clipping is possible.",
            "Lower the narration/music mix and inspect loud sections before publishing.",
            "warning",
        )
    if audio["rms_dbfs"] is not None and expected_audio and audio["rms_dbfs"] < -35:
        issue(
            "audio_level",
            "The average audio signal is very quiet.",
            "Increase the mix level and verify narration remains clear.",
            "warning",
        )
    if narration is not None:
        comparison = _compare_audio(audio, narration)
        metrics["narration_comparison"] = comparison
        coverage.narration_comparison = True
        if (
            expected_audio
            and abs(comparison["duration_difference_seconds"]) > tolerance
        ):
            issue(
                "narration_timing",
                "The rendered audio duration differs from the supplied narration reference.",
                "Confirm the render uses the complete accepted narration at the intended speed.",
                "warning",
            )
        if expected_audio and comparison["reference_activity_missing_ratio"] > 0.1:
            issue(
                "narration_activity",
                "The render is silent during more than 10% of active reference-narration windows.",
                "Check the voice mix and scene audio alignment for missing narration.",
                "warning",
            )
    return finish()
