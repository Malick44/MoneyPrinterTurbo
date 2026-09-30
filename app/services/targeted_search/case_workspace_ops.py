"""Portable folder views of the canonical case catalog; no placeholder evidence."""

from __future__ import annotations

import re

from app.services.task_artifacts import _write_json_atomic
from .repository import json_text, now


def prepare_case_folder(workspace, case_id: str) -> dict:
    case = workspace.get_case(case_id)
    name = case.get("metadata", {}).get("defendant_name") or case["name"]
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_")[:64] or "Case"
    folder = (
        workspace.repo.root
        / "owned"
        / "case_folders"
        / f"Case_Folder_{slug}_{case_id[-8:]}"
    )
    categories = [
        "00_Research",
        "01_Legal_Docs",
        "02_Audio_Raw",
        "03_Video_Footage",
        "04_Still_Assets/Crime_Scene_Photos",
        "04_Still_Assets/Satellite_Overlays",
        "05_Production/draft_01",
        "06_Derived",
        "07_Exports",
    ]
    for category in categories:
        (folder / category).mkdir(parents=True, exist_ok=True)
    relative = folder.relative_to(workspace.repo.root).as_posix()
    with workspace.repo.connect() as connection:
        connection.execute(
            "UPDATE cases SET metadata_json=?,updated_at=? WHERE id=?",
            (
                json_text(
                    {**case.get("metadata", {}), "workspace_relative_path": relative}
                ),
                now(),
                case_id,
            ),
        )
    manifest = workspace.export_case(case_id)
    _write_json_atomic(folder / "case.json", manifest)
    for filename, records in [
        ("Source_Requests.json", workspace.list_requests(case_id)),
        ("Timeline.json", workspace.list_events(case_id)),
        ("Claims.json", workspace.list_claims(case_id)),
    ]:
        _write_json_atomic(
            folder / "00_Research" / filename, {"case_id": case_id, "records": records}
        )
    return {
        "case_id": case_id,
        "workspace_relative_path": relative,
        "categories": categories,
        "media_copied": False,
        "manifest_sha256": manifest["manifest_sha256"],
    }
