"""Timestamp-preserving caption parsing and idempotent transcript indexing."""

from __future__ import annotations

import hashlib
import html
import json
import math
import re
from typing import Any

from app.models.search import SearchError
from .repository import decode, json_text, new_id, now


_TIMING = re.compile(
    r"(?P<start>(?:\d{1,3}:)?\d{2}:\d{2}[.,]\d{3})\s*-->\s*(?P<end>(?:\d{1,3}:)?\d{2}:\d{2}[.,]\d{3})"
)


def timestamp_ms(value: str) -> int:
    parts = value.replace(",", ".").split(":")
    if len(parts) not in {2, 3}:
        raise SearchError("Invalid caption timestamp.")
    seconds = float(parts[-1]) + int(parts[-2]) * 60
    if len(parts) == 3:
        seconds += int(parts[0]) * 3600
    return round(seconds * 1000)


def clean_text(value: Any) -> str:
    return re.sub(
        r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", "", str(value)))
    ).strip()


def _validated_cues(cues: list[dict], duration_ms: int | None = None) -> list[dict]:
    result, seen = [], set()
    for cue in cues:
        try:
            start, end = cue["start_ms"], cue["end_ms"]
            if (
                isinstance(start, bool)
                or isinstance(end, bool)
                or not all(
                    isinstance(v, (float, int)) and math.isfinite(v)
                    for v in (start, end)
                )
            ):
                raise ValueError
            start, end = round(start), round(end)
        except (KeyError, ValueError, TypeError):
            raise SearchError(
                "Captions must include finite start_ms and end_ms values."
            ) from None
        text = clean_text(cue.get("text", ""))
        if not text:
            continue
        if start < 0 or end <= start:
            raise SearchError("Caption timestamps are outside the source duration.")
        if duration_ms is not None:
            if start >= duration_ms:
                continue
            end = min(end, duration_ms)
        key = (start, end, text)
        if key not in seen:
            seen.add(key)
            result.append({"start_ms": start, "end_ms": end, "text": text})
    return sorted(result, key=lambda item: (item["start_ms"], item["end_ms"]))


def parse_captions(
    raw: str | dict | list, format: str = "vtt", duration_ms: int | None = None
) -> list[dict]:
    """Parse VTT/SRT, yt-dlp JSON3, or explicitly timestamped local cue rows."""
    if isinstance(raw, list):
        return _validated_cues(raw, duration_ms)
    if format in {"json", "json3"} or isinstance(raw, dict):
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(data, list):
                return _validated_cues(data, duration_ms)
            events = data.get("events", [])
            cues = []
            for index, event in enumerate(events):
                text = "".join(
                    segment.get("utf8", "") for segment in event.get("segs", [])
                )
                if not text.strip():
                    continue
                start = event["tStartMs"]
                duration = event.get("dDurationMs")
                if duration is None and index + 1 < len(events):
                    duration = events[index + 1]["tStartMs"] - start
                if duration is None:
                    continue  # Never make up a missing final timestamp.
                cues.append(
                    {"start_ms": start, "end_ms": start + duration, "text": text}
                )
            return _validated_cues(cues, duration_ms)
        except (AttributeError, KeyError, TypeError, ValueError):
            raise SearchError("The caption JSON is malformed.") from None
    if not isinstance(raw, str):
        raise SearchError("Unsupported caption format.")
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    cues, index = [], 0
    while index < len(lines):
        match = _TIMING.search(lines[index])
        if not match:
            index += 1
            continue
        text_lines = []
        index += 1
        while (
            index < len(lines)
            and lines[index].strip()
            and not _TIMING.search(lines[index])
        ):
            text_lines.append(lines[index])
            index += 1
        cues.append(
            {
                "start_ms": timestamp_ms(match["start"]),
                "end_ms": timestamp_ms(match["end"]),
                "text": " ".join(text_lines),
            }
        )
    return _validated_cues(cues, duration_ms)


