"""Reproducible collection ingestion and local search operations.

Run: python -m app.services.targeted_search.cli --help
"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

from app.models.search import SearchError


def import_collection(service, path: str | Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    entries = data["sources"]
    if (
        not isinstance(entries, list)
        or len(entries) > service.settings.max_collection_sources
    ):
        raise SearchError("The collection exceeds its configured source limit.")
    existing = next(
        (item for item in service.list_collections() if item["name"] == data["name"]),
        None,
    )
    collection = existing or service.add_collection(
        data["name"], data.get("topic", ""), data.get("queries", [])
    )
    selected = []
    for item in entries:
        source = service.register_metadata(
            item["url"],
            item["title"],
            item.get("description", ""),
            item.get("creator_name", ""),
            collection["id"],
            {
                **item.get("metadata", {}),
                "research_notes": item.get("selection_reason", ""),
            },
        )
        if not source.get("policy"):
            # Importing research never grants acquisition or export permission.
            service.set_policy(
                source["id"],
                "review_required",
                "internal_review",
                "Research-selected public source; media permission has not been verified.",
                "research-selection",
            )
        if item.get("discover", False):
            source = service.discover(item["url"], collection["id"])
        selected.append(
            {
                "source_id": source["id"],
                "title": source["title"],
                "job_id": source.get("job_id"),
            }
        )
    return {
        "collection_id": collection["id"],
        "name": collection["name"],
        "selected": selected,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--root",
        help="Search storage root (default: configured storage/targeted_search)",
    )
    result.add_argument(
        "--semantic",
        action="store_true",
        help="Use the configured installed local embedding model",
    )
    result.add_argument(
        "--rerank",
        action="store_true",
        help="Use the configured installed local cross-encoder",
    )
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("collections")
    commands.add_parser("cases")
    command = commands.add_parser("case-create")
    command.add_argument("name")
    command.add_argument("--topic", default="")
    for name in ["case-assets", "case-export", "case-folder"]:
        commands.add_parser(name).add_argument("case_id")
    command = commands.add_parser("case-import")
    command.add_argument("case_id")
    command.add_argument("folder")
    command.add_argument("--index", action="store_true")
    command = commands.add_parser("case-search")
    command.add_argument("case_id")
    command.add_argument("query")
    command.add_argument(
        "--mode", choices=["footage", "everything", "supporting"], default="footage"
    )
    command.add_argument("--top-k", type=int, default=20)
    commands.add_parser("case-index").add_argument("asset_id")
    command = commands.add_parser("case-storyboard")
    command.add_argument("case_id")
    command.add_argument("file")
    command = commands.add_parser("case-render")
    command.add_argument("storyboard_id")
    command.add_argument(
        "--use",
        choices=["internal_review", "generated_export", "publication"],
        default="generated_export",
    )
    command = commands.add_parser("case-words")
    command.add_argument("asset_id")
    command.add_argument("file")
    command.add_argument("--scope", choices=["source", "narration"], default="source")
    command.add_argument("--script-asset")
    command = commands.add_parser("import-collection")
    command.add_argument("file")
    command = commands.add_parser("discover")
    command.add_argument("url")
    command.add_argument("--collection")
    command = commands.add_parser("sources")
    command.add_argument("--collection")
    command = commands.add_parser("search")
    command.add_argument("query")
    command.add_argument("--collection")
    command.add_argument("--top-k", type=int, default=20)
    command = commands.add_parser("captions")
    command.add_argument("source_id")
    command.add_argument("file")
    command.add_argument(
        "--format", choices=["vtt", "srt", "json", "json3"], default="vtt"
    )
    command.add_argument("--language", default="en")
    for name in ["index", "visual-index", "transcribe", "refresh", "impact"]:
        commands.add_parser(name).add_argument("source_id")
    commands.add_parser("retry").add_argument("job_id")
    commands.add_parser("jobs")
    command = commands.add_parser("worker")
    command.add_argument(
        "--drain",
        action="store_true",
        help="Drain currently ready jobs; default processes one",
    )
    command = commands.add_parser("policy")
    command.add_argument("source_id")
    command.add_argument(
        "status",
        choices=[
            "unknown",
            "allowed_internal",
            "allowed_export",
            "review_required",
            "blocked",
            "expired",
        ],
    )
    command.add_argument("--uses", required=True, help="Comma-separated intended uses")
    command.add_argument("--reason", required=True)
    command.add_argument("--reviewer", required=True)
    command.add_argument("--expires-at")
    commands.add_parser("validate").add_argument("candidate_id")
    for name in ["approve", "clip"]:
        command = commands.add_parser(name)
        command.add_argument("candidate_id")
        command.add_argument("--start-ms", type=int)
        command.add_argument("--end-ms", type=int)
        command.add_argument("--use", default="internal_review")
        if name == "approve":
            command.add_argument("--reviewer", required=True)
    commands.add_parser("attach").add_argument("artifact_id")
    commands.add_parser("metrics")
    commands.add_parser("backup").add_argument("directory")
    commands.add_parser("verify-backup").add_argument("directory")
    command = commands.add_parser("restore-backup")
    command.add_argument("directory")
    command.add_argument("new_root")
    command = commands.add_parser("retention")
    command.add_argument("--days", type=int, default=30)
    command.add_argument(
        "--apply", action="store_true", help="Apply the conservative retention plan"
    )
    command = commands.add_parser("expand")
    command.add_argument("url")
    command.add_argument("collection_id")
    command.add_argument("--limit", type=int, default=25)
    command = commands.add_parser("evaluate")
    command.add_argument(
        "gold_file", help="JSON query rows with expected source/range labels"
    )
    command.add_argument("--top-k", type=int, default=10)
    command.add_argument(
        "--seed-fixtures",
        action="store_true",
        help="Import the gold file's owned local caption fixtures and optionally index them",
    )
    return result


def run(service, args) -> dict | list:
    command = args.command
    if command == "cases" or command.startswith("case-"):
        from .case_workspace import CaseWorkspace

        workspace = CaseWorkspace(service)
        if command == "cases":
            return workspace.list_cases()
        if command == "case-create":
            return workspace.create_case(args.name, args.topic)
        if command == "case-assets":
            return workspace.list_assets(args.case_id)
        if command == "case-export":
            return workspace.export_case(args.case_id)
        if command == "case-folder":
            from .case_workspace_ops import prepare_case_folder

            return prepare_case_folder(workspace, args.case_id)
        if command == "case-import":
            return workspace.import_folder(args.case_id, args.folder, index=args.index)
        if command == "case-index":
            return workspace.enqueue_index(args.asset_id)
        if command == "case-search":
            case = workspace.get_case(args.case_id)
            footage = (
                service.search(
                    args.query, {"source_ids": case["source_ids"]}, args.top_k
                )
                if args.mode != "supporting"
                else None
            )
            supporting = (
                workspace.search_supporting(args.case_id, args.query, top_k=args.top_k)
                if args.mode != "footage"
                else None
            )
            return {
                "case_id": args.case_id,
                "mode": args.mode,
                "footage": footage,
                "supporting": supporting,
            }
        if command == "case-storyboard":
            return workspace.save_storyboard(
                args.case_id, json.loads(Path(args.file).read_text(encoding="utf-8"))
            )
        if command == "case-render":
            return workspace.enqueue_render(args.storyboard_id, args.use)
        if command == "case-words":
            from .case_media import import_whisperx

            return import_whisperx(
                workspace,
                args.asset_id,
                json.loads(Path(args.file).read_text(encoding="utf-8")),
                scope=args.scope,
                script_asset_id=args.script_asset,
            )
    if command == "collections":
        return service.list_collections()
    if command == "import-collection":
        return import_collection(service, args.file)
    if command == "discover":
        return service.discover(args.url, args.collection)
    if command == "sources":
        return service.list_sources(args.collection)
    if command == "search":
        return service.search(
            args.query,
            {"collection_id": args.collection} if args.collection else {},
            args.top_k,
        )
    if command == "captions":
        return service.import_captions(
            args.source_id,
            Path(args.file).read_text(encoding="utf-8"),
            args.language,
            "manual",
            args.format,
        )
    if command in {"index", "visual-index", "transcribe", "refresh"}:
        method = {
            "index": "enqueue_index",
            "visual-index": "enqueue_visual",
            "transcribe": "enqueue_transcription",
            "refresh": "refresh_source",
        }[command]
        return getattr(service, method)(args.source_id)
    if command == "retry":
        return service.repo.retry_job(args.job_id)
    if command == "jobs":
        return service.list_jobs()
    if command == "worker":
        from .worker import process_once

        results = []
        while True:
            job = process_once(service)
            if not job:
                break
            results.append(service.get_job(job["id"]))
            if not args.drain:
                break
        return results
    if command == "policy":
        return service.set_policy(
            args.source_id,
            args.status,
            args.uses,
            args.reason,
            args.reviewer,
            args.expires_at,
        )
    if command == "validate":
        return service.validate(args.candidate_id)
    if command in {"approve", "clip"}:
        kwargs = dict(
            start_ms=args.start_ms, end_ms=args.end_ms, requested_use=args.use
        )
        if command == "approve":
            return service.approve_download(
                args.candidate_id, reviewed_by=args.reviewer, **kwargs
            )
        return service.enqueue_clip(args.candidate_id, **kwargs)
    if command == "attach":
        from .attachments import attach_clip

        return attach_clip(args.artifact_id, root_dir=service.repo.root)
    if command == "expand":
        return service.expand_collection(args.url, args.collection_id, args.limit)
    if command == "evaluate":
        from .evaluation import evaluate

        data = json.loads(Path(args.gold_file).read_text(encoding="utf-8"))
        if args.seed_fixtures:
            from .retrieval import build_embeddings

            for fixture in data.get("fixtures", []):
                if not fixture["url"].startswith("local://"):
                    raise SearchError(
                        "Evaluation fixtures must contain owned local captions only."
                    )
                source = service.discover(fixture["url"], metadata=fixture["metadata"])
                if service.settings.semantic_enabled:
                    build_embeddings(service, {"source_id": source["id"]})
        return evaluate(service, data, args.top_k)
    from . import operations

    if command == "metrics":
        return operations.metrics(service.repo.root)
    if command == "backup":
        return operations.backup_repository(service.repo.root, args.directory)
    if command == "verify-backup":
        return operations.verify_backup(args.directory)
    if command == "restore-backup":
        return operations.restore_backup(args.directory, args.new_root)
    if command == "retention":
        plan = operations.retention_plan(service.repo.root, older_than_days=args.days)
        return (
            operations.apply_retention(plan, service.repo.root) if args.apply else plan
        )
    if command == "impact":
        return operations.source_deletion_impact(args.source_id, service.repo.root)
    raise SearchError("Unknown search operation")


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    from .service import SearchService

    try:
        service = SearchService(args.root)
        service.settings = replace(
            service.settings,
            semantic_enabled=args.semantic or service.settings.semantic_enabled,
            rerank_enabled=args.rerank or service.settings.rerank_enabled,
        )
        service.repo.settings = service.settings
        print(
            json.dumps(
                run(service, args), ensure_ascii=False, indent=2, allow_nan=False
            )
        )
        return 0
    except (SearchError, OSError, json.JSONDecodeError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
