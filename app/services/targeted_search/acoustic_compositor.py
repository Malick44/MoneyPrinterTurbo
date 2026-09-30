"""Sample-clock narration/SFX mixing and an editable OpenTimelineIO timeline."""

from __future__ import annotations

import math
import tempfile
from pathlib import Path

from pydantic import ValidationError

from app.models.acoustic import MixOptions
from app.models.search import SearchError

from .media import executable, promote_artifact, run_command, verified_artifact_path
from .repository import json_text
from .sound_assets import _fingerprint, probe_wav, validate_sound


SAMPLE_RATE = 48000


def _integer(value, low, high, name):
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not low <= value <= high
    ):
        raise SearchError(f"Acoustic {name} must be a bounded integer", 422)
    return value


def _number(value, low, high, name):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not low <= value <= high
    ):
        raise SearchError(f"Acoustic {name} must be a bounded finite number", 422)
    return float(value)


def _pinned_asset(workspace, record, prefix, requested_use):
    asset = workspace.get_asset(record.get(prefix + "_asset_id"))
    if (
        asset["case_id"] != record["case_id"]
        or asset["asset_version_id"] != record.get(prefix + "_asset_version_id")
        or asset["sha256"] != record.get(prefix + "_sha256")
        or not asset["artifact_id"]
    ):
        raise SearchError(f"Acoustic {prefix} source version changed", 409)
    workspace.authorize_asset(asset["id"], requested_use)
    return asset, workspace.asset_path(asset["id"])


def validate_inputs(workspace, record):
    """Also available to the plan service; does not depend on its persistence."""
    case_id = record.get("case_id")
    workspace.get_case(case_id)
    requested_use = record.get("requested_use", "internal_review")
    duration = _integer(record.get("duration_ms"), 20, 10800000, "duration")
    narration, narration_path = _pinned_asset(
        workspace, record, "narration", requested_use
    )
    if narration["asset_kind"] != "audio":
        raise SearchError("Acoustic narration must be a retained audio WAV", 422)
    measured = probe_wav(narration_path)
    if abs(measured["duration_ms"] - duration) > 2:
        raise SearchError("Acoustic duration no longer matches its narration WAV", 409)
    script, script_path = _pinned_asset(workspace, record, "script", "internal_review")
    if script["asset_kind"] != "script":
        raise SearchError(
            "Acoustic narration needs its matching production script", 422
        )
    alignment = workspace.repo.get("artifacts", record.get("transcript_artifact_id"))
    if (
        not alignment
        or alignment["source_id"] != narration["source_id"]
        or alignment["sha256"] != record.get("alignment_sha256")
    ):
        raise SearchError(
            "Acoustic word alignment no longer matches its narration", 409
        )
    verified_artifact_path(workspace.repo, alignment)
    with workspace.repo.connect() as connection:
        transcript = connection.execute(
            "SELECT 1 FROM case_transcripts WHERE asset_id=? AND asset_version_id=? AND transcript_artifact_id=? AND scope='narration' LIMIT 1",
            (narration["id"], narration["asset_version_id"], alignment["id"]),
        ).fetchone()
    if not transcript:
        raise SearchError(
            "Mixing requires the current narration word alignment scope", 409
        )
    cues = record.get("cues", [])
    if not isinstance(cues, list) or len(cues) > 60:
        raise SearchError("Acoustic plans support at most 60 sound cues", 422)
    try:
        mix = MixOptions.model_validate(record.get("mix", {})).model_dump()
    except (ValidationError, TypeError, ValueError) as exc:
        raise SearchError(
            "Invalid acoustic gain, ducking or headroom settings", 422
        ) from exc
    enabled, members, seen = (
        [],
        {narration["id"]: narration, script["id"]: script},
        set(),
    )
    for cue in cues:
        if not isinstance(cue, dict) or not isinstance(cue.get("enabled", True), bool):
            raise SearchError("Acoustic cue enabled flags must be booleans", 422)
        if not cue.get("enabled", True):
            continue
        identifier = cue.get("cue_id")
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise SearchError("Enabled acoustic cue IDs must be unique", 422)
        seen.add(identifier)
        if not cue.get("asset_id"):
            raise SearchError(
                "Match or disable each acoustic cue before rendering", 422
            )
        asset = validate_sound(workspace, cue["asset_id"], requested_use)
        if (
            asset["case_id"] != case_id
            or asset["asset_version_id"] != cue.get("asset_version_id")
            or asset["sha256"] != cue.get("sha256")
        ):
            raise SearchError(
                "Acoustic sound cue refers to an obsolete or different case asset", 409
            )
        sound = asset["metadata"]["sound_asset"]
        expected_catalog = cue.get("match", {}).get("sound_metadata_sha256")
        if expected_catalog and expected_catalog != _fingerprint(asset, sound):
            raise SearchError("Sound taxonomy changed after this cue was matched", 409)
        start = _integer(cue.get("start_ms"), 0, duration, "cue start")
        source_start = _integer(
            cue.get("source_start_ms", 0), 0, sound["duration_ms"], "sound source start"
        )
        length = _integer(cue.get("duration_ms"), 20, 15000, "cue duration")
        if start + length > duration or source_start + length > sound["duration_ms"]:
            raise SearchError(
                "Acoustic cue range falls outside the narration or sound WAV", 422
            )
        gain = _number(cue.get("gain_db", -18), -40, -4, "sound gain")
        fade_in = _integer(cue.get("fade_in_ms", 20), 0, 5000, "fade in")
        fade_out = _integer(cue.get("fade_out_ms", 100), 0, 5000, "fade out")
        if fade_in + fade_out > length:
            raise SearchError("Sound fades must fit inside the placed cue", 422)
        enabled.append(
            {**cue, "gain_db": gain, "fade_in_ms": fade_in, "fade_out_ms": fade_out}
        )
        members[asset["id"]] = asset
    return {
        "narration": narration,
        "narration_path": narration_path,
        "script": script,
        "script_path": script_path,
        "alignment": alignment,
        "duration_ms": duration,
        "cues": enabled,
        "mix": mix,
        "sources": members,
    }


