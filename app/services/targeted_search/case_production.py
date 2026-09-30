"""Render explicitly bound case scenes and retain source-to-output provenance."""

from __future__ import annotations

import hashlib
import math
import tempfile
from pathlib import Path

from app.models.search import SearchError
from .media import executable, promote_artifact, run_command, verified_artifact_path
from .policy import authorize
from .repository import json_text


def _asset(workspace, identifier: str, requested_use: str) -> tuple[dict, Path]:
    asset = workspace.get_asset(identifier)
    authorize(workspace.repo, asset["source_id"], requested_use)
    return asset, workspace.asset_path(identifier)


def _probe(path: Path) -> dict:
    import json

    return json.loads(
        run_command(
            [
                executable("ffprobe"),
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ],
            timeout=30,
        ).stdout
    )


def _tempo(speed: float) -> str:
    parts = []
    while speed > 2:
        parts.append("atempo=2")
        speed /= 2
    while speed < 0.5:
        parts.append("atempo=0.5")
        speed *= 2
    parts.append(f"atempo={speed:.8f}")
    return ",".join(parts)


def _number(value, default, low, high, name):
    value = default if value is None else value
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not low <= value <= high
    ):
        raise SearchError(f"Invalid storyboard {name}", 422)
    return float(value)


def _picture(
    workspace, asset: dict, path: Path, locator: dict, requested_use: str
) -> Path:
    if asset["asset_kind"] == "document":
        from .case_media import preview_asset

        preview = preview_asset(
            workspace,
            asset["id"],
            {"kind": "page", "page_index": locator.get("page_index", 0)},
            requested_use=requested_use,
        )
        return verified_artifact_path(workspace.repo, preview)
    if asset["asset_kind"] in {"image", "map"} and path.suffix.lower() not in {
        ".json",
        ".geojson",
    }:
        return path
    raise SearchError(
        "Choose a rendered map/image or a document page for this visual", 422
    )


def _crop(locator: dict) -> str:
    box = locator.get("bbox")
    if not box:
        return ""
    if (
        len(box) != 4
        or not all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            for value in box
        )
        or not 0 <= box[0] < box[2] <= 1
        or not 0 <= box[1] < box[3] <= 1
    ):
        raise SearchError(
            "Storyboard crop requires normalized top-left coordinates", 422
        )
    x0, y0, x1, y1 = box
    return f"crop=iw*{x1 - x0}:ih*{y1 - y0}:iw*{x0}:ih*{y0}:exact=1,"


