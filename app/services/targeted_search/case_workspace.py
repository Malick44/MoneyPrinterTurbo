"""Local case assets, typed evidence, reviewed assertions and production records.

Original bytes and record revisions are retained. Relevance, transcription,
identity suggestions and document assertions never confer factual verification
or permission to publish.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from pydantic import ValidationError

from app.models.case_workspace import Citation, LOCATOR_ADAPTER, StoryboardRecord
from app.models.search import SearchError
from .policy import authorize, current_policy, is_expired
from .repository import decode, json_text, new_id, now


KINDS = {
    ".pdf": "document",
    ".wav": "audio",
    ".mp3": "audio",
    ".m4a": "audio",
    ".flac": "audio",
    ".aac": "audio",
    ".ogg": "audio",
    ".mp4": "video",
    ".mov": "video",
    ".mkv": "video",
    ".webm": "video",
    ".avi": "video",
    ".jpg": "image",
    ".jpeg": "image",
    ".png": "image",
    ".webp": "image",
    ".tif": "image",
    ".tiff": "image",
    ".txt": "script",
    ".md": "script",
    ".srt": "transcript",
    ".vtt": "transcript",
    ".json": "transcript",
    ".geojson": "map",
}
ASSET_KINDS = frozenset((*KINDS.values(), "map", "other", "reference"))
PRODUCTION_ROLES = {"production", "narration", "script", "production_transcript"}
OUTPUT_FOLDERS = {"research", "derived", "exports", "case_originals"}
OUTPUT_FILENAMES = {"case.json", "case_manifest.json"}


def digest(value) -> str:
    return hashlib.sha256(json_text(value).encode()).hexdigest()


def _safe_json(value):
    """Public/export records contain logical paths, never host storage paths."""
    if isinstance(value, dict):
        return {
            key: _safe_json(item)
            for key, item in value.items()
            if key
            not in {
                "path",
                "local_path",
                "import_root",
                "cookiefile",
                "cookies",
                "http_headers",
            }
            and not key.endswith("_json")
        }
    if isinstance(value, list):
        return [_safe_json(item) for item in value]
    return value


class CaseWorkspace:
    def __init__(self, search_service):
        self.search_service = search_service
        self.repo = search_service.repo
        self.settings = search_service.settings

    def _case(self, case_id):
        record = self.repo.get("cases", case_id)
        if not record:
            raise SearchError("Case not found.", 404)
        return record

    def create_case(self, name, topic="", metadata=None):
        if not str(name).strip() or len(str(name)) > 500:
            raise SearchError("A case needs a name of at most 500 characters.")
        collection = self.search_service.add_collection(
            str(name).strip(), str(topic), []
        )
        identifier, timestamp = new_id("case_"), now()
        with self.repo.connect() as connection:
            connection.execute(
                "INSERT INTO cases VALUES(?,?,?,?,?,?,?)",
                (
                    identifier,
                    collection["id"],
                    str(name).strip(),
                    str(topic),
                    json_text(metadata or {}),
                    timestamp,
                    timestamp,
                ),
            )
        self.repo.event(
            "case_created",
            payload={"case_id": identifier, "collection_id": collection["id"]},
        )
        return self.get_case(identifier)

    def get_case(self, case_id):
        record = self._case(case_id)
        assets = self.list_assets(case_id)
        return {
            **_safe_json(record),
            "asset_count": len(assets),
            "source_ids": sorted(
                {
                    asset["source_id"]
                    for asset in assets
                    if asset["asset_kind"] == "video" and not self._is_production(asset)
                }
            ),
            "counts": {
                kind: sum(asset["asset_kind"] == kind for asset in assets)
                for kind in ASSET_KINDS
            },
            "asset_ids": [asset["id"] for asset in assets],
        }

    def list_cases(self):
        with self.repo.connect() as connection:
            identifiers = [
                row[0]
                for row in connection.execute(
                    "SELECT id FROM cases ORDER BY updated_at DESC"
                )
            ]
        return [self.get_case(identifier) for identifier in identifiers]

    def get_asset(self, asset_id):
        asset = self.repo.get("case_assets", asset_id)
        if not asset:
            raise SearchError("Case asset not found.", 404)
        with self.repo.connect() as connection:
            version = connection.execute(
                "SELECT id FROM case_asset_versions WHERE asset_id=? AND version=?",
                (asset_id, asset["version"]),
            ).fetchone()
            unit_count = connection.execute(
                "SELECT count(*) FROM evidence_units WHERE asset_id=? AND is_active=1",
                (asset_id,),
            ).fetchone()[0]
        policy = current_policy(self.repo, asset["source_id"])
        return {
            **_safe_json(asset),
            "asset_version_id": version[0] if version else None,
            "unit_count": unit_count,
            "rights_status": "expired"
            if policy and is_expired(policy.get("expires_at"))
            else policy["rights_status"]
            if policy
            else "unknown",
            "policy": _safe_json(policy),
            "permitted_use": policy["permitted_use"] if policy else "",
        }

    def list_assets(self, case_id):
        self._case(case_id)
        with self.repo.connect() as connection:
            identifiers = [
                row[0]
                for row in connection.execute(
                    "SELECT id FROM case_assets WHERE case_id=? ORDER BY category,relative_path",
                    (case_id,),
                )
            ]
        return [self.get_asset(identifier) for identifier in identifiers]

    def allowed_import_roots(self):
        from app.config import config

        configured = config.app.get("case_workspace_import_roots", [])
        if isinstance(configured, str):
            configured = [configured]
        return [
            (self.repo.root / "owned").resolve(),
            *[
                Path(value).expanduser().resolve()
                for value in configured
                if isinstance(value, str) and value
            ],
        ]

    def _import_manifest(self, path, max_files, max_total_bytes):
        supplied = Path(path).expanduser()
        if supplied.is_symlink():
            raise SearchError("Case import folders must not be symlinks.")
        root = supplied.resolve()
        if not root.is_dir() or not any(
            root.is_relative_to(allowed) for allowed in self.allowed_import_roots()
        ):
            raise SearchError(
                "Select a case folder within owned storage or a configured case_workspace_import_roots directory.",
                403,
            )
        files, skipped, total, scanned = [], [], 0, 0
        limit = min(max_total_bytes, self.settings.max_storage_bytes)
        for directory, children, filenames in os.walk(root, followlinks=False):
            scanned += len(children) + len(filenames)
            if scanned > min(100000, max(1000, max_files * 20)):
                raise SearchError("Case folder exceeds the directory traversal budget.")
            excluded = [
                child
                for child in children
                if re.sub(r"^\d+[_ -]*", "", child.casefold()) in OUTPUT_FOLDERS
            ]
            skipped.extend(
                (Path(directory) / child).relative_to(root).as_posix() + "/"
                for child in excluded
            )
            children[:] = [
                child
                for child in children
                if child not in excluded and not child.startswith(".")
            ]
            if any((Path(directory) / child).is_symlink() for child in children):
                raise SearchError("Case import rejects directory symlinks.")
            children[:] = sorted(
                child for child in children if not child.startswith(".")
            )
            for filename in sorted(filenames):
                file = Path(directory) / filename
                relative = file.relative_to(root).as_posix()
                if filename.casefold() in OUTPUT_FILENAMES:
                    skipped.append(relative)
                    continue
                if file.is_symlink():
                    raise SearchError("Case import rejects file symlinks.")
                if filename.startswith(".") or file.suffix.lower() not in KINDS:
                    skipped.append(relative)
                    continue
                stat = file.stat()
                if (
                    not file.is_file()
                    or stat.st_size > self.settings.max_download_bytes
                ):
                    raise SearchError(
                        "Case asset exceeds the configured per-file size limit."
                    )
                total += stat.st_size
                files.append((relative, stat.st_size))
                if len(files) > max_files or total > limit:
                    raise SearchError(
                        "Case folder exceeds the configured file count or storage limit."
                    )
        return root, files, skipped

    def _copy_original(self, root, relative, expected_size):
        """Open each path component without following links, then hash staged bytes."""
        staging = self.repo.root / "staging"
        staging.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        staged = None
        try:
            parts = Path(relative).parts
            for part in parts[:-1]:
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = child
            source_fd = os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            with (
                os.fdopen(source_fd, "rb") as original,
                tempfile.NamedTemporaryFile(dir=staging, delete=False) as output,
            ):
                staged = Path(output.name)
                baseline = os.fstat(original.fileno())
                if baseline.st_size != expected_size:
                    raise SearchError(
                        "Case asset changed during import; retry the import.", 409
                    )
                sha, count = hashlib.sha256(), 0
                while block := original.read(1024 * 1024):
                    count += len(block)
                    if count > min(expected_size, self.settings.max_download_bytes):
                        raise SearchError(
                            "Case asset changed or exceeded the import limit.", 409
                        )
                    sha.update(block)
                    output.write(block)
                after = os.fstat(original.fileno())
                if (
                    count != expected_size
                    or after.st_mtime_ns != baseline.st_mtime_ns
                    or after.st_size != baseline.st_size
                ):
                    raise SearchError(
                        "Case asset changed during import; retry the import.", 409
                    )
                output.flush()
                os.fsync(output.fileno())
            checksum = sha.hexdigest()
            suffix = Path(relative).suffix.lower()
            destination = (
                self.repo.root / "artifacts" / checksum[:2] / (checksum + suffix)
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if self._file_hash(destination) != checksum:
                    raise SearchError(
                        "Stored original failed digest verification.", 409
                    )
            else:
                os.replace(staged, destination)
            return destination, checksum, count
        finally:
            os.close(descriptor)
            if staged:
                staged.unlink(missing_ok=True)

    @staticmethod
    def _file_hash(path):
        sha = hashlib.sha256()
        with Path(path).open("rb") as stream:
            while block := stream.read(1024 * 1024):
                sha.update(block)
        return sha.hexdigest()

    def import_folder(
        self,
        case_id,
        path,
        category=None,
        index=False,
        max_files=500,
        max_total_bytes=20000000000,
        rights_review=None,
    ):
        case = self._case(case_id)
        if (
            isinstance(max_files, bool)
            or not isinstance(max_files, int)
            or not 1 <= max_files <= 10000
            or isinstance(max_total_bytes, bool)
            or not isinstance(max_total_bytes, int)
            or max_total_bytes <= 0
        ):
            raise SearchError("Import limits must be positive bounded integers.")
        root, files, skipped = self._import_manifest(path, max_files, max_total_bytes)
        result = {
            "case_id": case_id,
            "imported": 0,
            "updated": 0,
            "unchanged": 0,
            "skipped": skipped,
            "assets": [],
            "jobs": [],
            "index_errors": [],
        }
        for relative, expected_size in files:
            original, checksum, count = self._copy_original(
                root, relative, expected_size
            )
            with self.repo.connect() as connection:
                existing = decode(
                    connection.execute(
                        "SELECT * FROM case_assets WHERE case_id=? AND relative_path=?",
                        (case_id, relative),
                    ).fetchone()
                )
            if existing and existing["sha256"] == checksum:
                asset = self.get_asset(existing["id"])
                result["unchanged"] += 1
            else:
                kind = KINDS[Path(relative).suffix.lower()]
                if Path(relative).suffix.lower() == ".json" and count <= 20000000:
                    try:
                        document = json.loads(original.read_text(encoding="utf-8"))
                        if isinstance(document, dict) and document.get("type") in {
                            "FeatureCollection",
                            "Feature",
                            "Point",
                            "LineString",
                            "Polygon",
                            "MultiPoint",
                            "MultiLineString",
                            "MultiPolygon",
                            "GeometryCollection",
                        }:
                            kind = "map"
                    except (ValueError, UnicodeError):
                        pass
                logical_category = category or (
                    Path(relative).parts[0]
                    if len(Path(relative).parts) > 1
                    else kind.title()
                )
                if kind == "image" and logical_category.casefold() in {"map", "maps"}:
                    kind = "map"
                role = (
                    "production"
                    if any(
                        re.search(r"(^|[_\W])production($|[_\W])", part, re.IGNORECASE)
                        for part in [*Path(relative).parts[:-1], str(logical_category)]
                    )
                    else "source_evidence"
                )
                metadata = {
                    "asset_kind": kind,
                    "filename": Path(relative).name,
                    "role": role,
                    "input_sha256": checksum,
                    "original_bytes": count,
                }
                owned_path = self.repo.root / "owned" / "case_originals" / original.name
                owned_path.parent.mkdir(parents=True, exist_ok=True)
                if not owned_path.exists():
                    try:
                        os.link(original, owned_path)
                    except FileExistsError:
                        pass
                if self._file_hash(owned_path) != checksum:
                    raise SearchError(
                        "Owned original mapping failed digest verification.", 409
                    )
                source = self.search_service.discover(
                    "local://case-" + digest([case_id, relative])[:40],
                    collection_id=case["collection_id"]
                    if kind == "video" and role != "production"
                    else None,
                    metadata={
                        "title": Path(relative).name,
                        "asset_kind": kind,
                        "local_path": str(owned_path),
                        "case_asset_sha256": checksum,
                    },
                )
                artifact = self.repo.insert_artifact(
                    id="case_original_" + digest([source["id"], checksum])[:40],
                    source_id=source["id"],
                    kind="case_original",
                    profile=kind + "-original",
                    path=original,
                    sha256=checksum,
                    bytes=count,
                    metadata=metadata,
                )
                identifier = existing["id"] if existing else new_id("asset_")
                version = existing["version"] + 1 if existing else 1
                timestamp = now()
                with self.repo.connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "INSERT INTO case_assets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET source_id=excluded.source_id,artifact_id=excluded.artifact_id,asset_kind=excluded.asset_kind,category=excluded.category,filename=excluded.filename,sha256=excluded.sha256,state=excluded.state,metadata_json=excluded.metadata_json,version=excluded.version,updated_at=excluded.updated_at",
                        (
                            identifier,
                            case_id,
                            source["id"],
                            artifact["id"],
                            kind,
                            str(logical_category),
                            relative,
                            Path(relative).name,
                            checksum,
                            "imported",
                            json_text(metadata),
                            version,
                            existing["created_at"] if existing else timestamp,
                            timestamp,
                        ),
                    )
                    connection.execute(
                        "INSERT INTO case_asset_versions VALUES(?,?,?,?,?,?,?)",
                        (
                            new_id("assetver_"),
                            identifier,
                            artifact["id"],
                            checksum,
                            version,
                            json_text(metadata),
                            timestamp,
                        ),
                    )
                    connection.execute(
                        "UPDATE evidence_units SET is_active=0 WHERE asset_id=?",
                        (identifier,),
                    )
                    connection.execute(
                        "UPDATE cases SET updated_at=? WHERE id=?", (timestamp, case_id)
                    )
                if existing and current_policy(self.repo, source["id"]):
                    self.search_service.set_policy(
                        source["id"],
                        "review_required",
                        "internal_review",
                        "Original asset replaced; review permitted uses for the new version.",
                        "case-importer",
                    )
                result["updated" if existing else "imported"] += 1
                asset = self.get_asset(identifier)
                self.repo.event(
                    "case_asset_imported",
                    source_id=source["id"],
                    artifact_id=artifact["id"],
                    payload={
                        "case_id": case_id,
                        "asset_id": identifier,
                        "version": version,
                        "sha256": checksum,
                    },
                )
            if rights_review:
                self.search_service.set_policy(asset["source_id"], **rights_review)
                asset = self.get_asset(asset["id"])
            if index:
                try:
                    result["jobs"].append(self.enqueue_index(asset["id"]))
                except SearchError as exc:
                    result["index_errors"].append(
                        {"asset_id": asset["id"], "error": str(exc)}
                    )
            result["assets"].append(asset)
        return result

    def link_source(self, case_id, source_id, category="Video", asset_kind="video"):
        case = self._case(case_id)
        if asset_kind not in ASSET_KINDS:
            raise SearchError("Unknown asset kind.")
        source = self.search_service.get_source(source_id)
        with self.repo.connect() as connection:
            originals = connection.execute(
                "SELECT id FROM artifacts WHERE source_id=? AND kind IN ('source','case_original') ORDER BY created_at DESC",
                (source_id,),
            ).fetchall()
            existing = decode(
                connection.execute(
                    "SELECT * FROM case_assets WHERE case_id=? AND relative_path=?",
                    (case_id, "linked/" + source_id),
                ).fetchone()
            )
        if existing:
            return self.get_asset(existing["id"])
        artifact = self.repo.get("artifacts", originals[0]["id"]) if originals else None
        production = (
            bool(re.search(r"(^|[_\W])production($|[_\W])", category, re.IGNORECASE))
            or source.get("metadata", {}).get("role") in PRODUCTION_ROLES
            or (
                artifact is not None
                and artifact.get("metadata", {}).get("role") in PRODUCTION_ROLES
            )
        )
        primary_video = asset_kind == "video" and not production
        identifier, timestamp = new_id("asset_"), now()
        with self.repo.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if primary_video:
                count = connection.execute(
                    "SELECT count(*) FROM collection_sources WHERE collection_id=?",
                    (case["collection_id"],),
                ).fetchone()[0]
                member = connection.execute(
                    "SELECT 1 FROM collection_sources WHERE collection_id=? AND source_id=?",
                    (case["collection_id"], source_id),
                ).fetchone()
                if count >= self.settings.max_collection_sources and not member:
                    raise SearchError(
                        "This footage collection has reached its configured source limit."
                    )
            connection.execute(
                "INSERT INTO case_assets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    case_id,
                    source_id,
                    artifact["id"] if artifact else None,
                    asset_kind,
                    category,
                    "linked/" + source_id,
                    source["title"] or source_id,
                    artifact["sha256"] if artifact else None,
                    "linked",
                    json_text(
                        {
                            "role": "production" if production else "source_evidence",
                            "linked": True,
                        }
                    ),
                    1,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                "INSERT INTO case_asset_versions VALUES(?,?,?,?,?,?,?)",
                (
                    new_id("assetver_"),
                    identifier,
                    artifact["id"] if artifact else None,
                    artifact["sha256"] if artifact else None,
                    1,
                    "{}",
                    timestamp,
                ),
            )
            if primary_video:
                connection.execute(
                    "INSERT OR IGNORE INTO collection_sources VALUES(?,?)",
                    (case["collection_id"], source_id),
                )
        return self.get_asset(identifier)

    def authorize_asset(self, asset_id, requested_use="analysis"):
        asset = self.get_asset(asset_id)
        policy = authorize(self.repo, asset["source_id"], requested_use)
        if asset["artifact_id"]:
            self.asset_path(asset_id)
        return policy

    def refresh_linked_asset(self, asset_id):
        from .media import verified_artifact_path

        asset = self.get_asset(asset_id)
        if not asset.get("metadata", {}).get("linked"):
            return asset
        with self.repo.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM artifacts WHERE source_id=? AND kind='source' ORDER BY created_at DESC",
                (asset["source_id"],),
            ).fetchall()
        if not rows:
            return asset
        artifact = self.repo.get("artifacts", rows[0]["id"])
        verified_artifact_path(self.repo, artifact)
        if asset["artifact_id"] == artifact["id"]:
            return asset
        timestamp = now()
        metadata = {
            **asset.get("metadata", {}),
            "availability": "acquired",
            "duration_ms": artifact.get("metadata", {}).get("duration_ms"),
        }
        version = asset["version"] + 1
        with self.repo.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE case_assets SET artifact_id=?,sha256=?,version=?,state='acquired',metadata_json=?,updated_at=? WHERE id=?",
                (
                    artifact["id"],
                    artifact["sha256"],
                    version,
                    json_text(metadata),
                    timestamp,
                    asset_id,
                ),
            )
            connection.execute(
                "INSERT INTO case_asset_versions VALUES(?,?,?,?,?,?,?)",
                (
                    new_id("assetver_"),
                    asset_id,
                    artifact["id"],
                    artifact["sha256"],
                    version,
                    json_text(metadata),
                    timestamp,
                ),
            )
            connection.execute(
                "UPDATE evidence_units SET is_active=0 WHERE asset_id=?", (asset_id,)
            )
        if (
            asset["sha256"]
            and asset["sha256"] != artifact["sha256"]
            and current_policy(self.repo, asset["source_id"])
        ):
            self.search_service.set_policy(
                asset["source_id"],
                "review_required",
                "internal_review",
                "Linked original content changed; review permitted uses again.",
                "case-sync",
            )
        self.repo.event(
            "case_linked_asset_refreshed",
            source_id=asset["source_id"],
            artifact_id=artifact["id"],
            payload={
                "case_id": asset["case_id"],
                "asset_id": asset_id,
                "version": version,
            },
        )
        return self.get_asset(asset_id)

    def asset_path(self, asset_id):
        from .media import verified_artifact_path

        asset = self.get_asset(asset_id)
        if not asset["artifact_id"]:
            raise SearchError("This linked source has no retained original asset.", 404)
        artifact = self.repo.get("artifacts", asset["artifact_id"])
        if (
            not artifact
            or artifact["source_id"] != asset["source_id"]
            or artifact["sha256"] != asset["sha256"]
        ):
            raise SearchError(
                "Case original identity no longer matches its asset version.", 409
            )
        return verified_artifact_path(self.repo, artifact)

    def set_asset_state(self, asset_id, state, metadata=None):
        asset = self.get_asset(asset_id)
        combined = {**asset.get("metadata", {}), **(metadata or {})}
        with self.repo.connect() as connection:
            connection.execute(
                "UPDATE case_assets SET state=?,metadata_json=?,updated_at=? WHERE id=?",
                (str(state), json_text(combined), now(), asset_id),
            )
        return self.get_asset(asset_id)

    def _assert_asset_snapshot(self, connection, asset, metadata=None, artifact=None):
        current = connection.execute(
            "SELECT version,sha256 FROM case_assets WHERE id=?", (asset["id"],)
        ).fetchone()
        if (
            not current
            or current["version"] != asset["version"]
            or current["sha256"] != asset["sha256"]
        ):
            raise SearchError("Asset version changed before evidence persistence.", 409)
        for snapshot in (
            metadata or {},
            artifact.get("metadata", {}) if artifact else {},
        ):
            if (
                snapshot.get("asset_version_id")
                and snapshot["asset_version_id"] != asset["asset_version_id"]
            ):
                raise SearchError(
                    "Evidence extraction belongs to an obsolete asset version.", 409
                )
            expected_hash = snapshot.get("input_sha256") or snapshot.get("audio_sha256")
            if expected_hash and expected_hash != asset["sha256"]:
                raise SearchError(
                    "Evidence extraction source digest was superseded.", 409
                )

    def _locator(self, asset, locator_type, locator):
        try:
            record = LOCATOR_ADAPTER.validate_python(
                {**locator, "kind": locator_type}
            ).model_dump(exclude_none=True)
        except (ValidationError, TypeError) as exc:
            raise SearchError("Invalid typed evidence locator.") from exc
        allowed = {
            "document": {"page", "metadata"},
            "audio": {"time", "word", "metadata"},
            "video": {"time", "word", "image", "metadata"},
            "image": {"image", "metadata"},
            "map": {"image", "metadata"},
            "script": {"script", "metadata"},
            "transcript": {"script", "time", "word", "metadata"},
            "other": {"metadata"},
            "reference": {"metadata"},
        }
        if locator_type not in allowed[asset["asset_kind"]]:
            raise SearchError("This locator does not match the asset kind.")
        if record.get("end_ms") is not None:
            duration = asset.get("metadata", {}).get("duration_ms") or self.repo.get(
                "sources", asset["source_id"]
            ).get("duration_ms")
            if duration and record["end_ms"] > duration:
                raise SearchError(
                    "Evidence timing exceeds the measured source duration."
                )
        if locator_type == "word":
            artifact = self.repo.get("artifacts", record["transcript_artifact_id"])
            if not artifact or artifact["source_id"] != asset["source_id"]:
                raise SearchError(
                    "Word locator must reference this asset's transcript artifact."
                )
        return record

    def add_evidence_unit(
        self,
        asset_id,
        text,
        locator_type,
        locator,
        unit_kind,
        origin,
        confidence=None,
        artifact_id=None,
        metadata=None,
    ):
        asset = self.get_asset(asset_id)
        record = self._locator(asset, locator_type, locator)
        if confidence is not None and (
            isinstance(confidence, bool)
            or not isinstance(confidence, (float, int))
            or not 0 <= confidence <= 1
        ):
            raise SearchError("Evidence confidence must be within [0,1].")
        artifact = None
        if artifact_id:
            artifact = self.repo.get("artifacts", artifact_id)
            if not artifact or artifact["source_id"] != asset["source_id"]:
                raise SearchError("Evidence artifact must belong to its source.")
        if not str(text).strip() or len(str(text)) > 2000000:
            raise SearchError("Evidence text must be present and bounded.")
        content_hash = digest(
            [asset["asset_version_id"], str(text), record, unit_kind, origin]
        )
        identifier = "unit_" + content_hash[:40]
        with self.repo.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_asset_snapshot(connection, asset, metadata, artifact)
            connection.execute(
                "INSERT OR IGNORE INTO evidence_units VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    asset["case_id"],
                    asset_id,
                    asset["asset_version_id"],
                    asset["source_id"],
                    artifact_id or asset["artifact_id"],
                    str(unit_kind),
                    str(text),
                    locator_type,
                    json_text(record),
                    content_hash,
                    str(origin),
                    confidence,
                    json_text(metadata or {}),
                    1,
                    now(),
                ),
            )
        return _safe_json(self.repo.get("evidence_units", identifier))

    def save_evidence_units(self, asset_id, units):
        return [
            self.add_evidence_unit(
                asset_id,
                text=item.get("text", item.get("evidence", "")),
                locator_type=item.get(
                    "locator_type", item.get("locator", {}).get("kind")
                ),
                locator=item["locator"],
                unit_kind=item.get("unit_kind", "passage"),
                origin=item.get("origin", "provided"),
                confidence=item.get("confidence"),
                artifact_id=item.get("artifact_id"),
                metadata=item.get("metadata"),
            )
            for item in units
        ]

    def record_document_page(
        self,
        asset_id,
        page_index,
        page_label,
        text,
        origin,
        confidence=None,
        render_artifact_id=None,
        metadata=None,
    ):
        asset = self.get_asset(asset_id)
        locator = self._locator(
            asset, "page", {"page_index": page_index, "page_label": page_label}
        )
        artifact = None
        if render_artifact_id:
            artifact = self.repo.get("artifacts", render_artifact_id)
            if not artifact or artifact["source_id"] != asset["source_id"]:
                raise SearchError("Rendered page does not belong to this source.")
        identifier = "page_" + digest([asset["asset_version_id"], page_index])[:40]
        with self.repo.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_asset_snapshot(connection, asset, metadata, artifact)
            connection.execute(
                "INSERT INTO document_pages VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET text=excluded.text,origin=excluded.origin,confidence=excluded.confidence,render_artifact_id=excluded.render_artifact_id,metadata_json=excluded.metadata_json",
                (
                    identifier,
                    asset_id,
                    asset["asset_version_id"],
                    page_index,
                    page_label,
                    str(text),
                    origin,
                    confidence,
                    render_artifact_id,
                    json_text(metadata or {}),
                    now(),
                ),
            )
        if str(text).strip():
            self.add_evidence_unit(
                asset_id,
                text,
                "page",
                locator,
                "document_page",
                origin,
                confidence,
                render_artifact_id,
                metadata,
            )
        return _safe_json(self.repo.get("document_pages", identifier))

    def record_transcript_words(self, asset_id, transcript_artifact_id, words):
        asset = self.get_asset(asset_id)
        artifact = self.repo.get("artifacts", transcript_artifact_id)
        if not artifact or artifact["source_id"] != asset["source_id"]:
            raise SearchError("Transcript artifact must belong to this source.")
        records = []
        for index, word in enumerate(words):
            start, end = word.get("start_ms"), word.get("end_ms")
            if (start is None) != (end is None) or (
                start is not None
                and (
                    isinstance(start, bool)
                    or isinstance(end, bool)
                    or not isinstance(start, int)
                    or not isinstance(end, int)
                    or start < 0
                    or end < start
                )
            ):
                raise SearchError(
                    "Word times must be known finite integer ranges or both null."
                )
            for field in ("confidence", "alignment_confidence"):
                value = word.get(field)
                if value is not None and (
                    isinstance(value, bool)
                    or not isinstance(value, (float, int))
                    or not 0 <= value <= 1
                ):
                    raise SearchError("Word confidence must be within [0,1].")
            text = str(word.get("text", word.get("word", "")))
            word_index = word.get("word_index", index)
            if (
                isinstance(word_index, bool)
                or not isinstance(word_index, int)
                or word_index < 0
            ):
                raise SearchError("Word indices must be nonnegative integers.")
            identifier = (
                "word_"
                + digest(
                    [asset["asset_version_id"], transcript_artifact_id, word_index]
                )[:40]
            )
            records.append(
                (
                    identifier,
                    asset_id,
                    asset["asset_version_id"],
                    transcript_artifact_id,
                    word_index,
                    text,
                    start,
                    end,
                    word.get("speaker"),
                    str(word["channel"]) if word.get("channel") is not None else None,
                    word.get("confidence"),
                    word.get("alignment_confidence"),
                    json_text(word.get("metadata", {})),
                    now(),
                )
            )
        with self.repo.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_asset_snapshot(connection, asset, artifact=artifact)
            connection.executemany(
                "INSERT OR IGNORE INTO transcript_words VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                records,
            )
        return {
            "asset_id": asset_id,
            "transcript_artifact_id": transcript_artifact_id,
            "words": len(records),
            "unaligned_words": sum(record[6] is None for record in records),
        }

    def store_transcript(self, asset_id, record):
        asset = self.get_asset(asset_id)
        transcript_id = record.get("transcript_artifact_id")
        artifact = None
        if transcript_id:
            artifact = self.repo.get("artifacts", transcript_id)
            if not artifact or artifact["source_id"] != asset["source_id"]:
                raise SearchError("Transcript artifact must belong to its source.")
        identifier = "transcript_" + digest([asset["asset_version_id"], record])[:40]
        with self.repo.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_asset_snapshot(connection, asset, record, artifact)
            connection.execute(
                "INSERT OR IGNORE INTO case_transcripts VALUES(?,?,?,?,?,?,?)",
                (
                    identifier,
                    asset_id,
                    asset["asset_version_id"],
                    transcript_id,
                    record.get("scope", "source"),
                    json_text(record),
                    now(),
                ),
            )
        return _safe_json(self.repo.get("case_transcripts", identifier))

    def add_derivative(self, asset_id, artifact_id, kind, metadata=None):
        asset = self.get_asset(asset_id)
        artifact = self.repo.get("artifacts", artifact_id)
        if not artifact or artifact["source_id"] != asset["source_id"]:
            raise SearchError("Derivative artifact must belong to the source.")
        identifier = (
            "derivative_" + digest([asset["asset_version_id"], artifact_id, kind])[:40]
        )
        with self.repo.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_asset_snapshot(connection, asset, metadata, artifact)
            connection.execute(
                "INSERT OR IGNORE INTO case_derivatives VALUES(?,?,?,?,?,?,?)",
                (
                    identifier,
                    asset_id,
                    asset["asset_version_id"],
                    artifact_id,
                    kind,
                    json_text(metadata or {}),
                    now(),
                ),
            )
        return _safe_json(self.repo.get("case_derivatives", identifier))

    def enqueue_index(self, asset_id):
        asset = self.get_asset(asset_id)
        self.authorize_asset(asset_id, "analysis")
        return self.repo.enqueue(
            "case_index",
            {
                "asset_id": asset_id,
                "case_id": asset["case_id"],
                "asset_version_id": asset["asset_version_id"],
                "input_sha256": asset["sha256"],
            },
            "case-index:" + digest([asset_id, asset["asset_version_id"]]),
        )

    def enqueue_alignment(self, asset_id, script_asset_id=None, scope="source"):
        asset = self.get_asset(asset_id)
        self.authorize_asset(asset_id, "analysis")
        if scope not in {"source", "narration"}:
            raise SearchError("Alignment scope must be source or narration.")
        script = self.get_asset(script_asset_id) if script_asset_id else None
        if script and script["case_id"] != asset["case_id"]:
            raise SearchError("Alignment script must belong to the same case.")
        payload = {
            "asset_id": asset_id,
            "case_id": asset["case_id"],
            "asset_version_id": asset["asset_version_id"],
            "input_sha256": asset["sha256"],
            "script_asset_id": script_asset_id,
            "scope": scope,
        }
        if script:
            self.authorize_asset(script["id"], "analysis")
            payload["script_asset_version_id"] = script["asset_version_id"]
            payload["input_script_sha256"] = script["sha256"]
        return self.repo.enqueue(
            "case_align",
            payload,
            "case-align:" + digest([payload, script["sha256"] if script else None]),
        )

    def _citation(self, case_id, raw):
        saved_version = raw.get("asset_version_id") if isinstance(raw, dict) else None
        if isinstance(raw, dict):
            raw = {
                key: value
                for key, value in raw.items()
                if key in {"unit_id", "asset_id", "locator", "quote", "relation"}
                and value is not None
            }
        try:
            citation = Citation.model_validate(
                {"unit_id": raw} if isinstance(raw, str) else raw
            ).model_dump(exclude_none=True)
        except ValidationError as exc:
            raise SearchError("Invalid evidence citation.") from exc
        unit = (
            self.repo.get("evidence_units", citation["unit_id"])
            if citation.get("unit_id")
            else None
        )
        if citation.get("unit_id") and (
            not unit or unit["case_id"] != case_id or not unit["is_active"]
        ):
            raise SearchError("Citation must refer to current evidence in this case.")
        asset = self.get_asset(unit["asset_id"] if unit else citation["asset_id"])
        if saved_version and saved_version != asset["asset_version_id"]:
            raise SearchError(
                "Citation refers to an obsolete asset version; review the new original."
            )
        if asset["case_id"] != case_id or (
            unit and unit["asset_version_id"] != asset["asset_version_id"]
        ):
            raise SearchError(
                "Citation asset belongs to another case or an obsolete version."
            )
        if (
            unit
            and citation.get("asset_id")
            and citation["asset_id"] != unit["asset_id"]
        ):
            raise SearchError("Citation asset and evidence unit do not match.")
        locator = (
            unit["locator"]
            if unit
            else self._locator(asset, citation["locator"]["kind"], citation["locator"])
        )
        if unit and citation.get("locator") and citation["locator"] != locator:
            raise SearchError("Citation locator differs from its evidence unit.")
        quote = citation.get("quote")
        if quote and (not unit or quote not in unit["text"]):
            raise SearchError(
                "Quoted citations must match an indexed evidence passage exactly."
            )
        return {
            "asset_id": asset["id"],
            "asset_version_id": asset["asset_version_id"],
            "unit_id": unit["id"] if unit else None,
            "locator": locator,
            "quote": quote,
            "relation": citation["relation"],
            "evidence_hash": unit["content_hash"] if unit else asset["sha256"],
        }

    def _save_record(self, table, kind, case_id, record, columns):
        self._case(case_id)
        record = dict(record)
        if len(json_text(record)) > 2000000:
            raise SearchError("Case record exceeds the configured text budget.")
        for field, target in (
            ("entity_ids", "case_entities"),
            ("event_ids", "case_events"),
            ("claim_ids", "case_claims"),
        ):
            for related_id in record.get(field, []):
                related = self.repo.get(target, related_id)
                if not related or related["case_id"] != case_id:
                    raise SearchError(
                        "Case relationships must refer to records in this case."
                    )
        identifier = record.get("id") or new_id(kind + "_")
        existing = self.repo.get(table, identifier)
        if existing and existing["case_id"] != case_id:
            raise SearchError("Record belongs to another case.", 403)
        citations = [
            self._citation(case_id, citation)
            for citation in record.get("citations", [])
        ]
        record["id"], record["citations"] = identifier, citations
        timestamp = now()
        with self.repo.connect() as connection:
            names = [
                "id",
                "case_id",
                *columns,
                "record_json",
                "created_at",
                "updated_at",
            ]
            values = [
                identifier,
                case_id,
                *[record.get(column) for column in columns],
                json_text(record),
                existing["created_at"] if existing else timestamp,
                timestamp,
            ]
            updates = ",".join(
                f"{column}=excluded.{column}"
                for column in [*columns, "record_json", "updated_at"]
            )
            connection.execute(
                f"INSERT INTO {table}({','.join(names)}) VALUES({','.join('?' for _ in names)}) ON CONFLICT(id) DO UPDATE SET {updates}",
                values,
            )
            connection.execute(
                "DELETE FROM case_citations WHERE record_type=? AND record_id=?",
                (kind, identifier),
            )
            for citation in citations:
                connection.execute(
                    "INSERT INTO case_citations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        new_id("cite_"),
                        case_id,
                        kind,
                        identifier,
                        citation["asset_id"],
                        citation["asset_version_id"],
                        citation["unit_id"],
                        json_text(citation["locator"]),
                        citation["quote"],
                        citation["relation"],
                        citation["evidence_hash"],
                        timestamp,
                    ),
                )
            connection.execute(
                "INSERT INTO case_record_versions VALUES(?,?,?,?,?,?,?)",
                (
                    new_id("recordver_"),
                    case_id,
                    kind,
                    identifier,
                    json_text(record),
                    digest(record),
                    timestamp,
                ),
            )
            connection.execute(
                "UPDATE cases SET updated_at=? WHERE id=?", (timestamp, case_id)
            )
        self.repo.event(
            "case_record_saved",
            payload={
                "case_id": case_id,
                "record_type": kind,
                "record_id": identifier,
                "record_hash": digest(record),
            },
        )
        return self._public_record(self.repo.get(table, identifier))

    def _public_record(self, row):
        record = {**_safe_json(row), **_safe_json(row.get("record", {}))}
        record.pop("record", None)
        citation_status = []
        for citation in record.get("citations", []):
            asset = self.get_asset(citation["asset_id"])
            unit = (
                self.repo.get("evidence_units", citation["unit_id"])
                if citation.get("unit_id")
                else None
            )
            current = asset["asset_version_id"] == citation["asset_version_id"] and (
                not citation.get("unit_id")
                or (
                    unit
                    and unit["is_active"]
                    and unit["content_hash"] == citation["evidence_hash"]
                )
            )
            citation_status.append({**citation, "is_current": bool(current)})
        record["citations"] = citation_status
        record["has_stale_citations"] = any(
            not citation["is_current"] for citation in citation_status
        )
        return record

    def _list_records(self, table, case_id):
        self._case(case_id)
        with self.repo.connect() as connection:
            rows = [
                decode(row)
                for row in connection.execute(
                    f"SELECT * FROM {table} WHERE case_id=? ORDER BY created_at",
                    (case_id,),
                )
            ]
        return [self._public_record(row) for row in rows]

    def save_request(self, case_id, record):
        record = {"status": "missing", **record}
        if (
            record.get("asset_id")
            and self.get_asset(record["asset_id"])["case_id"] != case_id
        ):
            raise SearchError("Received request assets must belong to this case.")
        if not str(record.get("title", "")).strip() or record["status"] not in {
            "missing",
            "requested",
            "received",
            "unavailable",
            "cancelled",
        }:
            raise SearchError("A request needs a title and valid request status.")
        return self._save_record(
            "case_requests", "request", case_id, record, ["title", "status"]
        )

    def list_requests(self, case_id):
        return self._list_records("case_requests", case_id)

    def save_claim(self, case_id, record):
        record = {"status": "proposed", "assertion_class": "unclassified", **record}
        if not str(record.get("text", "")).strip() or record["status"] not in {
            "proposed",
            "reviewed",
            "disputed",
            "insufficient_support",
        }:
            raise SearchError(
                "A claim needs text and a reviewed assertion status; retrieval cannot verify facts."
            )
        if record["status"] == "reviewed" and (
            not str(record.get("reviewed_by", "")).strip()
            or not record.get("citations")
        ):
            raise SearchError(
                "Reviewed claims require a reviewer and retained evidence citations."
            )
        if record.get("assertion_class") not in {
            "unclassified",
            "allegation",
            "testimony",
            "police_report",
            "court_finding",
            "news_report",
            "editorial",
            "recording_observation",
        }:
            raise SearchError("Unknown assertion class.")
        citations = [
            self._citation(case_id, citation)
            for citation in record.get("citations", [])
        ]
        if record["status"] == "reviewed" and any(
            self._is_production(self.get_asset(citation["asset_id"]))
            for citation in citations
        ):
            raise SearchError(
                "Production scripts or narration cannot substantiate a reviewed source claim."
            )
        return self._save_record(
            "case_claims", "claim", case_id, record, ["text", "status"]
        )

    def list_claims(self, case_id):
        return self._list_records("case_claims", case_id)

    def save_event(self, case_id, record):
        if not str(record.get("title", "")).strip():
            raise SearchError("A case event needs a title.")
        record = {"event_at": None, "time_precision": "unknown", **record}
        if record["time_precision"] not in {
            "unknown",
            "year",
            "month",
            "day",
            "minute",
            "second",
            "range",
        }:
            raise SearchError("Unknown event-time precision.")
        return self._save_record(
            "case_events", "case_event", case_id, record, ["title", "event_at"]
        )

    def list_events(self, case_id):
        return self._list_records("case_events", case_id)

    def save_entity(self, case_id, record):
        if not str(record.get("name", "")).strip() or record.get("entity_type") not in {
            "person",
            "organization",
            "location",
            "object",
        }:
            raise SearchError("An entity needs a name and supported type.")
        record = {"review_status": "proposed", "aliases": [], **record}
        return self._save_record(
            "case_entities", "entity", case_id, record, ["entity_type", "name"]
        )

    def list_entities(self, case_id):
        return self._list_records("case_entities", case_id)

    def save_mention(self, case_id, record):
        entity = self.repo.get("case_entities", record.get("entity_id", ""))
        unit = self.repo.get("evidence_units", record.get("unit_id", ""))
        if (
            not entity
            or not unit
            or entity["case_id"] != case_id
            or unit["case_id"] != case_id
            or not unit["is_active"]
        ):
            raise SearchError(
                "Mentions require an entity and current evidence in the same case."
            )
        start, end = record.get("char_start"), record.get("char_end")
        if (start is None) != (end is None) or (
            start is not None
            and (
                not isinstance(start, int)
                or not isinstance(end, int)
                or not 0 <= start < end <= len(unit["text"])
            )
        ):
            raise SearchError("Mention span must lie inside its evidence passage.")
        identifier = record.get("id") or new_id("mention_")
        with self.repo.connect() as connection:
            connection.execute(
                "INSERT INTO case_mentions VALUES(?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    case_id,
                    entity["id"],
                    unit["id"],
                    start,
                    end,
                    json_text(record),
                    now(),
                ),
            )
        return _safe_json(self.repo.get("case_mentions", identifier))

    def save_storyboard(self, case_id, record):
        self._case(case_id)
        record = {
            key: value
            for key, value in record.items()
            if key in StoryboardRecord.model_fields
        }
        record["scenes"] = [
            {
                key: value
                for key, value in scene.items()
                if key
                not in {
                    "asset_version_id",
                    "input_sha256",
                    "visual_asset_version_id",
                    "visual_input_sha256",
                    "visual_sha256",
                }
            }
            for scene in record.get("scenes", [])
        ]
        try:
            record = StoryboardRecord.model_validate(record).model_dump(
                exclude_none=True
            )
        except ValidationError as exc:
            raise SearchError("Invalid storyboard scene contract.") from exc
        identifier = record.get("id") or new_id("storyboard_")
        existing = self.repo.get("case_storyboards", identifier)
        if existing and existing["case_id"] != case_id:
            raise SearchError("Storyboard belongs to another case.", 403)
        scene_ids = set()
        for scene in record["scenes"]:
            if scene["scene_id"] in scene_ids:
                raise SearchError("Storyboard scene IDs must be unique.")
            scene_ids.add(scene["scene_id"])
            asset = self.get_asset(scene["asset_id"])
            if asset["case_id"] != case_id:
                raise SearchError("Storyboard assets must belong to this case.")
            allowed_roles = {
                "audio": {"original_sound"},
                "video": {"broll", "original_sound"},
                "document": {"document", "still", "map"},
                "image": {"still", "map"},
                "map": {"map", "still"},
            }
            if scene["role"] not in allowed_roles.get(asset["asset_kind"], set()):
                raise SearchError(
                    "Storyboard role does not match the source asset kind."
                )
            if scene.get("visual_asset_id"):
                if asset["asset_kind"] != "audio":
                    raise SearchError(
                        "A separate scene background is supported for audio scenes."
                    )
                visual = self.get_asset(scene["visual_asset_id"])
                if visual["case_id"] != case_id:
                    raise SearchError("Audio backgrounds must belong to the same case.")
                if visual["asset_kind"] not in {"image", "map", "document"}:
                    raise SearchError(
                        "Choose a still, map or document page as the audio background."
                    )
                if scene.get("visual_locator"):
                    self._locator(
                        visual, scene["visual_locator"]["kind"], scene["visual_locator"]
                    )
                scene["visual_asset_version_id"] = visual["asset_version_id"]
                scene["visual_input_sha256"] = visual["sha256"]
                scene["visual_sha256"] = visual["sha256"]
            if scene.get("source_start_ms") is not None:
                self._locator(
                    asset,
                    "time",
                    {
                        "start_ms": scene["source_start_ms"],
                        "end_ms": scene["source_end_ms"],
                    },
                )
            if scene.get("locator"):
                self._locator(asset, scene["locator"]["kind"], scene["locator"])
            for unit_id in scene["citations"]:
                self._citation(case_id, unit_id)
            for claim_id in scene["claim_ids"]:
                claim = self.repo.get("case_claims", claim_id)
                if not claim or claim["case_id"] != case_id:
                    raise SearchError("Storyboard claims must belong to the same case.")
            scene["asset_version_id"] = asset["asset_version_id"]
            scene["input_sha256"] = asset["sha256"]
        narration_id = record.get("narration_asset_id")
        if narration_id:
            narration = self.get_asset(narration_id)
            if narration["case_id"] != case_id or narration["asset_kind"] != "audio":
                raise SearchError(
                    "Storyboard narration must be an audio asset in this case."
                )
            record["narration_asset_version_id"] = narration["asset_version_id"]
            record["narration_sha256"] = narration["sha256"]
        metadata = record["metadata"]
        metadata.pop("script_asset_version_id", None)
        metadata.pop("script_asset_sha256", None)
        script_id = metadata.get("script_asset_id")
        if script_id:
            if not isinstance(script_id, str):
                raise SearchError("Storyboard script reference must be an asset ID.")
            script = self.get_asset(script_id)
            if script["case_id"] != case_id or script["asset_kind"] != "script":
                raise SearchError(
                    "Storyboard script must be a script asset in this case."
                )
            self.asset_path(script_id)
            metadata["script_asset_version_id"] = script["asset_version_id"]
            metadata["script_asset_sha256"] = script["sha256"]
        record["id"] = identifier
        content_hash, timestamp = digest(record), now()
        with self.repo.connect() as connection:
            connection.execute(
                "INSERT INTO case_storyboards VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,record_json=excluded.record_json,content_hash=excluded.content_hash,updated_at=excluded.updated_at",
                (
                    identifier,
                    case_id,
                    record["title"],
                    json_text(record),
                    content_hash,
                    existing["created_at"] if existing else timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                "INSERT INTO case_record_versions VALUES(?,?,?,?,?,?,?)",
                (
                    new_id("recordver_"),
                    case_id,
                    "storyboard",
                    identifier,
                    json_text(record),
                    content_hash,
                    timestamp,
                ),
            )
        return self.get_storyboard(identifier)

    def get_storyboard(self, storyboard_id):
        row = self.repo.get("case_storyboards", storyboard_id)
        if not row:
            raise SearchError("Storyboard not found.", 404)
        return {**self._public_record(row), "storyboard_hash": row["content_hash"]}

    def list_storyboards(self, case_id, include_archived=False):
        return [
            {**row, "storyboard_hash": row["content_hash"]}
            for row in self._list_records("case_storyboards", case_id)
            if include_archived or not row.get("metadata", {}).get("archived")
        ]

    def delete_storyboard(self, storyboard_id):
        """Archive the editable storyboard, retaining immutable revision history."""
        row = self.repo.get("case_storyboards", storyboard_id)
        if not row:
            raise SearchError("Storyboard not found.", 404)
        record = {
            **row["record"],
            "metadata": {**row["record"].get("metadata", {}), "archived": True},
        }
        content_hash, timestamp = digest(record), now()
        with self.repo.connect() as connection:
            connection.execute(
                "UPDATE case_storyboards SET record_json=?,content_hash=?,updated_at=? WHERE id=?",
                (json_text(record), content_hash, timestamp, storyboard_id),
            )
            connection.execute(
                "INSERT INTO case_record_versions VALUES(?,?,?,?,?,?,?)",
                (
                    new_id("recordver_"),
                    row["case_id"],
                    "storyboard",
                    storyboard_id,
                    json_text(record),
                    content_hash,
                    timestamp,
                ),
            )
        self.repo.event(
            "case_storyboard_archived",
            payload={"case_id": row["case_id"], "storyboard_id": storyboard_id},
        )
        return self.get_storyboard(storyboard_id)

    def enqueue_render(self, storyboard_id, requested_use="generated_export"):
        storyboard = self.get_storyboard(storyboard_id)
        if storyboard.get("metadata", {}).get("archived"):
            raise SearchError(
                "Archived storyboards must be restored before rendering.", 409
            )
        if not storyboard["scenes"]:
            raise SearchError("Add storyboard scenes before rendering.")
        for scene in storyboard["scenes"]:
            asset = self.get_asset(scene["asset_id"])
            if (
                asset["asset_version_id"] != scene["asset_version_id"]
                or asset["sha256"] != scene["input_sha256"]
            ):
                raise SearchError(
                    "Storyboard asset changed; review and save the storyboard again.",
                    409,
                )
            self.authorize_asset(asset["id"], requested_use)
            if scene.get("visual_asset_id"):
                visual = self.get_asset(scene["visual_asset_id"])
                if (
                    visual["asset_version_id"] != scene["visual_asset_version_id"]
                    or visual["sha256"] != scene["visual_input_sha256"]
                ):
                    raise SearchError(
                        "Storyboard background changed; review it again.", 409
                    )
                self.authorize_asset(scene["visual_asset_id"], requested_use)
            for unit_id in scene["citations"]:
                self._citation(storyboard["case_id"], unit_id)
        if storyboard.get("narration_asset_id"):
            narration = self.get_asset(storyboard["narration_asset_id"])
            if narration["sha256"] != storyboard["narration_sha256"]:
                raise SearchError("Storyboard narration changed; review it again.", 409)
            self.authorize_asset(narration["id"], requested_use)
        metadata = storyboard.get("metadata", {})
        if metadata.get("script_asset_id"):
            script = self.get_asset(metadata["script_asset_id"])
            if script["asset_version_id"] != metadata.get(
                "script_asset_version_id"
            ) or script["sha256"] != metadata.get("script_asset_sha256"):
                raise SearchError(
                    "Storyboard script changed; review and save its current version.",
                    409,
                )
            self.authorize_asset(script["id"], "internal_review")
        payload = {
            "case_id": storyboard["case_id"],
            "storyboard_id": storyboard_id,
            "storyboard_hash": storyboard["storyboard_hash"],
            "requested_use": requested_use,
        }
        return self.repo.enqueue(
            "case_render", payload, "case-render:" + digest(payload)
        )

    @staticmethod
    def _is_production(asset):
        return (
            asset["asset_kind"] == "script"
            or asset.get("metadata", {}).get("role") in PRODUCTION_ROLES
            or re.search(
                r"(^|[_\W])production($|[_\W])", asset["category"], re.IGNORECASE
            )
        )

    def search_supporting(self, case_id, query, filters=None, top_k=20):
        from .case_retrieval import retrieve_supporting

        self._case(case_id)
        if (
            not str(query).strip()
            or len(str(query)) > 2000
            or isinstance(top_k, bool)
            or not isinstance(top_k, int)
            or not 1 <= top_k <= 100
        ):
            raise SearchError(
                "Search needs a bounded query and top_k between 1 and 100."
            )
        return retrieve_supporting(self, case_id, query, filters or {}, top_k)

    def export_case(self, case_id):
        case = self.get_case(case_id)
        assets = self.list_assets(case_id)
        with self.repo.connect() as connection:
            units = [
                _safe_json(decode(row))
                for row in connection.execute(
                    "SELECT * FROM evidence_units WHERE case_id=? ORDER BY created_at",
                    (case_id,),
                )
            ]
            versions = [
                _safe_json(decode(row))
                for row in connection.execute(
                    "SELECT v.* FROM case_asset_versions v JOIN case_assets a ON a.id=v.asset_id WHERE a.case_id=? ORDER BY v.asset_id,v.version",
                    (case_id,),
                )
            ]
        manifest = {
            "schema_version": "case-workspace-1",
            "exported_at": now(),
            "case": case,
            "assets": assets,
            "asset_versions": versions,
            "evidence_units": units,
            "requests": self.list_requests(case_id),
            "claims": self.list_claims(case_id),
            "events": self.list_events(case_id),
            "entities": self.list_entities(case_id),
            "storyboards": self.list_storyboards(case_id),
            "export_kind": "inventory_and_citations",
            "media_included": False,
        }
        # The manifest may retain identity/hash/locator records after permission
        # changes, but it cannot become a route around current content policies.
        accessible = set()
        for asset in assets:
            try:
                authorize(self.repo, asset["source_id"], "internal_review")
                accessible.add(asset["id"])
            except SearchError:
                pass
        for unit in units:
            if unit["asset_id"] not in accessible:
                unit.pop("text", None)
                unit["content_withheld"] = True
        for records in (
            manifest["claims"],
            manifest["events"],
            manifest["requests"],
            manifest["entities"],
        ):
            for record in records:
                for citation in record.get("citations", []):
                    if citation["asset_id"] not in accessible:
                        citation["quote"] = None
                        citation["content_withheld"] = True
        manifest["manifest_sha256"] = digest(manifest)
        self.repo.event(
            "case_manifest_exported",
            payload={
                "case_id": case_id,
                "manifest_sha256": manifest["manifest_sha256"],
            },
        )
        return manifest