def _speech_intervals(workspace, inputs):
    narration = inputs["narration"]
    with workspace.repo.connect() as connection:
        words = connection.execute(
            "SELECT start_ms,end_ms FROM transcript_words WHERE asset_id=? AND asset_version_id=? AND transcript_artifact_id=? AND start_ms IS NOT NULL AND end_ms>start_ms ORDER BY start_ms,end_ms",
            (narration["id"], narration["asset_version_id"], inputs["alignment"]["id"]),
        ).fetchall()
    intervals = []
    for word in words:
        start = max(0, word["start_ms"] - 40)
        end = min(inputs["duration_ms"], word["end_ms"] + 120)
        if end <= start:
            continue
        if intervals and start <= intervals[-1][1]:
            intervals[-1][1] = max(intervals[-1][1], end)
        else:
            intervals.append([start, end])
    if len(intervals) > 10000:
        raise SearchError("Narration ducking exceeds the aligned interval budget", 422)
    return intervals


def _time(ms):
    return {
        "OTIO_SCHEMA": "RationalTime.1",
        "value": round(ms * SAMPLE_RATE / 1000),
        "rate": SAMPLE_RATE,
    }


def _range(start, duration):
    return {
        "OTIO_SCHEMA": "TimeRange.1",
        "start_time": _time(start),
        "duration": _time(duration),
    }


def _clip(name, path, source_start, duration, source_duration, metadata):
    return {
        "OTIO_SCHEMA": "Clip.2",
        "name": name,
        "metadata": metadata,
        "source_range": _range(source_start, duration),
        "effects": [],
        "markers": [],
        "media_references": {
            "DEFAULT_MEDIA": {
                "OTIO_SCHEMA": "ExternalReference.1",
                "name": name,
                "metadata": {},
                "target_url": path.as_uri(),
                "available_range": _range(0, source_duration),
            }
        },
        "active_media_reference_key": "DEFAULT_MEDIA",
    }


def _otio_timeline(workspace, record, inputs):
    tracks = [
        {
            "OTIO_SCHEMA": "Track.1",
            "name": "Narration",
            "metadata": {},
            "source_range": None,
            "effects": [],
            "markers": [],
            "kind": "Audio",
            "children": [
                _clip(
                    "Narration",
                    inputs["narration_path"],
                    0,
                    inputs["duration_ms"],
                    inputs["duration_ms"],
                    {
                        "asset_id": inputs["narration"]["id"],
                        "gain_db": inputs["mix"]["narration_gain_db"],
                    },
                )
            ],
        }
    ]
    for cue in inputs["cues"]:
        asset = inputs["sources"][cue["asset_id"]]
        children = []
        if cue["start_ms"]:
            children.append(
                {
                    "OTIO_SCHEMA": "Gap.1",
                    "name": "Cue delay",
                    "metadata": {},
                    "source_range": _range(0, cue["start_ms"]),
                    "effects": [],
                    "markers": [],
                }
            )
        children.append(
            _clip(
                cue["cue_id"],
                workspace.asset_path(asset["id"]),
                cue.get("source_start_ms", 0),
                cue["duration_ms"],
                asset["metadata"]["sound_asset"]["duration_ms"],
                cue,
            )
        )
        tracks.append(
            {
                "OTIO_SCHEMA": "Track.1",
                "name": cue["cue_id"],
                "metadata": {},
                "source_range": None,
                "effects": [],
                "markers": [],
                "kind": "Audio",
                "children": children,
            }
        )
    return {
        "OTIO_SCHEMA": "Timeline.1",
        "name": record.get("title", "Cinematic narration mix"),
        "global_start_time": _time(0),
        "metadata": {
            "schema_version": "acoustic-timeline-1",
            "case_id": record["case_id"],
            "plan_id": record["id"],
            "plan_revision": record["revision"],
            "mix": inputs["mix"],
            "gain_application": "Gain, fades, ducking and limiting are recorded as editorial metadata; recreate these in the NLE. They are applied in the rendered WAV.",
        },
        "tracks": {
            "OTIO_SCHEMA": "Stack.1",
            "name": "Audio tracks",
            "metadata": {},
            "source_range": None,
            "effects": [],
            "markers": [],
            "children": tracks,
        },
    }


