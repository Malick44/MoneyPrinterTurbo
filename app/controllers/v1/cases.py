"""Authenticated local case workspace endpoints; imports never grant rights."""

from __future__ import annotations

from functools import wraps
from typing import Literal

from fastapi import Depends
from fastapi.responses import FileResponse
from pydantic import Field

from app.config import config
from app.controllers import base
from app.controllers.v1.base import new_router
from app.models.case_workspace import (
    CaseCreate,
    CaseQuery,
    FolderImport,
    RecordBody,
    RenderBody,
    SourceLink,
    StrictModel,
    StoryboardRecord,
)
from app.models.exception import HttpException
from app.models.search import RightsStatus, SearchError
from app.utils import utils


router = new_router(dependencies=[Depends(base.verify_token)])
router.tags = ["Case workspace"]


class PreviewBody(StrictModel):
    locator: dict | None = None
    requested_use: Literal["internal_review", "analysis"] = "internal_review"


class TranscriptBody(StrictModel):
    payload: dict
    scope: Literal["source", "narration"] = "source"
    script_asset_id: str | None = None


class AlignmentBody(StrictModel):
    script_asset_id: str | None = None
    scope: Literal["source", "narration"] = "source"


class RightsBody(StrictModel):
    rights_status: RightsStatus
    permitted_use: list[str] | str
    reason: str = Field(min_length=1, max_length=4000)
    reviewed_by: str = Field(min_length=1, max_length=200)
    expires_at: str | None = None


def get_workspace():
    if not config.app.get("targeted_search_enabled", True):
        raise HttpException("case", 503, "Targeted search is disabled")
    from app.services.targeted_search.case_workspace import CaseWorkspace
    from app.services.targeted_search.service import SearchService

    return CaseWorkspace(SearchService())


def endpoint(function):
    @wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except SearchError as exc:
            raise HttpException("case", exc.status_code, str(exc)) from exc

    return call


def _response(value):
    result = utils.get_response(200, value)
    result["data"] = value
    return result


def _start_worker():
    from app.services.targeted_search.worker import ensure_worker_running

    ensure_worker_running()


def _asset_in_case(workspace, case_id, asset_id):
    asset = workspace.get_asset(asset_id)
    if asset["case_id"] != case_id:
        raise SearchError("Asset does not belong to this case.", 404)
    return asset


@router.get("/cases")
@endpoint
def list_cases():
    return _response(get_workspace().list_cases())


@router.post("/cases")
@endpoint
def create_case(body: CaseCreate):
    return _response(get_workspace().create_case(**body.model_dump()))


@router.get("/cases/capabilities")
@endpoint
def capabilities():
    from app.services.targeted_search.case_media import (
        capabilities as media_capabilities,
    )

    workspace = get_workspace()
    return _response(
        {
            "media": media_capabilities(workspace),
            "import_roots": [str(path) for path in workspace.allowed_import_roots()],
            "search": workspace.search_service.capabilities(),
        }
    )


@router.get("/cases/{case_id}")
@endpoint
def get_case(case_id: str):
    return _response(get_workspace().get_case(case_id))


@router.get("/cases/{case_id}/assets")
@endpoint
def assets(case_id: str):
    return _response(get_workspace().list_assets(case_id))


@router.post("/cases/{case_id}/prepare-folder")
@endpoint
def prepare_folder(case_id: str):
    from app.services.targeted_search.case_workspace_ops import prepare_case_folder

    workspace = get_workspace()
    workspace.get_case(case_id)
    return _response(prepare_case_folder(workspace, case_id))


@router.post("/cases/{case_id}/import-folder")
@endpoint
def import_folder(case_id: str, body: FolderImport):
    result = get_workspace().import_folder(case_id, **body.model_dump())
    if result["jobs"]:
        _start_worker()
    return _response(result)


@router.post("/cases/{case_id}/sources")
@endpoint
def link_source(case_id: str, body: SourceLink):
    return _response(get_workspace().link_source(case_id, **body.model_dump()))


