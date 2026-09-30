"""Bind search artifacts to generation without trusting client provenance."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from app.models.schema import MaterialInfo
from app.utils import utils


def prepare_search_materials(params, task_id: str) -> list[dict]:
    """Resolve registered artifacts and persist their verified task provenance."""
    from app.services.targeted_search.attachments import attach_clip, resolve_attachments

    selected = list(dict.fromkeys(getattr(params, "search_artifact_ids", []) or []))
    materials = list(params.video_materials or [])
    if selected and params.video_source != "local":
        raise ValueError("Search clips require the Local video material source")
    attached_ids = {getattr(item, "artifact_id", None) for item in materials}
    for artifact_id in selected:
        if artifact_id in attached_ids:
            continue
        reference = attach_clip(artifact_id, requested_use="generated_export")
        materials.append(MaterialInfo(
            provider="local", url=reference.get("local_file") or reference.get("url", ""),
            duration=reference.get("duration", 0),
            artifact_id=artifact_id,
        ))
    provenance = resolve_attachments(materials, task_id, requested_use="generated_export")
    if provenance:
        from app.services.task_artifacts import _write_json_atomic

        _write_json_atomic(Path(utils.task_dir(task_id)) / "search-materials.json", {
            "schema_version": "1.0", "task_id": task_id, "materials": provenance,
        })
    params.video_materials = materials
    return provenance


def authorize_task_sources(task_id: str, requested_use: str = "generated_export") -> None:
    """Recheck live policies before delivery or publication of derived output."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", task_id):
        raise ValueError("Task identifier is invalid")
    from app.services.targeted_search.settings import storage_root

    database = storage_root() / "search.sqlite3"
    registered = []
    if database.is_file():
        try:
            with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=10) as connection:
                registered = [MaterialInfo(provider="local", artifact_id=row[0], url=row[1])
                              for row in connection.execute(
                                  "SELECT DISTINCT artifact_id,local_path FROM attachments WHERE task_id=?", (task_id,))]
        except sqlite3.Error:
            raise ValueError("Task source registrations could not be verified") from None
    manifest = Path(utils.task_dir()) / task_id / "search-materials.json"
    if not manifest.is_file():
        if registered:
            from app.services.targeted_search.attachments import authorize_attachments

            authorize_attachments(registered, requested_use=requested_use)
        return
    from app.services.targeted_search.attachments import authorize_attachments

    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("materials"), list):
            raise ValueError("Task search provenance is invalid")
        materials = []
        for entry in payload["materials"]:
            if not isinstance(entry, dict):
                raise ValueError("Task search provenance is invalid")
            artifact_id = entry.get("material_artifact_id") or entry.get("clip_id") or entry.get("artifact_id")
            if not artifact_id:
                raise ValueError("Task search provenance is missing its artifact reference")
            materials.append(MaterialInfo(
                provider="local", artifact_id=artifact_id,
                url=entry.get("local_path") or entry.get("local_file") or entry.get("url", ""),
            ))
        if not materials:
            raise ValueError("Task search provenance is empty")
    except (KeyError, TypeError, json.JSONDecodeError):
        raise ValueError("Task search provenance is invalid") from None
    # The database remains canonical even if a sidecar loses one reference.
    by_id = {item.artifact_id: item for item in materials}
    by_id.update({item.artifact_id: item for item in registered})
    authorize_attachments(list(by_id.values()), requested_use=requested_use)
