"""Source browsing keeps originals and current permissions ahead of setup tools."""

from copy import deepcopy
from unittest.mock import patch

from test.services.test_webui_case_workspace import FakeWorkspace, app_for
from webui import case_workspace as ui


def test_library_opens_source_preview_before_optional_import_and_indexing():
    workspace = FakeWorkspace()
    with patch.object(ui, "render_asset_preview") as preview:
        app = app_for("_render_library", workspace)
        assert not list(app.exception)
        panels = {item.label: item for item in app.expander}
        labels = [item.label for item in app.expander]
        assert panels["Preview original asset"].proto.expanded
        assert not panels["Import case folder"].proto.expanded
        assert not panels["Make case files searchable"].proto.expanded
        assert labels.index("Preview original asset") < labels.index("Import case folder")
        assert labels.index("Review source rights") < labels.index("Make case files searchable")
        assert preview.call_args.args[:2] == (workspace, workspace.asset)
        assert preview.call_count == 1
        workspace.import_folder.assert_not_called()
        workspace.enqueue_index.assert_not_called()


def test_library_retains_every_file_and_reads_rights_from_current_source_policy():
    workspace = FakeWorkspace()
    audio = {
        **deepcopy(workspace.asset),
        "id": "asset-b",
        "source_id": "source-b",
        "filename": "Original_Recording.wav",
        "relative_path": "02_Audio_Raw/Original_Recording.wav",
        "asset_kind": "audio",
        "state": "indexed",
        "rights_status": "allowed_export",
    }
    workspace.list_assets.return_value = [workspace.asset, audio]
    policies = {"source-a": "blocked", "source-b": "allowed_internal"}
    workspace.search_service.get_source.side_effect = lambda source_id: {
        "id": source_id,
        "policy": {"rights_status": policies[source_id]},
    }
    with (
        patch.object(ui, "get_workspace", return_value=workspace),
        patch.object(ui, "render_asset_preview") as preview,
    ):
        app = app_for("_render_library", workspace)
        assert not list(app.exception)
        table = app.dataframe[0].value
        assert table["Original filename"].tolist() == [
            workspace.asset["relative_path"],
            audio["relative_path"],
        ]
        assert table["Rights status"].tolist() == [
            "rights_status.blocked",
            "rights_status.allowed_internal",
        ]
        app.selectbox(key=ui._key("case-a", "review_asset")).set_value("asset-b").run()
        assert not list(app.exception)
        assert preview.call_args.args[:2] == (workspace, audio)
        workspace.enqueue_index.assert_not_called()
