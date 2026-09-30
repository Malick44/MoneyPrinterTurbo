"""Authenticated endpoints for the persistent targeted footage library."""

from __future__ import annotations

from functools import wraps
from typing import Literal

from fastapi import Depends, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config import config
from app.controllers import base
from app.controllers.v1.base import new_router
from app.models.exception import HttpException
from app.utils import utils

router = new_router(dependencies=[Depends(base.verify_token)])
router.tags = ["Targeted video search"]


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DiscoverBody(Body):
    url: str = Field(min_length=1, max_length=2000)
    collection_id: str | None = None


class MetadataBody(DiscoverBody):
    title: str = Field(min_length=1, max_length=1000)
    description: str = Field(default="", max_length=20000)
    creator_name: str = Field(default="", max_length=300)
    metadata: dict = Field(default_factory=dict)


class QueryBody(Body):
    query: str = Field(min_length=1, max_length=2000)
    filters: dict = Field(default_factory=dict)
    top_k: int = Field(default=20, ge=1, le=100)


class PolicyBody(Body):
    rights_status: Literal["unknown", "allowed_internal", "allowed_export", "review_required", "blocked", "expired"]
    permitted_use: list[str] | str = "internal_review"
    reason: str = Field(min_length=1, max_length=4000)
    reviewed_by: str = Field(min_length=1, max_length=200)
    expires_at: str | None = None


class RangeBody(Body):
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, gt=0)
    requested_use: Literal["internal_review", "analysis", "generated_export", "clip_export", "publication", "archival"] = "internal_review"

    @model_validator(mode="after")
    def valid_range(self):
        if (self.start_ms is None) != (self.end_ms is None):
            raise ValueError("Supply both start_ms and end_ms")
        if self.start_ms is not None and self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be after start_ms")
        return self


class ApprovalBody(RangeBody):
    reviewed_by: str = Field(min_length=1, max_length=200)


class ClipBody(RangeBody):
    candidate_id: str = Field(min_length=1, max_length=128)


class CollectionBody(Body):
    name: str = Field(min_length=1, max_length=200)
    topic: str = Field(min_length=1, max_length=1000)
    queries: list[str] = Field(default_factory=list, max_length=100)


class CaptionsBody(Body):
    text: str = Field(min_length=1, max_length=5000000)
    language: str = Field(default="en", max_length=32)
    kind: Literal["vtt", "srt", "json"] = "vtt"


class ExpandBody(DiscoverBody):
    collection_id: str
    limit: int = Field(default=25, ge=1, le=500)


def get_service():
    if not config.app.get("targeted_search_enabled", True):
        raise HttpException("search", 503, "Targeted search is disabled")
    from app.services.targeted_search.service import SearchService

    return SearchService()


def _start_worker():
    from app.services.targeted_search.worker import ensure_worker_running

    ensure_worker_running()


def endpoint(function):
    @wraps(function)
    def call(*args, **kwargs):
        from app.models.search import SearchError

        try:
            return function(*args, **kwargs)
        except SearchError as exc:
            raise HttpException("search", getattr(exc, "status_code", 400), str(exc)) from exc
    return call


@router.get("/search/collections")
@endpoint
def collections():
    return utils.get_response(200, get_service().list_collections())


@router.get("/search/capabilities")
@endpoint
def capabilities():
    return utils.get_response(200, get_service().capabilities())


@router.get("/search/metrics")
@endpoint
def metrics():
    from app.services.targeted_search.operations import metrics as search_metrics

    return utils.get_response(200, search_metrics(get_service().repo.root))


@router.post("/search/collections/expand")
@endpoint
def expand(body: ExpandBody):
    result = get_service().expand_collection(**body.model_dump())
    _start_worker()
    return utils.get_response(200, result)


@router.post("/search/collections")
@endpoint
def add_collection(body: CollectionBody):
    return utils.get_response(200, get_service().add_collection(**body.model_dump()))


@router.get("/search/sources")
@endpoint
def sources(collection_id: str | None = None):
    return utils.get_response(200, get_service().list_sources(collection_id))