@router.post("/cases/{case_id}/assets/{asset_id}/policy")
@endpoint
def policy(case_id: str, asset_id: str, body: RightsBody):
    workspace = get_workspace()
    asset = _asset_in_case(workspace, case_id, asset_id)
    return _response(
        workspace.search_service.set_policy(asset["source_id"], **body.model_dump())
    )


@router.post("/cases/{case_id}/assets/{asset_id}/index")
@endpoint
def index_asset(case_id: str, asset_id: str):
    workspace = get_workspace()
    _asset_in_case(workspace, case_id, asset_id)
    result = workspace.enqueue_index(asset_id)
    _start_worker()
    return _response(result)


@router.post("/cases/{case_id}/assets/{asset_id}/refresh")
@endpoint
def refresh_asset(case_id: str, asset_id: str):
    workspace = get_workspace()
    _asset_in_case(workspace, case_id, asset_id)
    return _response(workspace.refresh_linked_asset(asset_id))


@router.post("/cases/{case_id}/assets/{asset_id}/align")
@endpoint
def align_asset(case_id: str, asset_id: str, body: AlignmentBody):
    workspace = get_workspace()
    _asset_in_case(workspace, case_id, asset_id)
    result = workspace.enqueue_alignment(asset_id, **body.model_dump())
    _start_worker()
    return _response(result)


@router.post("/cases/{case_id}/assets/{asset_id}/whisperx")
@endpoint
def import_words(case_id: str, asset_id: str, body: TranscriptBody):
    from app.services.targeted_search.case_media import import_whisperx

    workspace = get_workspace()
    _asset_in_case(workspace, case_id, asset_id)
    return _response(import_whisperx(workspace, asset_id, **body.model_dump()))


@router.post("/cases/{case_id}/assets/{asset_id}/preview")
@endpoint
def preview(case_id: str, asset_id: str, body: PreviewBody):
    from app.services.targeted_search.case_media import preview_asset

    workspace = get_workspace()
    asset = _asset_in_case(workspace, case_id, asset_id)
    if body.locator:
        workspace._locator(asset, body.locator.get("kind"), body.locator)
    result = preview_asset(workspace, asset_id, body.locator, body.requested_use)
    return _response(
        {
            key: value
            for key, value in result.items()
            if key not in {"path", "metadata_json"}
        }
    )


@router.get("/cases/{case_id}/assets/{asset_id}/content")
@endpoint
def content(case_id: str, asset_id: str):
    from app.services.targeted_search.case_media import asset_content

    workspace = get_workspace()
    asset = _asset_in_case(workspace, case_id, asset_id)
    return FileResponse(asset_content(workspace, asset_id), filename=asset["filename"])


@router.get("/cases/{case_id}/assets/{asset_id}/derivatives/{artifact_id}/content")
@endpoint
def derivative_content(case_id: str, asset_id: str, artifact_id: str):
    from app.services.targeted_search.case_media import preview_content

    workspace = get_workspace()
    asset = _asset_in_case(workspace, case_id, asset_id)
    workspace.authorize_asset(asset_id, "internal_review")
    artifact = workspace.repo.get("artifacts", artifact_id)
    with workspace.repo.connect() as connection:
        mapping = connection.execute(
            "SELECT id FROM case_derivatives WHERE asset_id=? AND asset_version_id=? AND artifact_id=?",
            (asset_id, asset["asset_version_id"], artifact_id),
        ).fetchone()
    if not artifact or artifact["source_id"] != asset["source_id"] or not mapping:
        raise SearchError("Derivative does not belong to the current case asset.", 404)
    return FileResponse(preview_content(workspace, artifact_id, "internal_review"))


@router.post("/cases/{case_id}/search")
@endpoint
def supporting(case_id: str, body: CaseQuery):
    return _response(get_workspace().search_supporting(case_id, **body.model_dump()))


