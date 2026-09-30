"""Explicitly registered, version-pinned WAV assets for editorial sound design."""

from __future__ import annotations

import hashlib
import json
import math
import re

from app.models.search import SearchError

from .media import executable, run_command
from .repository import json_text, new_id, now


TAXONOMY = {
    "impact": ("hit", "boom", "thud", "slam", "reveal"),
    "riser": ("rise", "build", "crescendo", "escalation"),
    "drone": ("suspense", "tension", "ominous", "dark", "low", "dread"),
    "pulse": ("heartbeat", "rhythm", "tick", "pressure"),
    "ambience": ("room", "wind", "atmosphere", "ambient", "environment"),
    "transition": ("whoosh", "sweep", "swish", "cut", "change"),
    "foley": ("footstep", "door", "paper", "object", "movement"),
    "sting": ("accent", "stinger", "punctuation", "surprise"),
}
ALIASES = {
    term: category
    for category, terms in TAXONOMY.items()
    for term in (category, *terms)
}
SOUND_ROLES = frozenset({"sound_effect", "sfx"})


def canonical_category(value: str) -> str:
    category = ALIASES.get(str(value).strip().casefold())
    if category is None:
        raise SearchError("Choose a recognized sound-effect category", 422)
    return category


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.casefold()))


def probe_wav(path) -> dict:
    """Require an actual uncompressed WAV, rather than trusting a suffix."""
    if path.suffix.casefold() != ".wav":
        raise SearchError("Sound design requires a retained WAV file", 422)
    raw = json.loads(
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
    try:
        streams = raw.get("streams", [])
        stream = streams[0]
        duration = float(raw["format"]["duration"])
        channels = int(stream["channels"])
        sample_rate = int(stream["sample_rate"])
        if (
            raw["format"].get("format_name") != "wav"
            or len(streams) != 1
            or stream.get("codec_type") != "audio"
            or not stream.get("codec_name", "").startswith("pcm_")
            or not math.isfinite(duration)
            or not 0 < duration <= 10800
            or channels not in {1, 2}
            or not 8000 <= sample_rate <= 192000
        ):
            raise ValueError
        return {
            "duration_ms": round(duration * 1000),
            "channels": channels,
            "sample_rate": sample_rate,
            "codec": stream["codec_name"],
        }
    except (KeyError, ValueError, TypeError, IndexError) as exc:
        raise SearchError("Use a playable mono or stereo PCM WAV file", 422) from exc


def _description(asset, sound):
    return " ".join(
        [sound["category"], *sound["tags"], sound["description"], asset["filename"]]
    )[:6000]


def _fingerprint(asset, sound):
    return hashlib.sha256(
        json_text([asset["asset_version_id"], asset["sha256"], sound]).encode()
    ).hexdigest()


def _vector(workspace, text):
    from .retrieval import _revision, _text_model, normalized

    settings = workspace.settings
    model = _text_model(
        settings.embedding_model,
        settings.embedding_revision,
        settings.local_models_only,
    )
    values = normalized(
        model.encode([text], normalize_embeddings=True, show_progress_bar=False)[0]
    )
    return values, _revision(model, settings.embedding_revision)


def register_sound(workspace, asset_id, tags, description="", category="impact"):
    """Registration is an explicit editorial designation, never automatic ASR."""
    asset = workspace.get_asset(asset_id)
    if asset["asset_kind"] != "audio" or not asset["artifact_id"]:
        raise SearchError("Register a retained audio WAV as a sound effect", 422)
    workspace.authorize_asset(asset_id, "analysis")
    workspace.authorize_asset(asset_id, "internal_review")
    with workspace.repo.connect() as connection:
        if connection.execute(
            "SELECT 1 FROM case_citations WHERE asset_id=? AND record_type IN ('claim','case_event') LIMIT 1",
            (asset_id,),
        ).fetchone():
            raise SearchError(
                "Evidence cited for case facts cannot be repurposed as an editorial sound effect",
                409,
            )
    if not isinstance(tags, (list, tuple)) or not 1 <= len(tags) <= 40:
        raise SearchError("Sound assets need between one and 40 descriptive tags", 422)
    normalized_tags = []
    for value in tags:
        if not isinstance(value, str) or not re.fullmatch(
            r"[a-zA-Z0-9][a-zA-Z0-9 _-]{0,63}", value.strip()
        ):
            raise SearchError("Use short plain-text sound tags", 422)
        normalized_tags.append(value.strip().casefold())
    if not isinstance(description, str) or len(description) > 4000:
        raise SearchError("Sound descriptions must be bounded plain text", 422)
    sound = {
        "category": canonical_category(category),
        "tags": sorted(set(normalized_tags)),
        "description": description.strip(),
        "asset_version_id": asset["asset_version_id"],
        "sha256": asset["sha256"],
        **probe_wav(workspace.asset_path(asset_id)),
    }
    values, model_revision = (
        _vector(workspace, _description(asset, sound))
        if workspace.settings.semantic_enabled
        else (None, None)
    )
    workspace.authorize_asset(asset_id, "analysis")
    workspace.authorize_asset(asset_id, "internal_review")
    metadata = {
        **asset.get("metadata", {}),
        "role": "sound_effect",
        "sound_asset": sound,
    }
    with workspace.repo.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        workspace._assert_asset_snapshot(connection, asset)
        connection.execute(
            "UPDATE case_assets SET metadata_json=?,updated_at=? WHERE id=?",
            (json_text(metadata), now(), asset_id),
        )
        if values:
            connection.execute(
                "INSERT OR IGNORE INTO embeddings VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    new_id("emb_"),
                    "sound_metadata",
                    asset_id,
                    asset["source_id"],
                    "text",
                    workspace.settings.embedding_model,
                    model_revision,
                    len(values),
                    json_text(values),
                    _fingerprint(asset, sound),
                    now(),
                ),
            )
    workspace.repo.event(
        "sound_asset_registered",
        source_id=asset["source_id"],
        payload={
            "case_id": asset["case_id"],
            "asset_id": asset_id,
            "asset_version_id": asset["asset_version_id"],
            "category": sound["category"],
        },
    )
    return workspace.get_asset(asset_id)


