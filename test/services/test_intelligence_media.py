import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image

from app.intelligence import contact_sheets, execution
from app.intelligence.contracts import ProductionPlan, SceneMaterial, ScenePlan
from app.models.schema import VideoAspect, VideoParams
from app.services import video


def plan_fixture():
    return ProductionPlan(
        title="Colors",
        summary="Two scenes",
        scenes=[
            ScenePlan(
                scene_id="red",
                narration="Red.",
                purpose="Introduce red",
                target_duration=1,
                visual_intent="A red field",
                preferred_visual_type="local_asset",
            ),
            ScenePlan(
                scene_id="blue",
                narration="Blue.",
                purpose="Introduce blue",
                target_duration=3,
                visual_intent="A blue field",
                preferred_visual_type="local_asset",
            ),
        ],
    )


def test_render_frames_follow_unequal_scene_boundaries(tmp_path):
    plan = plan_fixture()
    with patch.object(
        contact_sheets,
        "extract_thumbnail",
        return_value=Image.new("RGB", (40, 40), "red"),
    ) as extract:
        result = contact_sheets.create_render_contact_sheet(
            "render.mp4", plan, tmp_path / "sheet.jpg", duration=8
        )
    timestamps = [call.args[1] for call in extract.call_args_list]
    assert timestamps == pytest.approx([0.08, 1, 1.92, 2.08, 5, 7.92])
    assert Path(result).is_file()
    with Image.open(result) as sheet:
        assert sheet.width == 1440
        assert sheet.height == 1120


def test_explicit_actual_timeline_overrides_estimates(tmp_path):
    with patch.object(
        contact_sheets, "extract_thumbnail", return_value=Image.new("RGB", (40, 40))
    ) as extract:
        contact_sheets.create_render_contact_sheet(
            "render.mp4",
            plan_fixture(),
            tmp_path / "sheet.jpg",
            timeline=[
                {"scene_id": "red", "start": 0, "end": 5},
                {"scene_id": "blue", "start": 5, "end": 6},
            ],
        )
    assert extract.call_args_list[1].args[1] == 2.5
    assert extract.call_args_list[4].args[1] == 5.5


def test_material_order_and_missing_assets_are_visible(tmp_path):
    red = tmp_path / "red.png"
    Image.new("RGB", (50, 50), "red").save(red)
    materials = [SimpleNamespace(scene_id="red", paths=[str(red)])]
    result = contact_sheets.create_material_contact_sheet(
        materials, plan_fixture(), tmp_path / "sheet.jpg"
    )
    with Image.open(result) as sheet:
        assert sheet.width == 960
        assert sheet.getpixel((100, 100))[0] > 200
        # Missing scene remains a visibly red error tile, never omitted.
        assert sheet.getpixel((485, 5))[0] > sheet.getpixel((485, 5))[1] * 1.5


def test_portrait_evidence_keeps_full_width_and_uncropped_edges(tmp_path):
    source = Image.new("RGB", (1080, 1920), "red")
    source.paste("blue", (0, 1500, 1080, 1920))
    with patch.object(contact_sheets, "extract_thumbnail", return_value=source):
        first = contact_sheets._sheet(
            [("portrait.png", 0, "scene-1 | asset 1")], tmp_path / "portrait.jpg"
        )
    pages = contact_sheets.contact_sheet_pages(first)
    assert len(pages) == 1
    with Image.open(pages[0]) as sheet:
        assert sheet.size == (480, 933)
        # Portrait pixels occupy the complete 480px width instead of 114px.
        assert sheet.getpixel((5, 100))[0] > 240
        assert sheet.getpixel((475, 100))[0] > 240
        assert sheet.getpixel((240, 840))[2] > 240
        assert sheet.getpixel((240, 860))[0] < 100  # Separate caption area.


@pytest.mark.parametrize(
    "size", [(1920, 1080), (1080, 1920), (1080, 1080), (100, 10000)]
)
def test_orientation_aware_pages_stay_within_image_bounds(tmp_path, size):
    with patch.object(
        contact_sheets, "extract_thumbnail", return_value=Image.new("RGB", size, "red")
    ):
        first = contact_sheets._sheet(
            [(f"asset-{index}", 0, f"scene-{index}") for index in range(30)],
            tmp_path / "sheet.jpg",
        )
    for path in contact_sheets.contact_sheet_pages(first):
        with Image.open(path) as page:
            assert page.width <= 1440
            assert page.height <= 3000
            assert page.width * page.height <= 4_320_000


