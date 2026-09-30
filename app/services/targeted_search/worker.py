"""Durable local search worker: CLI or one managed daemon per storage root."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import tempfile
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from app.models.search import SearchError

from .media import _staging, executable, run_command, setting, verified_artifact_path
from .repository import Repository


def transcribe_source(service, payload: dict) -> dict:
    from .acquisition import download_source
    from .captions import ingest_captions
    from .policy import authorize

    repo = service.repo
    source_id = payload["source_id"]
    requested_use = payload.get("requested_use", "analysis")
    approval_id = payload.get("approval_id")
    if not approval_id:
        raise SearchError(
            "ASR requires an approved source acquisition", status_code=403
        )
    authorize(
        repo,
        source_id,
        requested_use,
        approval_id,
        payload.get("start_ms"),
        payload.get("end_ms"),
    )
    authorize(repo, source_id, "analysis")
    if importlib.util.find_spec("faster_whisper") is None:
        raise SearchError("ASR requires faster-whisper", status_code=503)
    from faster_whisper import WhisperModel

    model_name = setting(service, "asr_model", "small")
    try:
        model = WhisperModel(
            model_name,
            device="cpu",
            compute_type="int8",
            local_files_only=setting(service, "local_models_only", True),
        )
    except (ValueError, OSError, RuntimeError) as exc:
        raise SearchError(
            "ASR model is unavailable locally; configure or install the selected model",
            status_code=503,
        ) from exc
    acquisition = download_source(service, payload)
    artifact = repo.get("artifacts", acquisition["artifact_id"])
    if not artifact.get("metadata", {}).get("has_audio"):
        raise SearchError("This source has no audio to transcribe", status_code=422)
    with tempfile.TemporaryDirectory(prefix="asr-", dir=_staging(repo)) as directory:
        audio_path = Path(directory) / "analysis.wav"
        run_command(
            [
                executable("ffmpeg"),
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(verified_artifact_path(repo, artifact)),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                str(audio_path),
            ],
            timeout=setting(service, "command_timeout_seconds", 1800),
        )
        segments, info = model.transcribe(str(audio_path), vad_filter=True, beam_size=5)
        cues = [
            {
                "start_ms": round(segment.start * 1000),
                "end_ms": round(segment.end * 1000),
                "text": segment.text,
            }
            for segment in segments
        ]
    if not cues:
        raise SearchError("ASR found no timestamped speech", status_code=422)
    authorize(
        repo,
        source_id,
        requested_use,
        approval_id,
        payload.get("start_ms"),
        payload.get("end_ms"),
    )
    authorize(repo, source_id, "analysis")
    caption = ingest_captions(
        repo,
        source_id,
        cues,
        language=info.language,
        kind="asr",
        provider="faster-whisper:" + model_name,
    )
    from importlib.metadata import version

    with repo.connect() as connection:
        connection.execute(
            "UPDATE captions SET metadata_json=? WHERE id=?",
            (
                json.dumps(
                    {
                        "model_name": model_name,
                        "runtime_version": version("faster-whisper"),
                        "source_sha256": artifact["sha256"],
                        "source_artifact_id": artifact["id"],
                    },
                    sort_keys=True,
                ),
                caption["id"],
            ),
        )
    return {
        "source_id": source_id,
        "caption_id": caption["id"],
        "cues": len(cues),
        "model_name": model_name,
    }


def _dispatch(service, job: dict) -> dict:
    kind, payload = job["job_type"], job["payload"]
    if kind in {"case_acoustic_analyze", "case_acoustic_mix"}:
        from .acoustic_pipeline import AcousticPipeline
        from .case_workspace import CaseWorkspace

        workspace = CaseWorkspace(service)
        pipeline = AcousticPipeline(workspace)
        if kind == "case_acoustic_analyze":
            return pipeline.analyze(
                payload["case_id"], payload["options"],
                expected_input_hash=payload["input_hash"],
            )
        from .acoustic_compositor import render_mix

        record = pipeline.authorize_plan(
            payload["case_id"], payload["plan_id"],
            expected_revision=payload["revision"], expected_hash=payload["content_hash"],
        )
        return render_mix(workspace, record)
    if kind == "case_documentary":
        from .case_workspace import CaseWorkspace
        from .documentary import DocumentaryWriter

        writer = DocumentaryWriter(CaseWorkspace(service))
        options = payload["options"]
        if options.get("document_id") and options.get("stage", "outline") != "outline":
            document = writer.get_document(payload["case_id"], options["document_id"])
            if document["revision"] != payload.get("document_revision"):
                raise SearchError(
                    "Documentary draft changed; enqueue its current revision", 409
                )
        return writer.generate(
            payload["case_id"],
            options,
            expected_packet_hash=payload["packet_hash"],
        )
    if kind in {"case_index", "case_render", "case_align"}:
        from .case_workspace import CaseWorkspace

        workspace = CaseWorkspace(service)
        if kind == "case_index":
            from .case_media import index_asset

            asset = workspace.get_asset(payload["asset_id"])
            if (
                asset["case_id"] != payload["case_id"]
                or asset["sha256"] != payload["input_sha256"]
                or asset["asset_version_id"] != payload["asset_version_id"]
            ):
                raise SearchError(
                    "Case indexing input was superseded; enqueue its current version",
                    409,
                )
            result = index_asset(workspace, asset["id"])
            if service.settings.semantic_enabled:
                if asset["asset_kind"] == "video":
                    from .retrieval import build_embeddings

                    result["semantic_index"] = build_embeddings(
                        service, {"source_id": asset["source_id"]}
                    )
                else:
                    from .case_retrieval import build_supporting_embeddings

                    result["semantic_index"] = build_supporting_embeddings(
                        workspace, asset["id"]
                    )
            return result
        if kind == "case_align":
            from .case_media import align_audio

            asset = workspace.get_asset(payload["asset_id"])
            if payload.get("input_sha256", asset["sha256"]) != asset["sha256"]:
                raise SearchError("Alignment recording was superseded", 409)
            return align_audio(workspace, **payload)
        from .case_production import render_storyboard

        return render_storyboard(
            workspace,
            payload["storyboard_id"],
            requested_use=payload.get("requested_use", "generated_export"),
            expected_hash=payload.get("storyboard_hash"),
        )
    if kind in {"discovery", "discover_source"}:
        from .discovery import ingest_source

        return ingest_source(service, payload)
    if kind == "download_source":
        from .acquisition import download_source

        return download_source(service, payload)
    if kind in {"extract_clip", "clip"}:
        from .media import extract_clip

        return extract_clip(service, payload)
    if kind in {"visual_index", "visual"}:
        from .visual import visual_index

        return visual_index(service, payload)
    if kind in {"asr", "ASR", "transcribe_source"}:
        return transcribe_source(service, payload)
    if kind in {"embedding", "embeddings", "build_embeddings"}:
        from .retrieval import build_embeddings

        return build_embeddings(service, payload)
    raise SearchError("Unsupported targeted-search job type", status_code=422)


def process_once(
    service=None, root_dir=None, worker_id: str | None = None
) -> dict | None:
    """Claim one durable job; lease heartbeat protects long FFmpeg/model work."""
    if service is None:
        from .service import SearchService

        service = SearchService(root_dir)
    repo = service.repo
    worker_id = worker_id or (f"worker-{os.getpid()}-" + uuid.uuid4().hex)
    job = repo.claim_job(worker_id)
    if not job:
        return None
    heartbeat_stop, lease_lost = threading.Event(), threading.Event()

    def heartbeat():
        interval = max(0.2, min(30, repo.settings.job_lease_seconds / 3))
        while not heartbeat_stop.wait(interval):
            try:
                if not repo.renew_lease(job["id"], worker_id):
                    lease_lost.set()
                    return
            except Exception:
                lease_lost.set()
                return

    monitor = threading.Thread(target=heartbeat, name="search-lease", daemon=True)
    monitor.start()
    try:
        repo.event(
            "job_started",
            source_id=job["payload"].get("source_id"),
            job_id=job["id"],
            payload={
                "job_type": job["job_type"],
                "attempt": job["attempts"],
                "worker_id": worker_id,
            },
        )
        result = _dispatch(service, job)
        if lease_lost.is_set() or not repo.finish_job(
            job["id"], result, worker_id=worker_id
        ):
            raise SearchError(
                "Worker lease was lost before completion", status_code=409
            )
        repo.event(
            "job_complete",
            source_id=job["payload"].get("source_id"),
            job_id=job["id"],
            payload={"job_type": job["job_type"], "result": result},
        )
    except SearchError as exc:
        # Missing optional models/dependencies and policy denials require user
        # action rather than automatic retry storms. Network/timeout/temporary
        # disk errors can retry within the persisted job attempt budget.
        retryable = exc.status_code in {408, 429, 500, 502, 504}
        repo.fail_job(job["id"], exc.message, retryable=retryable, worker_id=worker_id)
        repo.event(
            "job_failed",
            source_id=job["payload"].get("source_id"),
            job_id=job["id"],
            level="error",
            payload={"error": exc.message, "retryable": retryable},
        )
    except Exception as exc:
        safe_error = f"{type(exc).__name__}: search processing failed"
        repo.fail_job(
            job["id"],
            safe_error,
            retryable=isinstance(exc, (OSError, TimeoutError)),
            worker_id=worker_id,
        )
        repo.event(
            "job_failed",
            source_id=job["payload"].get("source_id"),
            job_id=job["id"],
            level="error",
            payload={"error": safe_error},
        )
    finally:
        heartbeat_stop.set()
        monitor.join(timeout=1)
    return repo.get("jobs", job["id"])


@contextmanager
def _worker_lock(root: Path):
    """One process owns a root's drain loop; DB claims additionally survive crashes."""
    stream = (root / "worker.lock").open("a+")
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt

            stream.seek(0)
            if not stream.read(1):
                stream.write("0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
            except OSError:
                pass
        else:
            import fcntl

            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
        yield acquired
    finally:
        if acquired:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)
        stream.close()