def _authorize_plan(workspace, record):
    from .acoustic_pipeline import AcousticPipeline

    return AcousticPipeline(workspace).authorize_plan(
        record["case_id"],
        record["id"],
        expected_revision=record["revision"],
        expected_hash=record["content_hash"],
    )


def render_mix(workspace, record):
    """Render exactly the authorized plan, then recheck all inputs before promotion."""
    # Render the canonical approved revision, rather than caller-supplied cues
    # carrying an otherwise valid plan ID and content hash.
    record = _authorize_plan(workspace, record)
    inputs = validate_inputs(workspace, record)
    intervals = _speech_intervals(workspace, inputs)
    staging = workspace.repo.root / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="acoustic-", dir=staging) as temporary:
        directory = Path(temporary)
        arguments = [
            executable("ffmpeg"),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(inputs["narration_path"]),
        ]
        total_samples = inputs["duration_ms"] * 48
        filters = [
            f"[0:a:0]aresample={SAMPLE_RATE},aformat=channel_layouts=stereo,apad=whole_len={total_samples},atrim=end_sample={total_samples},asetpts=N/SR/TB,volume={inputs['mix']['narration_gain_db']}dB[narration]"
        ]
        for number, cue in enumerate(inputs["cues"], 1):
            asset = inputs["sources"][cue["asset_id"]]
            arguments += ["-i", str(workspace.asset_path(asset["id"]))]
            length = cue["duration_ms"] / 1000
            source_sample = cue.get("source_start_ms", 0) * 48
            source_end_sample = source_sample + cue["duration_ms"] * 48
            fades = ""
            if cue["fade_in_ms"]:
                fades += f",afade=t=in:st=0:d={cue['fade_in_ms'] / 1000}"
            if cue["fade_out_ms"]:
                fades += f",afade=t=out:st={length - cue['fade_out_ms'] / 1000:.6f}:d={cue['fade_out_ms'] / 1000}"
            filters.append(
                f"[{number}:a:0]aresample={SAMPLE_RATE},aformat=channel_layouts=stereo,atrim=start_sample={source_sample}:end_sample={source_end_sample},asetpts=N/SR/TB,volume={cue['gain_db']}dB{fades},adelay={cue['start_ms'] * 48}S:all=1,apad=whole_len={total_samples},atrim=end_sample={total_samples}[sound{number}]"
            )
        if inputs["cues"]:
            filters.append(
                "".join(f"[sound{i}]" for i in range(1, len(inputs["cues"]) + 1))
                + f"amix=inputs={len(inputs['cues'])}:duration=longest:normalize=0[effects]"
            )
            if intervals and inputs["mix"]["duck_db"]:
                active = "+".join(
                    f"between(t,{start / 1000:.3f},{end / 1000:.3f})"
                    for start, end in intervals
                )
                factor = 10 ** (inputs["mix"]["duck_db"] / 20)
                filters.append(
                    f"[effects]volume='if(gt({active},0),{factor:.9f},1)':eval=frame[ducked]"
                )
            else:
                filters.append("[effects]anull[ducked]")
            filters.append(
                "[narration][ducked]amix=inputs=2:duration=first:normalize=0[combined]"
            )
        else:
            filters.append("[narration]anull[combined]")
        ceiling = 10 ** (inputs["mix"]["headroom_db"] / 20)
        filters.append(
            f"[combined]alimiter=limit={ceiling:.9f}:attack=5:release=50:level=0:latency=1,apad=whole_len={total_samples},atrim=end_sample={total_samples}[final]"
        )
        output = directory / "final.wav"
        arguments += [
            "-filter_complex_threads",
            "1",
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[final]",
            "-map_metadata",
            "-1",
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            "2",
            "-c:a",
            "pcm_s24le",
            str(output),
        ]
        run_command(arguments, timeout=workspace.settings.command_timeout_seconds)
        measured = probe_wav(output)
        if abs(measured["duration_ms"] - inputs["duration_ms"]) > 2:
            raise SearchError("Rendered acoustic WAV duration failed verification", 422)
        _authorize_plan(workspace, record)
        fresh = validate_inputs(workspace, record)
        if fresh["cues"] != inputs["cues"]:
            raise SearchError("Acoustic cues changed while rendering", 409)
        sources = [
            {
                "asset_id": asset["id"],
                "asset_version_id": asset["asset_version_id"],
                "sha256": asset["sha256"],
                "source_id": asset["source_id"],
                "asset_kind": asset["asset_kind"],
            }
            for asset in inputs["sources"].values()
        ]
        metadata = {
            "case_id": record["case_id"],
            "plan_id": record["id"],
            "plan_revision": record["revision"],
            "plan_hash": record["content_hash"],
            "sources": sources,
            "transcript_artifact_id": inputs["alignment"]["id"],
            "alignment_sha256": inputs["alignment"]["sha256"],
            "requested_use": record.get("requested_use", "internal_review"),
        }
        timeline = {
            "schema_version": "acoustic-timeline-1",
            **metadata,
            "sample_rate": SAMPLE_RATE,
            "duration_ms": inputs["duration_ms"],
            "narration_asset_id": inputs["narration"]["id"],
            "cues": inputs["cues"],
            "disabled_cues": [
                cue for cue in record.get("cues", []) if not cue.get("enabled", True)
            ],
            "mix": inputs["mix"],
            "duck_intervals_ms": intervals,
            "timing_note": "Cue delays use a 48 kHz sample clock; word-based ducking gain updates on FFmpeg audio-frame boundaries. Limiter lookahead delay is compensated.",
        }
        timeline_file = directory / "timeline.json"
        timeline_file.write_text(json_text(timeline), encoding="utf-8")
        otio_file = directory / "timeline.otio"
        otio_file.write_text(
            json_text(_otio_timeline(workspace, record, inputs)), encoding="utf-8"
        )
        promoted = {}
        for key, path, kind, profile in [
            (
                "timeline_artifact_id",
                timeline_file,
                "acoustic_timeline",
                "editable-json-48khz",
            ),
            ("otio_artifact_id", otio_file, "acoustic_otio", "opentimelineio-json"),
            ("artifact_id", output, "acoustic_mix", "48khz-stereo-pcm24"),
        ]:
            artifact = promote_artifact(
                workspace.repo,
                path,
                source_id=inputs["narration"]["source_id"],
                kind=kind,
                profile=profile,
                parent_artifact_id=inputs["narration"]["artifact_id"],
                metadata=metadata,
            )
            promoted[key] = artifact["id"]
        workspace.repo.event(
            "acoustic_mix_complete",
            source_id=inputs["narration"]["source_id"],
            artifact_id=promoted["artifact_id"],
            payload={**metadata, **promoted},
        )
        return {
            "case_id": record["case_id"],
            "plan_id": record["id"],
            "revision": record["revision"],
            **promoted,
            "duration_ms": measured["duration_ms"],
        }