def validate_sound(workspace, asset_id, requested_use="internal_review"):
    asset = workspace.get_asset(asset_id)
    sound = asset.get("metadata", {}).get("sound_asset", {})
    if (
        asset["asset_kind"] != "audio"
        or asset.get("metadata", {}).get("role") not in SOUND_ROLES
        or not isinstance(sound, dict)
        or sound.get("asset_version_id") != asset["asset_version_id"]
        or sound.get("sha256") != asset["sha256"]
        or sound.get("category") not in TAXONOMY
        or not isinstance(sound.get("tags"), list)
        or not 1 <= len(sound["tags"]) <= 40
        or any(not isinstance(tag, str) or len(tag) > 64 for tag in sound["tags"])
        or not isinstance(sound.get("description"), str)
        or len(sound["description"]) > 4000
    ):
        raise SearchError(
            "The sound asset needs registration for its current WAV version", 409
        )
    workspace.authorize_asset(asset_id, requested_use)
    measured = probe_wav(workspace.asset_path(asset_id))
    if any(
        measured.get(key) != sound.get(key)
        for key in ("duration_ms", "channels", "sample_rate", "codec")
    ):
        raise SearchError("Sound file properties changed after registration", 409)
    return asset


def list_sounds(workspace, case_id):
    """Unavailable and obsolete assets are skipped, with no stale vectors used."""
    result = []
    for asset in workspace.list_assets(case_id):
        if asset.get("metadata", {}).get("role") not in SOUND_ROLES:
            continue
        try:
            workspace.authorize_asset(asset["id"], "analysis")
            result.append(validate_sound(workspace, asset["id"]))
        except SearchError:
            continue
    return result


def match_sound(workspace, case_id, cue):
    category = canonical_category(cue.get("category", ""))
    query = cue.get("query", "")
    if not isinstance(query, str) or len(query) > 1000:
        raise SearchError("Sound search query exceeds its text budget", 422)
    candidates = list_sounds(workspace, case_id)
    if not candidates:
        raise SearchError(
            "Register permitted WAV sound assets before matching cues", 422
        )
    query_words = _tokens(query) | {category}
    query_vector, model_revision = (
        _vector(workspace, category + " " + query)
        if workspace.settings.semantic_enabled
        else (None, None)
    )
    scored = []
    for asset in candidates:
        sound = asset["metadata"]["sound_asset"]
        same_category = sound["category"] == category
        if not same_category:
            continue
        words = _tokens(_description(asset, sound))
        overlap = len(query_words & words) / max(1, len(query_words))
        cosine, used_vector = 0, False
        if query_vector:
            with workspace.repo.connect() as connection:
                rows = connection.execute(
                    "SELECT vector_json,input_hash FROM embeddings WHERE entity_type='sound_metadata' AND entity_id=? AND model_name=? AND model_revision=? ORDER BY created_at DESC",
                    (asset["id"], workspace.settings.embedding_model, model_revision),
                ).fetchall()
            for row in rows:
                if row["input_hash"] != _fingerprint(asset, sound):
                    continue
                values = json.loads(row["vector_json"])
                if len(values) != len(query_vector) or any(
                    not math.isfinite(float(value)) for value in values
                ):
                    continue
                cosine = max(
                    0, sum(left * right for left, right in zip(values, query_vector))
                )
                used_vector = True
                break
        score = 0.55 + 0.30 * overlap + 0.15 * cosine
        scored.append((score, asset["id"], asset, used_vector))
    if not scored:
        raise SearchError(
            "No sound asset matches this cue; add a suitable WAV or edit its query", 422
        )
    score, _, asset, used_vector = sorted(scored, key=lambda item: (-item[0], item[1]))[
        0
    ]
    # A rights change or replacement during vector inference cannot yield a stale match.
    fresh = validate_sound(workspace, asset["id"])
    workspace.authorize_asset(asset["id"], "analysis")
    if (
        fresh["asset_version_id"] != asset["asset_version_id"]
        or fresh["metadata"].get("sound_asset") != asset["metadata"]["sound_asset"]
    ):
        raise SearchError("Sound asset changed during matching; repeat the search", 409)
    sound = fresh["metadata"]["sound_asset"]
    return {
        "asset_id": asset["id"],
        "asset_version_id": asset["asset_version_id"],
        "sha256": asset["sha256"],
        "duration_ms": sound["duration_ms"],
        "tags": sound["tags"],
        "category": sound["category"],
        "score": round(score, 6),
        "method": "taxonomy+text_vector" if used_vector else "taxonomy+tags",
        "sound_metadata_sha256": _fingerprint(fresh, sound),
    }