@router.get("/cases/{case_id}/requests")
@endpoint
def requests(case_id: str):
    return _response(get_workspace().list_requests(case_id))


@router.post("/cases/{case_id}/requests")
@endpoint
def save_request(case_id: str, body: RecordBody):
    return _response(get_workspace().save_request(case_id, body.record))


@router.get("/cases/{case_id}/claims")
@endpoint
def claims(case_id: str):
    return _response(get_workspace().list_claims(case_id))


@router.post("/cases/{case_id}/claims")
@endpoint
def save_claim(case_id: str, body: RecordBody):
    return _response(get_workspace().save_claim(case_id, body.record))


@router.get("/cases/{case_id}/events")
@endpoint
def events(case_id: str):
    return _response(get_workspace().list_events(case_id))


@router.post("/cases/{case_id}/events")
@endpoint
def save_event(case_id: str, body: RecordBody):
    return _response(get_workspace().save_event(case_id, body.record))


@router.get("/cases/{case_id}/entities")
@endpoint
def entities(case_id: str):
    return _response(get_workspace().list_entities(case_id))


@router.post("/cases/{case_id}/entities")
@endpoint
def save_entity(case_id: str, body: RecordBody):
    return _response(get_workspace().save_entity(case_id, body.record))


@router.post("/cases/{case_id}/mentions")
@endpoint
def save_mention(case_id: str, body: RecordBody):
    return _response(get_workspace().save_mention(case_id, body.record))


@router.get("/cases/{case_id}/storyboards")
@endpoint
def storyboards(case_id: str):
    return _response(get_workspace().list_storyboards(case_id))


@router.post("/cases/{case_id}/storyboards")
@endpoint
def save_storyboard(case_id: str, body: StoryboardRecord):
    return _response(get_workspace().save_storyboard(case_id, body.model_dump()))


@router.get("/cases/{case_id}/storyboards/{storyboard_id}")
@endpoint
def get_storyboard(case_id: str, storyboard_id: str):
    storyboard = get_workspace().get_storyboard(storyboard_id)
    if storyboard["case_id"] != case_id:
        raise SearchError("Storyboard does not belong to this case.", 404)
    return _response(storyboard)


@router.delete("/cases/{case_id}/storyboards/{storyboard_id}")
@endpoint
def delete_storyboard(case_id: str, storyboard_id: str):
    workspace = get_workspace()
    if workspace.get_storyboard(storyboard_id)["case_id"] != case_id:
        raise SearchError("Storyboard does not belong to this case.", 404)
    return _response(workspace.delete_storyboard(storyboard_id))


@router.post("/cases/{case_id}/storyboards/{storyboard_id}/render")
@endpoint
def render(case_id: str, storyboard_id: str, body: RenderBody):
    workspace = get_workspace()
    storyboard = workspace.get_storyboard(storyboard_id)
    if storyboard["case_id"] != case_id:
        raise SearchError("Storyboard does not belong to this case.", 404)
    result = workspace.enqueue_render(storyboard_id, body.requested_use)
    _start_worker()
    return _response(result)


@router.get("/cases/{case_id}/export")
@endpoint
def export(case_id: str):
    return _response(get_workspace().export_case(case_id))


@router.get("/cases/{case_id}/renders/{artifact_id}/content")
@router.get("/cases/{case_id}/production/{artifact_id}/content")
@endpoint
def render_content(
    case_id: str,
    artifact_id: str,
    requested_use: Literal[
        "internal_review", "generated_export", "publication"
    ] = "generated_export",
):
    from app.services.targeted_search.case_production import authorize_render_artifact

    workspace = get_workspace()
    workspace.get_case(case_id)
    artifact = workspace.repo.get("artifacts", artifact_id)
    if not artifact or artifact.get("metadata", {}).get("case_id") != case_id:
        raise SearchError("Render does not belong to this case.", 404)
    return FileResponse(
        authorize_render_artifact(workspace, artifact_id, requested_use)
    )