@router.post("/search/sources/discover")
@endpoint
def discover(body: DiscoverBody):
    result = get_service().discover(**body.model_dump())
    _start_worker()
    return utils.get_response(200, result)


@router.post("/search/sources/metadata")
@endpoint
def metadata(body: MetadataBody):
    return utils.get_response(200, get_service().register_metadata(**body.model_dump()))


@router.get("/search/sources/{source_id}")
@endpoint
def source(source_id: str):
    return utils.get_response(200, get_service().get_source(source_id))


@router.post("/search/sources/{source_id}/policy")
@endpoint
def policy(source_id: str, body: PolicyBody):
    return utils.get_response(200, get_service().set_policy(source_id, **body.model_dump()))


@router.post("/search/sources/{source_id}/captions")
@endpoint
def captions(source_id: str, body: CaptionsBody):
    return utils.get_response(200, get_service().import_captions(
        source_id, text=body.text, language=body.language, format=body.kind, kind="manual",
    ))


@router.post("/search/query")
@endpoint
def query(body: QueryBody):
    return utils.get_response(200, get_service().search(**body.model_dump()))


@router.post("/search/candidates/{candidate_id}/validate")
@endpoint
def validate(candidate_id: str):
    return utils.get_response(200, get_service().validate(candidate_id))


@router.post("/search/candidates/{candidate_id}/approve-download")
@endpoint
def approve(candidate_id: str, body: ApprovalBody):
    result = get_service().approve_download(candidate_id, **body.model_dump())
    _start_worker()
    return utils.get_response(200, result)


@router.post("/search/clips")
@endpoint
def extract(body: ClipBody):
    result = get_service().enqueue_clip(**body.model_dump())
    _start_worker()
    return utils.get_response(200, result)


@router.get("/search/clips/{clip_id}")
@endpoint
def clip(clip_id: str):
    return utils.get_response(200, get_service().get_clip(clip_id))


@router.post("/search/clips/{clip_id}/attach")
@endpoint
def attach(clip_id: str):
    from app.services.targeted_search.attachments import attach_clip

    return utils.get_response(200, attach_clip(clip_id, "generated_export"))


@router.get("/search/artifacts/{artifact_id}/content")
@endpoint
def content(artifact_id: str, request: Request, download: bool = False):
    from app.services.targeted_search.attachments import artifact_content

    artifact = get_service().repo.get("artifacts", artifact_id)
    file_path = artifact_content(artifact_id, requested_use="clip_export" if download else "internal_review")
    response = FileResponse(
        file_path, filename=file_path.name if download else None,
        content_disposition_type="attachment" if download else "inline",
    )
    response.headers["Cache-Control"] = "private, no-store"
    if artifact and artifact.get("sha256"):
        response.headers["ETag"] = f'"{artifact["sha256"]}"'
    return response


@router.get("/search/jobs")
@endpoint
def jobs():
    return utils.get_response(200, get_service().list_jobs())


@router.get("/search/jobs/{job_id}")
@endpoint
def job(job_id: str):
    return utils.get_response(200, get_service().get_job(job_id))


@router.post("/search/jobs/{job_id}/retry")
@endpoint
def retry(job_id: str):
    result = get_service().repo.retry_job(job_id)
    _start_worker()
    return utils.get_response(200, get_service().get_job(result["id"]))


@router.post("/search/sources/{source_id}/refresh")
@endpoint
def refresh(source_id: str):
    result = get_service().refresh_source(source_id)
    _start_worker()
    return utils.get_response(200, result)


@router.post("/search/sources/{source_id}/index")
@endpoint
def index(source_id: str):
    result = get_service().enqueue_index(source_id)
    _start_worker()
    return utils.get_response(200, result)


@router.post("/search/sources/{source_id}/visual-index")
@endpoint
def visual_index(source_id: str):
    result = get_service().enqueue_visual(source_id)
    _start_worker()
    return utils.get_response(200, result)


@router.post("/search/sources/{source_id}/transcribe")
@endpoint
def transcribe(source_id: str):
    result = get_service().enqueue_transcription(source_id)
    _start_worker()
    return utils.get_response(200, result)