def test_mixed_orientations_share_rows_without_cropping(tmp_path):
    images = [
        Image.new("RGB", (1920, 1080), "red"),
        Image.new("RGB", (1080, 1920), "green"),
        Image.new("RGB", (1000, 1000), "blue"),
    ]
    with patch.object(contact_sheets, "extract_thumbnail", side_effect=images):
        first = contact_sheets._sheet(
            [(str(index), 0, f"scene-{index}") for index in range(3)],
            tmp_path / "mixed.jpg",
        )
    with Image.open(first) as sheet:
        assert sheet.size == (1440, 933)
        assert sheet.getpixel((240, 400))[0] > 240
        assert sheet.getpixel((720, 400))[1] > 100
        assert sheet.getpixel((1200, 400))[2] > 240


def test_all_480_samples_are_preserved_in_order_across_pages(tmp_path):
    samples = [
        (f"asset-{index}", index / 10, f"scene-{index} | middle | {index / 10:.2f}s")
        for index in range(480)
    ]
    with patch.object(
        contact_sheets,
        "extract_thumbnail",
        return_value=Image.new("RGB", (108, 192), "blue"),
    ) as extract:
        first = contact_sheets._sheet(samples, tmp_path / "large.jpg")
    assert [call.args for call in extract.call_args_list] == [
        (path, timestamp) for path, timestamp, _ in samples
    ]
    metadata = json.loads((tmp_path / "large.pages.json").read_text())
    assert metadata["sample_count"] == 480
    assert [label for page in metadata["pages"] for label in page["labels"]] == [
        sample[2] for sample in samples
    ]
    assert [page["sample_start"] for page in metadata["pages"]] == list(
        range(0, 480, 9)
    )
    pages = contact_sheets.contact_sheet_pages(first)
    assert len(pages) == 54
    for path in pages:
        with Image.open(path) as page:
            assert page.width <= 1440 and page.height <= 3000


def test_new_manifest_excludes_old_generation_and_unrelated_pages(tmp_path):
    with patch.object(
        contact_sheets,
        "extract_thumbnail",
        return_value=Image.new("RGB", (108, 192), "red"),
    ):
        first = contact_sheets._sheet(
            [("asset", 0, str(index)) for index in range(12)], tmp_path / "sheet.jpg"
        )
        old_pages = contact_sheets.contact_sheet_pages(first)
        contact_sheets._sheet([("asset", 0, "replacement")], tmp_path / "sheet.jpg")
    (tmp_path / "sheet-unrelated-page.jpg").write_bytes(b"unrelated")
    current_pages = contact_sheets.contact_sheet_pages(first)
    assert len(current_pages) == 1
    assert set(current_pages).isdisjoint(old_pages)
    assert all(Path(path).is_file() for path in old_pages)


def test_missing_or_invalid_manifest_does_not_silently_lose_pages(tmp_path):
    first = tmp_path / "sheet.jpg"
    assert contact_sheets.contact_sheet_pages(first) == [str(first)]
    with patch.object(
        contact_sheets,
        "extract_thumbnail",
        return_value=Image.new("RGB", (108, 192), "red"),
    ):
        contact_sheets._sheet([("asset", 0, "scene")], first)
    page = contact_sheets.contact_sheet_pages(first)[0]
    Path(page).unlink()
    with pytest.raises(ValueError, match="complete contact sheet evidence"):
        contact_sheets.contact_sheet_pages(first)
    manifest = tmp_path / "sheet.pages.json"
    metadata = json.loads(manifest.read_text())
    metadata["pages"][0]["file"] = "../outside.jpg"
    manifest.write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="complete contact sheet evidence"):
        contact_sheets.contact_sheet_pages(first)


def test_failed_replacement_keeps_previous_complete_generation(tmp_path):
    first = tmp_path / "sheet.jpg"
    with patch.object(
        contact_sheets,
        "extract_thumbnail",
        return_value=Image.new("RGB", (108, 192), "red"),
    ):
        contact_sheets._sheet([("asset", 0, "original")], first)
        original = first.read_bytes()
        original_pages = contact_sheets.contact_sheet_pages(first)
        with (
            patch.object(
                contact_sheets, "_save_page", side_effect=OSError("disk full")
            ),
            pytest.raises(OSError),
        ):
            contact_sheets._sheet([("asset", 0, "replacement")], first)
    assert first.read_bytes() == original
    assert contact_sheets.contact_sheet_pages(first) == original_pages