def _segment(
    workspace, scene: dict, directory: Path, number: int, requested_use: str
) -> tuple[Path, dict, list[dict]]:
    asset, path = _asset(workspace, scene["asset_id"], requested_use)
    if (
        scene.get("asset_version_id", asset["asset_version_id"])
        != asset["asset_version_id"]
        or scene.get("input_sha256", asset["sha256"]) != asset["sha256"]
    ):
        raise SearchError(
            "Storyboard source changed; review and save its current version", 409
        )
    speed = _number(scene.get("speed"), 1, 0.25, 4, "playback speed")
    volume = _number(scene.get("volume"), 1, 0, 4, "volume")
    duration = _number(scene.get("duration_ms"), None, 1, 300000, "duration") / 1000
    role = scene.get("role", "broll")
    if role not in {"broll", "original_sound", "still", "document", "map"}:
        raise SearchError("Unsupported storyboard role", 422)
    locator = scene.get("locator") or {}
    for key in ("start_ms", "end_ms"):
        if (
            locator.get(key) is not None
            and scene.get("source_" + key) is not None
            and locator[key] != scene["source_" + key]
        ):
            raise SearchError("Scene range and citation locator disagree", 422)
    start = (
        _number(
            scene.get("source_start_ms"),
            locator.get("start_ms", 0),
            0,
            86400000,
            "source start",
        )
        / 1000
    )
    end = (
        _number(
            scene.get("source_end_ms"),
            locator.get("end_ms", (start + duration * speed) * 1000),
            1,
            86400000,
            "source end",
        )
        / 1000
    )
    inputs = []
    filters = []
    members = [asset]
    kind = asset["asset_kind"]
    original_audio = role == "original_sound"
    channel_filter = ""
    if kind in {"video", "audio"}:
        raw = _probe(path)
        measured = float(raw.get("format", {}).get("duration", 0))
        if (
            end <= start
            or end > measured + 0.03
            or (end - start) / speed + 0.03 < duration
        ):
            raise SearchError(
                "Storyboard source range is outside the recording or too short", 422
            )
        inputs += ["-ss", f"{start:.6f}", "-t", f"{end - start:.6f}", "-i", str(path)]
        has_audio = any(
            stream.get("codec_type") == "audio" for stream in raw.get("streams", [])
        )
        channel = locator.get("channel")
        if channel is not None:
            streams = [
                stream
                for stream in raw.get("streams", [])
                if stream.get("codec_type") == "audio"
            ]
            if (
                isinstance(channel, bool)
                or not isinstance(channel, int)
                or not streams
                or not 0 <= channel < streams[0].get("channels", 0)
            ):
                raise SearchError(
                    "Choose a numeric channel index from the source audio stream", 422
                )
            channel_filter = f"pan=mono|c0=c{channel},"
        if kind == "video":
            filters.append(
                f"[0:v:0]setpts=(PTS-STARTPTS)/{speed},{_crop(locator)}scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=25,trim=duration={duration}[v]"
            )
        else:
            if not original_audio:
                raise SearchError("Audio scenes require the original_sound role", 422)
            visual_id = scene.get("visual_asset_id")
            if visual_id:
                visual, visual_path = _asset(workspace, visual_id, requested_use)
                if visual["case_id"] != asset["case_id"]:
                    raise SearchError("Scene background belongs to another case", 422)
                if (
                    scene.get("visual_asset_version_id", visual["asset_version_id"])
                    != visual["asset_version_id"]
                    or scene.get("visual_sha256", visual["sha256"]) != visual["sha256"]
                ):
                    raise SearchError(
                        "Storyboard background changed; review its current version", 409
                    )
                members.append(visual)
                visual_path = _picture(
                    workspace,
                    visual,
                    visual_path,
                    scene.get("visual_locator") or {},
                    requested_use,
                )
                inputs += ["-loop", "1", "-i", str(visual_path)]
                filters.append(
                    f"[1:v]{_crop(scene.get('visual_locator') or {})}scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=25,trim=duration={duration},setpts=PTS-STARTPTS[v]"
                )
            else:
                filters.append(f"color=c=black:s=1280x720:r=25:d={duration}[v]")
        if original_audio and not has_audio:
            raise SearchError("Original-sound scene has no audio stream", 422)
        if original_audio:
            filters.append(
                f"[0:a:0]asetpts=PTS-STARTPTS,{channel_filter}{_tempo(speed)},volume={volume},apad,atrim=duration={duration},aformat=sample_rates=48000:channel_layouts=stereo[a]"
            )
        else:
            filters.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={duration}[a]")
    elif kind in {"document", "image", "map"}:
        picture = _picture(
            workspace, asset, path, scene.get("locator") or {}, requested_use
        )
        inputs += ["-loop", "1", "-i", str(picture)]
        filters += [
            f"[0:v]{_crop(locator)}scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=25,trim=duration={duration},setpts=PTS-STARTPTS[v]",
            f"anullsrc=r=48000:cl=stereo,atrim=duration={duration}[a]",
        ]
    else:
        raise SearchError(
            "Production drafts and unrendered geographic data cannot become footage",
            422,
        )
    output = directory / f"scene-{number:03d}.mp4"
    run_command(
        [
            executable("ffmpeg"),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            *inputs,
            "-filter_complex_threads",
            "1",
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-t",
            str(duration),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "22",
            "-pix_fmt",
            "yuv420p",
            "-threads",
            "2",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(output),
        ],
        timeout=1800,
    )
    rendered_ms = round(float(_probe(output)["format"]["duration"]) * 1000)
    mapping = {
        "scene_id": scene["scene_id"],
        "asset_id": asset["id"],
        "asset_version_id": asset["asset_version_id"],
        "sha256": asset["sha256"],
        "source_start_ms": round(start * 1000) if kind in {"video", "audio"} else None,
        "source_end_ms": round((start + duration * speed) * 1000)
        if kind in {"video", "audio"}
        else None,
        "duration_ms": rendered_ms,
        "requested_duration_ms": round(duration * 1000),
        "output_video_timebase": "1/25",
        "speed": speed,
        "volume": volume,
        "role": role,
        "locator": scene.get("locator", {}),
        "citations": scene.get("citations", []),
        "claim_ids": scene.get("claim_ids", []),
        "original_audio": original_audio,
        "selected_audio_stream": 0 if original_audio else None,
        "selected_audio_channel": locator.get("channel") if original_audio else None,
        "visual_asset_id": scene.get("visual_asset_id"),
        "visual_locator": scene.get("visual_locator"),
        "visual_asset_version_id": scene.get("visual_asset_version_id"),
        "visual_sha256": scene.get("visual_sha256"),
    }
    return output, mapping, members