def authorize_mix_artifact(workspace, artifact_id, requested_use="internal_review"):
    artifact = workspace.repo.get("artifacts", artifact_id)
    if not artifact or artifact["kind"] not in {
        "acoustic_mix",
        "acoustic_timeline",
        "acoustic_otio",
    }:
        raise SearchError("Acoustic production artifact not found", 404)
    metadata = artifact.get("metadata", {})
    if not metadata.get("sources") or not metadata.get("plan_id"):
        raise SearchError("Acoustic artifact has no pinned input provenance", 409)
    _authorize_plan(
        workspace,
        {
            "case_id": metadata["case_id"],
            "id": metadata["plan_id"],
            "revision": metadata["plan_revision"],
            "content_hash": metadata["plan_hash"],
        },
    )
    for source in metadata["sources"]:
        asset = workspace.get_asset(source["asset_id"])
        if (
            asset["case_id"] != metadata["case_id"]
            or asset["asset_version_id"] != source["asset_version_id"]
            or asset["sha256"] != source["sha256"]
        ):
            raise SearchError("Acoustic artifact source version was superseded", 409)
        workspace.authorize_asset(
            asset["id"],
            "internal_review" if asset["asset_kind"] == "script" else requested_use,
        )
    alignment = workspace.repo.get("artifacts", metadata.get("transcript_artifact_id"))
    if not alignment or alignment["sha256"] != metadata.get("alignment_sha256"):
        raise SearchError("Acoustic artifact alignment was superseded", 409)
    verified_artifact_path(workspace.repo, alignment)
    return verified_artifact_path(workspace.repo, artifact)