def chunk_cues(
    cues: list[dict], target_ms: int = 30_000, overlap_ms: int = 5_000
) -> list[dict]:
    chunks, cursor = [], 0
    while cursor < len(cues):
        start = cues[cursor]["start_ms"]
        stop = cursor + 1
        while stop < len(cues) and cues[stop]["end_ms"] - start <= target_ms:
            # Do not combine unrelated cues separated by a large silence.
            if cues[stop]["start_ms"] - cues[stop - 1]["end_ms"] > 10_000:
                break
            stop += 1
        selected = cues[cursor:stop]
        text_parts = []
        for cue in selected:
            text = cue["text"]
            if not text_parts or text != text_parts[-1]:
                # Rolling auto-captions repeat whole prior lines; preserve evidence
                # once without changing any original cue timings.
                if text_parts and text.startswith(text_parts[-1] + " "):
                    text_parts[-1] = text
                else:
                    text_parts.append(text)
        chunks.append(
            {
                "start_ms": start,
                "end_ms": max(item["end_ms"] for item in selected),
                "text": " ".join(text_parts),
                "cue_ids": [item.get("id") for item in selected if item.get("id")],
            }
        )
        if stop == len(cues):
            break
        next_cursor = stop
        while (
            next_cursor > cursor + 1
            and cues[next_cursor - 1]["start_ms"] >= selected[-1]["end_ms"] - overlap_ms
        ):
            next_cursor -= 1
        cursor = max(cursor + 1, next_cursor)
    return chunks


def ingest_captions(
    repo,
    source_id: str,
    raw: str | dict | list,
    language: str = "en",
    format: str = "vtt",
    kind: str = "manual",
    provider: str = "provided",
) -> dict:
    source = repo.get("sources", source_id)
    if source is None:
        raise SearchError("Source not found.", 404)
    original_cues = parse_captions(raw, format)
    cues = _validated_cues(original_cues, source.get("duration_ms"))
    if not cues:
        raise SearchError("No timestamped caption cues were found.")
    canonical_raw = json_text(cues)
    digest = hashlib.sha256(canonical_raw.encode()).hexdigest()
    identifier = new_id("cap_")
    timestamp = now()
    with repo.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM captions WHERE source_id=? AND language=? AND kind=? AND sha256=?",
            (source_id, language, kind, digest),
        ).fetchone()
        if row is not None:
            if not row["is_active"]:
                connection.execute(
                    "UPDATE captions SET is_active=0 WHERE source_id=? AND language=? AND kind=?",
                    (source_id, language, kind),
                )
                connection.execute(
                    "UPDATE captions SET is_active=1 WHERE id=?", (row["id"],)
                )
                connection.execute(
                    "UPDATE sources SET state='text_indexed',updated_at=? WHERE id=?",
                    (now(), source_id),
                )
            result = decode(row)
            result["is_active"] = 1
            return result
        connection.execute(
            "UPDATE captions SET is_active=0 WHERE source_id=? AND language=? AND kind=?",
            (source_id, language, kind),
        )
        connection.execute(
            "INSERT INTO captions(id,source_id,language,kind,provider,sha256,raw_text,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                identifier,
                source_id,
                language,
                kind,
                provider,
                digest,
                json_text(original_cues),
                timestamp,
            ),
        )
        for cue in cues:
            cue["id"] = new_id("cue_")
            cue_hash = hashlib.sha256(
                json_text(
                    {key: cue[key] for key in ("start_ms", "end_ms", "text")}
                ).encode()
            ).hexdigest()
            connection.execute(
                "INSERT INTO caption_cues VALUES(?,?,?,?,?,?,?,?)",
                (
                    cue["id"],
                    identifier,
                    source_id,
                    cue["start_ms"],
                    cue["end_ms"],
                    cue["text"],
                    cue_hash,
                    timestamp,
                ),
            )
        for chunk in chunk_cues(cues):
            chunk_hash = hashlib.sha256(json_text(chunk).encode()).hexdigest()
            connection.execute(
                "INSERT INTO transcript_chunks VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    new_id("chunk_"),
                    source_id,
                    identifier,
                    chunk["start_ms"],
                    chunk["end_ms"],
                    chunk["text"],
                    language,
                    chunk_hash,
                    json_text(chunk["cue_ids"]),
                    timestamp,
                ),
            )
        connection.execute(
            "UPDATE sources SET state='text_indexed',updated_at=? WHERE id=?",
            (timestamp, source_id),
        )
    trimmed = sum(
        1
        for cue in original_cues
        if source.get("duration_ms") is not None
        and cue["start_ms"] < source["duration_ms"] < cue["end_ms"]
    )
    repo.event(
        "captions_indexed",
        source_id=source_id,
        payload={
            "caption_id": identifier,
            "cue_count": len(cues),
            "caption_sha256": digest,
            "cues_trimmed_to_source": trimmed,
            "cues_outside_source_skipped": len(original_cues) - len(cues),
        },
    )
    return repo.get("captions", identifier)