def render_storyboard(
    workspace,
    storyboard_id: str,
    requested_use: str = "generated_export",
    expected_hash: str | None = None,
) -> dict:
    storyboard = workspace.get_storyboard(storyboard_id)
    digest = storyboard.get("storyboard_hash") or storyboard.get("content_hash")
    if expected_hash and digest != expected_hash:
        raise SearchError("Storyboard changed after this render was queued", 409)
    scenes = storyboard.get("scenes", [])
    if not scenes or len(scenes) > 80:
        raise SearchError("A storyboard needs between one and 80 bound scenes", 422)
    total = sum(
        _number(item.get("duration_ms"), None, 1, 300000, "duration") for item in scenes
    )
    if total > 3600000:
        raise SearchError("Case renders are limited to one hour", 422)
    source_assets = {}
    script_binding = storyboard.get("metadata") or {}
    script_id = script_binding.get("script_asset_id")
    if script_id:
        script_asset, script_path = _asset(workspace, script_id, "internal_review")
        if (
            script_asset["case_id"] != storyboard["case_id"]
            or script_asset["asset_kind"] != "script"
        ):
            raise SearchError(
                "Choose the matching production script from this case", 422
            )
        if (
            script_binding.get("script_asset_version_id")
            != script_asset["asset_version_id"]
            or script_binding.get("script_asset_sha256") != script_asset["sha256"]
        ):
            raise SearchError(
                "Storyboard script changed; review and save its current version", 409
            )
        if (
            storyboard.get("script", "").strip()
            != script_path.read_text(encoding="utf-8").strip()
        ):
            raise SearchError(
                "Script binding does not match the storyboard script", 409
            )
        source_assets[script_id] = script_asset
    mappings = []
    staging = workspace.repo.root / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="case-render-", dir=staging) as temp:
        directory = Path(temp)
        offset = 0
        for index, scene in enumerate(scenes):
            if (
                workspace.get_asset(scene["asset_id"])["case_id"]
                != storyboard["case_id"]
            ):
                raise SearchError("Storyboard asset belongs to another case", 422)
            _, mapping, members = _segment(
                workspace, scene, directory, index, requested_use
            )
            requested_offset = scene.get("output_start_ms")
            if requested_offset is not None and requested_offset != offset:
                raise SearchError(
                    "Scene output offsets must form a contiguous storyboard", 422
                )
            mapping.update(
                output_start_ms=offset, output_end_ms=offset + mapping["duration_ms"]
            )
            offset += mapping["duration_ms"]
            mappings.append(mapping)
            source_assets.update({member["id"]: member for member in members})
        total = offset
        concat_file = directory / "segments.txt"
        concat_file.write_text(
            "\n".join(f"file 'scene-{i:03d}.mp4'" for i in range(len(scenes))),
            encoding="utf-8",
        )
        output = directory / "render.mp4"
        run_command(
            [
                executable("ffmpeg"),
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "concat",
                "-safe",
                "1",
                "-i",
                str(concat_file),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(output),
            ],
            timeout=1800,
        )
        narration_id = storyboard.get("narration_asset_id")
        narration_mappings, narration_words = [], []
        alignment_id = None
        if narration_id:
            narration, narration_path = _asset(workspace, narration_id, requested_use)
            if narration["case_id"] != storyboard["case_id"] or narration[
                "asset_kind"
            ] not in {"audio", "video"}:
                raise SearchError("Choose a narration recording from this case", 422)
            if (
                storyboard.get(
                    "narration_asset_version_id", narration["asset_version_id"]
                )
                != narration["asset_version_id"]
                or storyboard.get("narration_sha256", narration["sha256"])
                != narration["sha256"]
            ):
                raise SearchError(
                    "Storyboard narration changed; review its current version", 409
                )
            source_assets[narration_id] = narration
            # A continuous narration take pauses for source-sound inserts. Its
            # words resume on the next visual rather than being muted/dropped.
            narration_probe = _probe(narration_path)
            audio_streams = [
                stream
                for stream in narration_probe.get("streams", [])
                if stream.get("codec_type") == "audio"
            ]
            if not audio_streams:
                raise SearchError("Narration recording has no audio stream", 422)
            narration_ms = round(
                float(
                    audio_streams[0].get(
                        "duration", narration_probe["format"]["duration"]
                    )
                )
                * 1000
            )
            allocation = sum(
                row["duration_ms"] for row in mappings if not row["original_audio"]
            )
            if narration_ms > allocation + 40:
                raise SearchError(
                    "The whole narration take must fit the nonquote scene durations; extend the visual scenes",
                    422,
                )
            offset = 0
            for row in mappings:
                if row["original_audio"]:
                    continue
                end = min(offset + row["duration_ms"], narration_ms)
                if end > offset:
                    narration_mappings.append(
                        {
                            "asset_id": narration_id,
                            "asset_version_id": narration["asset_version_id"],
                            "sha256": narration["sha256"],
                            "source_start_ms": offset,
                            "source_end_ms": end,
                            "output_start_ms": row["output_start_ms"],
                            "output_end_ms": row["output_start_ms"] + end - offset,
                            "speed": 1,
                        }
                    )
                offset += row["duration_ms"]
            if not narration_mappings:
                raise SearchError(
                    "Narration needs at least one visual scene outside source-sound inserts",
                    422,
                )
            with workspace.repo.connect() as connection:
                alignments = connection.execute(
                    "SELECT record_json FROM case_transcripts WHERE asset_version_id=? AND scope='narration' ORDER BY created_at DESC",
                    (narration["asset_version_id"],),
                ).fetchall()
            import json

            for alignment in alignments:
                record = json.loads(alignment[0])
                if not script_id or record.get("script_asset_id") != script_id:
                    continue
                script_asset = source_assets[script_id]
                if (
                    record.get("audio_sha256") != narration["sha256"]
                    or record.get("script_sha256") != script_asset["sha256"]
                    or record.get("script_asset_version_id")
                    != script_asset["asset_version_id"]
                ):
                    raise SearchError(
                        "Narration alignment references a superseded script or audio version",
                        409,
                    )
                alignment_id = record["transcript_artifact_id"]
                verified_artifact_path(
                    workspace.repo, workspace.repo.get("artifacts", alignment_id)
                )
                for word in record.get("words", []):
                    ranges = []
                    if word.get("start_ms") is not None:
                        for interval in narration_mappings:
                            start = max(word["start_ms"], interval["source_start_ms"])
                            end = min(word["end_ms"], interval["source_end_ms"])
                            if end > start:
                                ranges.append(
                                    {
                                        "start_ms": interval["output_start_ms"]
                                        + start
                                        - interval["source_start_ms"],
                                        "end_ms": interval["output_start_ms"]
                                        + end
                                        - interval["source_start_ms"],
                                    }
                                )
                    narration_words.append({**word, "output_ranges": ranges})
                break
            count = len(narration_mappings)
            audio_filters = [
                "[1:a:0]asplit=" + str(count) + "".join(f"[n{i}]" for i in range(count))
            ]
            for index, interval in enumerate(narration_mappings):
                audio_filters.append(
                    f"[n{index}]atrim=start={interval['source_start_ms'] / 1000}:end={interval['source_end_ms'] / 1000},asetpts=PTS-STARTPTS,adelay={interval['output_start_ms']}:all=1[placed{index}]"
                )
            audio_filters.append(
                "[0:a:0]"
                + "".join(f"[placed{i}]" for i in range(count))
                + f"amix=inputs={count + 1}:duration=first:normalize=0[a]"
            )
            narrated = directory / "narrated.mp4"
            run_command(
                [
                    executable("ffmpeg"),
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(output),
                    "-i",
                    str(narration_path),
                    "-filter_complex_threads",
                    "1",
                    "-filter_complex",
                    ";".join(audio_filters),
                    "-map",
                    "0:v:0",
                    "-map",
                    "[a]",
                    "-c:v",
                    "copy",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-t",
                    str(total / 1000),
                    "-movflags",
                    "+faststart",
                    str(narrated),
                ],
                timeout=1800,
            )
            output = narrated
        # Recheck immediately before delivery; revocation during rendering stops
        # the output promotion even when the underlying originals are intact.
        for member in source_assets.values():
            current_member, _ = _asset(
                workspace,
                member["id"],
                "internal_review"
                if member["asset_kind"] == "script"
                else requested_use,
            )
            if (
                current_member["asset_version_id"] != member["asset_version_id"]
                or current_member["sha256"] != member["sha256"]
            ):
                raise SearchError("Production input changed while rendering", 409)
        current = workspace.get_storyboard(storyboard_id)
        if (current.get("storyboard_hash") or current.get("content_hash")) != digest:
            raise SearchError("Storyboard changed while rendering", 409)
        if alignment_id:
            verified_artifact_path(
                workspace.repo, workspace.repo.get("artifacts", alignment_id)
            )
        sources = [
            {
                "asset_id": item["id"],
                "asset_version_id": item["asset_version_id"],
                "source_id": item["source_id"],
                "sha256": item["sha256"],
                "asset_kind": item["asset_kind"],
            }
            for item in source_assets.values()
        ]
        final_probe = _probe(output)
        actual_ms = round(float(final_probe["format"]["duration"]) * 1000)
        mux_timing = [
            {
                "stream_index": stream["index"],
                "kind": stream["codec_type"],
                "start_ms": round(float(stream.get("start_time", 0)) * 1000),
                "duration_ms": round(
                    float(stream.get("duration", final_probe["format"]["duration"]))
                    * 1000
                ),
                "time_base": stream.get("time_base"),
            }
            for stream in final_probe["streams"]
        ]
        manifest = {
            "schema_version": "case-production-1",
            "case_id": storyboard["case_id"],
            "storyboard_id": storyboard_id,
            "storyboard_hash": digest,
            "requested_use": requested_use,
            "duration_ms": actual_ms,
            "planned_duration_ms": round(total),
            "timeline_clock": "planned_sequence_ms",
            "mux_timing": mux_timing,
            "timing_note": "Frame quantization and AAC mux start offsets are recorded separately from the planned scene clock.",
            "scenes": mappings,
            "sources": sources,
            "narration_asset_id": narration_id,
            "narration_time_mappings": narration_mappings,
            "narration_words": narration_words,
            "narration_alignment_artifact_id": alignment_id,
            "script_text_sha256": hashlib.sha256(
                storyboard.get("script", "").encode()
            ).hexdigest(),
            "script_asset_id": script_id,
            "script_asset_sha256": source_assets[script_id]["sha256"]
            if script_id
            else None,
        }
        master = source_assets[scenes[0]["asset_id"]]
        metadata = {
            "case_id": storyboard["case_id"],
            "storyboard_id": storyboard_id,
            "storyboard_hash": digest,
            "sources": sources,
            "requested_use": requested_use,
        }
        manifest_file = directory / "source-usage.json"
        manifest_file.write_text(json_text(manifest), encoding="utf-8")
        evidence = promote_artifact(
            workspace.repo,
            manifest_file,
            source_id=master["source_id"],
            kind="case_render_manifest",
            profile="case-production-1",
            parent_artifact_id=master["artifact_id"],
            metadata=metadata,
        )
        metadata["manifest_artifact_id"] = evidence["id"]
        artifact = promote_artifact(
            workspace.repo,
            output,
            source_id=master["source_id"],
            kind="case_render",
            profile="720p25-h264-aac",
            parent_artifact_id=master["artifact_id"],
            metadata=metadata,
        )
        workspace.repo.event(
            "case_render_complete",
            source_id=master["source_id"],
            artifact_id=artifact["id"],
            payload={
                "case_id": storyboard["case_id"],
                "storyboard_id": storyboard_id,
                "manifest_artifact_id": evidence["id"],
            },
        )
        return {
            "case_id": storyboard["case_id"],
            "storyboard_id": storyboard_id,
            "artifact_id": artifact["id"],
            "manifest_artifact_id": evidence["id"],
            "duration_ms": actual_ms,
        }


def authorize_render_artifact(
    workspace, artifact_id: str, requested_use: str = "generated_export"
) -> Path:
    artifact = workspace.repo.get("artifacts", artifact_id)
    if not artifact or artifact["kind"] not in {"case_render", "case_render_manifest"}:
        raise SearchError("Case production artifact not found", 404)
    for source in artifact.get("metadata", {}).get("sources", []):
        asset, _ = _asset(
            workspace,
            source["asset_id"],
            "internal_review" if source["asset_kind"] == "script" else requested_use,
        )
        if (
            asset["sha256"] != source["sha256"]
            or asset["asset_version_id"] != source["asset_version_id"]
        ):
            raise SearchError("Production input version was superseded", 409)
    if not artifact.get("metadata", {}).get("sources"):
        raise SearchError("Production provenance is missing", 409)
    return verified_artifact_path(workspace.repo, artifact)