def test_failed_manifest_publication_restores_previous_preview_and_pages(tmp_path):
    first = tmp_path / "sheet.jpg"
    replace = contact_sheets.os.replace

    def fail_manifest(source, destination):
        if str(destination).endswith(".pages.json"):
            raise OSError("manifest write failed")
        return replace(source, destination)

    with patch.object(
        contact_sheets,
        "extract_thumbnail",
        return_value=Image.new("RGB", (108, 192), "red"),
    ):
        contact_sheets._sheet([("asset", 0, "original")], first)
        original = first.read_bytes()
        original_pages = contact_sheets.contact_sheet_pages(first)
        with (
            patch.object(contact_sheets.os, "replace", side_effect=fail_manifest),
            pytest.raises(OSError, match="manifest write failed"),
        ):
            contact_sheets._sheet([("asset", 0, "replacement")], first)
    assert first.read_bytes() == original
    assert contact_sheets.contact_sheet_pages(first) == original_pages
    assert sorted(tmp_path.glob("sheet-*-page-*.jpg")) == [
        Path(path) for path in original_pages
    ]


def test_evidence_over_limit_is_rejected_before_decoding(tmp_path):
    with (
        patch.object(contact_sheets, "extract_thumbnail") as extract,
        pytest.raises(ValueError, match="480"),
    ):
        contact_sheets._sheet([("asset", 0, "scene")] * 481, tmp_path / "sheet.jpg")
    extract.assert_not_called()


def test_remote_input_is_never_downloaded():
    with patch.object(contact_sheets.subprocess, "run") as run:
        with pytest.raises((OSError, ValueError)):
            contact_sheets.extract_thumbnail("https://example.org/private.mp4")
        run.assert_not_called()


@pytest.mark.parametrize("duration", [0, -1, float("nan"), float("inf")])
def test_bad_timeline_is_rejected(duration):
    with pytest.raises(ValueError):
        contact_sheets.scene_timeline(plan_fixture(), duration)


def test_real_local_scene_encoding_caching_and_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(VideoAspect, "to_resolution", lambda self: (160, 90))
    monkeypatch.setattr(video, "fps", 10)
    plan = plan_fixture()
    plan.scenes[0].target_duration = 0.4
    plan.scenes[1].target_duration = 0.6
    materials = []
    for scene, color in zip(plan.scenes, ("red", "blue")):
        source = tmp_path / f"{scene.scene_id}.png"
        Image.new("RGB", (160, 90), color).save(source)
        materials.append(
            SceneMaterial(
                scene_id=scene.scene_id,
                paths=[str(source)],
                source="local",
                visual_type="local_asset",
                target_duration=scene.target_duration,
            )
        )
    params = VideoParams(
        video_subject="Colors", video_source="local", bgm_type="", n_threads=1
    )
    timeline = contact_sheets.scene_timeline(plan)
    paths = execution.prepare_scene_clips(materials, tmp_path, params, timeline, plan)
    red = contact_sheets.extract_thumbnail(paths[0], 0.1)
    blue = contact_sheets.extract_thumbnail(paths[1], 0.1)
    assert red.getpixel((30, 30))[0] > 200
    assert blue.getpixel((30, 30))[2] > 200
    mtimes = [Path(p).stat().st_mtime_ns for p in paths]
    assert (
        execution.prepare_scene_clips(materials, tmp_path, params, timeline, plan)
        == paths
    )
    assert [Path(p).stat().st_mtime_ns for p in paths] == mtimes
    Image.new("RGB", (160, 90), "green").save(materials[1].paths[0])
    repaired = execution.prepare_scene_clips(
        materials, tmp_path, params, timeline, plan
    )
    assert repaired[0] == paths[0]
    assert repaired[1] != paths[1]
    assert Path(paths[1]).is_file()  # Previous usable material is preserved.


def test_text_overlay_and_transition_validation(tmp_path):
    overlay = execution._text_overlay(
        "A short scene title", (640, 360), "BeVietnamPro-Bold.ttf"
    )
    assert overlay.shape == (360, 640, 4)
    assert overlay[:, :, 3].max() == 255
    with pytest.raises(ValueError, match="too long"):
        execution._text_overlay("word " * 1000, (640, 360), "BeVietnamPro-Bold.ttf")
