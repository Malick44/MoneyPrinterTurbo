"""SQLite persistence, atomic claims, and immutable search provenance."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from app.models.search import SearchError
from .settings import Settings, storage_root


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex}"


def json_text(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def decode(row: sqlite3.Row | dict | None) -> dict | None:
    if row is None:
        return None
    result = dict(row)
    for name, value in tuple(result.items()):
        if name.endswith("_json") and value is not None:
            try:
                result[name[:-5]] = json.loads(value)
            except (TypeError, ValueError):
                result[name[:-5]] = None
    return result


TABLES = frozenset(
    {
        "sources",
        "metadata_snapshots",
        "captions",
        "caption_cues",
        "transcript_chunks",
        "embeddings",
        "search_runs",
        "candidates",
        "policies",
        "approvals",
        "jobs",
        "events",
        "artifacts",
        "clip_provenance",
        "attachments",
        "collections",
        "visual_frames",
        "ocr_blocks",
        "cases",
        "case_assets",
        "case_asset_versions",
        "evidence_units",
        "document_pages",
        "transcript_words",
        "case_transcripts",
        "case_derivatives",
        "case_requests",
        "case_claims",
        "case_events",
        "case_entities",
        "case_mentions",
        "case_citations",
        "case_storyboards",
        "case_record_versions",
    }
)
ALIASES = {
    "media_artifacts": "artifacts",
    "processing_events": "events",
    "candidate_segments": "candidates",
    "rights_and_policy": "policies",
    "source_metadata_snapshots": "metadata_snapshots",
}


class Repository:
    def __init__(self, root_dir: str | Path | None = None):
        self.root = storage_root(root_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "search.sqlite3"
        self.settings = Settings.from_config()
        self._migrate()

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = self._connection()
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _migrate(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            applied = {
                row[0]
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
            for file in sorted((Path(__file__).parent / "migrations").glob("*.sql")):
                version = int(file.stem.split("_", 1)[0])
                if version in applied:
                    continue
                statement = ""
                for line in file.read_text(encoding="utf-8").splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        connection.execute(statement)
                        statement = ""
                if statement.strip():
                    raise SearchError("Incomplete targeted-search migration.", 500)
                connection.execute(
                    "INSERT INTO schema_migrations VALUES(?,?)", (version, now())
                )

    def get(self, table: str, identifier: str) -> dict | None:
        table = ALIASES.get(table, table)
        if table not in TABLES:
            raise SearchError("Unknown search record type.")
        with self.connect() as connection:
            result = decode(
                connection.execute(
                    f"SELECT * FROM {table} WHERE id=?", (identifier,)
                ).fetchone()
            )
            if result and table == "artifacts" and Path(result["path"]).is_absolute():
                saved = Path(result["path"]).resolve()
                if saved.is_relative_to(self.root):
                    result["path"] = saved.relative_to(self.root).as_posix()
                    connection.execute(
                        "UPDATE artifacts SET path=? WHERE id=?",
                        (result["path"], identifier),
                    )
            return result

    def enqueue(self, job_type: str, payload: dict, idempotency_key: str) -> dict:
        if not isinstance(payload, dict) or not idempotency_key:
            raise SearchError("A job needs a payload and an idempotency key.")
        timestamp = now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM jobs WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if existing is not None:
                if existing["job_type"] != job_type or existing[
                    "payload_json"
                ] != json_text(payload):
                    raise SearchError(
                        "An idempotency key is already bound to a different job.", 409
                    )
                return decode(existing)
            pending = connection.execute(
                "SELECT count(*) FROM jobs WHERE status IN ('queued','retry','running')"
            ).fetchone()[0]
            if pending >= self.settings.max_pending_jobs:
                raise SearchError(
                    "The targeted-search job queue is full; retry after current jobs finish.",
                    429,
                )
            connection.execute(
                "INSERT OR IGNORE INTO jobs(id,job_type,idempotency_key,payload_json,status,max_attempts,available_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    new_id("job_"),
                    job_type,
                    idempotency_key,
                    json_text(payload),
                    "queued",
                    self.settings.max_attempts,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM jobs WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if row["job_type"] != job_type or row["payload_json"] != json_text(payload):
                raise SearchError(
                    "An idempotency key is already bound to a different job.", 409
                )
            return decode(row)

    def claim_job(self, worker_id: str) -> dict | None:
        timestamp = now()
        lease_until = (
            datetime.now(timezone.utc)
            + timedelta(seconds=self.settings.job_lease_seconds)
        ).isoformat(timespec="milliseconds")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # Abandoned work is reclaimable, but never beyond its attempt budget.
            connection.execute(
                "UPDATE jobs SET status='failed',last_error='Worker lease expired after final attempt',locked_by=NULL,locked_at=NULL,lease_until=NULL,updated_at=? WHERE status='running' AND lease_until<=? AND attempts>=max_attempts",
                (timestamp, timestamp),
            )
            row = connection.execute(
                "SELECT * FROM jobs WHERE attempts<max_attempts AND ((status IN ('queued','retry') AND available_at<=?) OR (status='running' AND lease_until<=?)) ORDER BY available_at,created_at LIMIT 1",
                (timestamp, timestamp),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE jobs SET status='running',attempts=attempts+1,locked_by=?,locked_at=?,lease_until=?,updated_at=? WHERE id=?",
                (worker_id, timestamp, lease_until, timestamp, row["id"]),
            )
            return decode(
                connection.execute(
                    "SELECT * FROM jobs WHERE id=?", (row["id"],)
                ).fetchone()
            )

    def renew_lease(self, job_id: str, worker_id: str) -> bool:
        timestamp = now()
        until = (
            datetime.now(timezone.utc)
            + timedelta(seconds=self.settings.job_lease_seconds)
        ).isoformat(timespec="milliseconds")
        with self.connect() as connection:
            return bool(
                connection.execute(
                    "UPDATE jobs SET lease_until=?,updated_at=? WHERE id=? AND status='running' AND locked_by=? AND lease_until>?",
                    (until, timestamp, job_id, worker_id, timestamp),
                ).rowcount
            )

    heartbeat = renew_lease

    def finish_job(
        self, job_id: str, result: dict, worker_id: str | None = None
    ) -> bool:
        with self.connect() as connection:
            where = "id=? AND status='running'"
            args: list[Any] = [json_text(result), now(), job_id]
            if worker_id is not None:
                where += " AND locked_by=? AND lease_until>?"
                args.extend((worker_id, now()))
            return bool(
                connection.execute(
                    f"UPDATE jobs SET status='complete',result_json=?,updated_at=?,locked_by=NULL,locked_at=NULL,lease_until=NULL WHERE {where}",
                    args,
                ).rowcount
            )

    def fail_job(
        self, job_id: str, error: str, retryable: bool, worker_id: str | None = None
    ) -> bool:
        safe_error = re.sub(r"https?://\S+", "[source URL]", str(error))[:2000]
        safe_error = re.sub(
            r"(?i)(token|api[_-]?key|password|authorization)\s*[:=]\s*\S+",
            r"\1=[redacted]",
            safe_error,
        )
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None or row["status"] != "running":
                return False
            if worker_id is not None and (
                row["locked_by"] != worker_id or row["lease_until"] <= now()
            ):
                return False
            status = (
                "retry"
                if retryable and row["attempts"] < row["max_attempts"]
                else "failed"
            )
            available = (
                datetime.now(timezone.utc)
                + timedelta(
                    seconds=self.settings.retry_delay_seconds * max(1, row["attempts"])
                )
            ).isoformat(timespec="milliseconds")
            connection.execute(
                "UPDATE jobs SET status=?,last_error=?,available_at=?,updated_at=?,locked_by=NULL,locked_at=NULL,lease_until=NULL WHERE id=?",
                (status, safe_error, available, now(), job_id),
            )
            return True

    def retry_job(self, job_id: str) -> dict:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise SearchError("Search job not found.", 404)
            if row["status"] != "failed":
                raise SearchError("Only failed jobs can be explicitly retried.", 409)
            connection.execute(
                "UPDATE jobs SET status='queued',attempts=0,available_at=?,updated_at=? WHERE id=?",
                (now(), now(), job_id),
            )
        return self.get("jobs", job_id)

    def event(
        self,
        event_type: str,
        source_id: str | None = None,
        artifact_id: str | None = None,
        job_id: str | None = None,
        payload: dict | None = None,
        level: str = "info",
        **extra,
    ) -> str:
        identifier = new_id("evt_")
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    source_id,
                    artifact_id,
                    job_id,
                    event_type,
                    level,
                    json_text(payload or extra),
                    now(),
                ),
            )
        return identifier

    def insert_artifact(self, **record) -> dict:
        supplied_path = Path(record["path"])
        path = (
            supplied_path if supplied_path.is_absolute() else self.root / supplied_path
        ).resolve()
        if not path.is_relative_to(self.root):
            raise SearchError("Artifact path must stay inside targeted-search storage.")
        values = {
            "id": record.get("id") or new_id("art_"),
            "source_id": record["source_id"],
            "kind": record["kind"],
            "profile": record.get("profile", ""),
            "path": path.relative_to(self.root).as_posix(),
            "sha256": record["sha256"],
            "bytes": record["bytes"],
            "metadata_json": record.get("metadata_json")
            or json_text(record.get("metadata", {})),
            "parent_artifact_id": record.get("parent_artifact_id"),
            "start_ms": record.get("start_ms"),
            "end_ms": record.get("end_ms"),
            "approval_id": record.get("approval_id"),
            "created_at": record.get("created_at") or now(),
        }
        with self.connect() as connection:
            connection.execute(
                f"INSERT OR IGNORE INTO artifacts({','.join(values)}) VALUES({','.join('?' for _ in values)})",
                tuple(values.values()),
            )
            result = decode(
                connection.execute(
                    "SELECT * FROM artifacts WHERE id=?", (values["id"],)
                ).fetchone()
            )
            if (
                result is None
                or result["sha256"] != values["sha256"]
                or result["source_id"] != values["source_id"]
            ):
                raise SearchError(
                    "Artifact identity does not match the existing immutable record.",
                    409,
                )
            return result