@dataclass
class WorkerHandle:
    root: Path
    thread: threading.Thread
    stop_event: threading.Event

    def stop(self):
        self.stop_event.set()


_handles: dict[str, WorkerHandle] = {}
_handles_lock = threading.Lock()


def ensure_worker_running(root_dir=None) -> WorkerHandle:
    repo = Repository(root_dir)
    key = str(repo.root)
    with _handles_lock:
        existing = _handles.get(key)
        if existing and existing.thread.is_alive():
            return existing
        stop_event = threading.Event()

        def run():
            from .service import SearchService

            service = SearchService(repo.root)
            worker_id = f"embedded-{os.getpid()}-" + uuid.uuid4().hex
            # If a separately launched CLI owns the root, remain available to
            # take over after it exits without scheduling duplicate workers.
            while not stop_event.is_set():
                with _worker_lock(repo.root) as acquired:
                    if acquired:
                        while not stop_event.is_set():
                            if process_once(service, worker_id=worker_id) is None:
                                stop_event.wait(1)
                stop_event.wait(1)

        thread = threading.Thread(
            target=run, name="targeted-search-worker", daemon=True
        )
        handle = WorkerHandle(repo.root, thread, stop_event)
        _handles[key] = handle
        thread.start()
        return handle


def stop_worker(root_dir=None) -> None:
    from .settings import storage_root

    key = str(storage_root(root_dir))
    with _handles_lock:
        handle = _handles.get(key)
        if handle:
            handle.stop()
    if handle:
        # A running media operation retains its lease until it finishes. A
        # shutdown request prevents new claims and never blocks app shutdown.
        handle.thread.join(timeout=2)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the durable targeted video search worker"
    )
    parser.add_argument(
        "--root",
        type=Path,
        help="Search storage root (default storage/targeted_search)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Process one ready job")
    mode.add_argument(
        "--drain", action="store_true", help="Process all currently ready jobs"
    )
    args = parser.parse_args(argv)
    from .service import SearchService

    service = SearchService(args.root)
    stop_event = threading.Event()
    worker_id = f"cli-{os.getpid()}-" + uuid.uuid4().hex
    with _worker_lock(service.repo.root) as acquired:
        if not acquired:
            print("Another worker already owns this search storage root.")
            return 0
        try:
            while True:
                job = process_once(service, worker_id=worker_id)
                if job:
                    print(
                        json.dumps(
                            {
                                "job_id": job["id"],
                                "job_type": job["job_type"],
                                "status": job["status"],
                                "error": job.get("last_error"),
                            },
                            ensure_ascii=False,
                        )
                    )
                if args.once or (args.drain and job is None):
                    return 1 if job and job["status"] == "failed" else 0
                if job is None:
                    stop_event.wait(1)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
