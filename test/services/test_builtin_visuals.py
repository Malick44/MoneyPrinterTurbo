from pathlib import Path
from typing import get_args
from unittest.mock import patch

import pytest
from PIL import Image, ImageChops
from pydantic import ValidationError

from app.intelligence import builtin_visuals
from app.intelligence.builtin_visuals import render_builtin_visual
from app.intelligence.visual_contracts import (
    BuiltinVisualSpec,
    ChartSpec,
    DiagramSpec,
    IconName,
)


FONT = "BeVietnamPro-Medium.ttf"


def chart_spec(chart_type="bar", values=None):
    return {
        "title": "Weekly output",
        "chart": {
            "chart_type": chart_type,
            "labels": ["Monday", "Tuesday", "Wednesday"],
            "values": [12, 24, 8] if values is None else values,
            "source": "Illustrative example, not observed data",
            "unit": "videos",
        },
    }


def diagram_spec():
    return {
        "title": "A reviewed production",
        "diagram": {
            "nodes": [
                {"id": "brief", "label": "Production brief"},
                {"id": "plan", "label": "Scene plan"},
                {"id": "render", "label": "Local visuals"},
                {"id": "review", "label": "Review and repair"},
            ],
            "edges": [
                {"source": "brief", "target": "plan", "label": "plan"},
                {"source": "plan", "target": "render", "label": "render"},
                {"source": "render", "target": "review", "label": "review"},
                {"source": "review", "target": "plan", "label": "repair"},
            ],
        },
    }


@pytest.mark.parametrize("size", [(1080, 1920), (1920, 1080), (320, 568)])
@pytest.mark.parametrize(
    "kind,spec",
    [
        (
            "text_card",
            {
                "title": "Make the idea visible.",
                "body": "Clear plans. Useful visuals. Careful reviews.",
            },
        ),
        ("diagram", diagram_spec()),
        ("chart", chart_spec()),
        ("chart", chart_spec("line", [-12, 24, -8])),
        ("chart", chart_spec("pie")),
        (
            "icon_composition",
            {
                "title": "Production essentials",
                "icons": [
                    {"icon": "document", "label": "Plan your story"},
                    {"icon": "play", "label": "Render every scene"},
                    {"icon": "check", "label": "Review the result"},
                ],
            },
        ),
    ],
)
def test_real_visuals_render_at_supported_video_aspects(tmp_path, kind, spec, size):
    path = render_builtin_visual("scene", kind, spec, tmp_path, size, FONT)
    with Image.open(path) as image:
        assert image.size == size
        assert image.format == "PNG"
        # Ensure the output contains substantive content, not a blank fallback.
        difference = ImageChops.difference(
            image, Image.new("RGB", size, builtin_visuals._BG)
        )
        assert difference.getbbox() is not None
        assert (
            sum(1 for pixel in image.resize((64, 64)).getdata() if max(pixel) > 70) > 20
        )


def test_repeated_render_is_byte_identical_and_uses_cache(tmp_path):
    spec = BuiltinVisualSpec(
        title="A reusable visual", body="Content determines the cache key."
    )
    first = Path(
        render_builtin_visual("scene", "text_card", spec, tmp_path, (720, 1280), FONT)
    )
    before = first.stat().st_mtime_ns
    original = first.read_bytes()
    with patch.object(
        builtin_visuals, "_frame", side_effect=AssertionError("Unexpected rerender")
    ):
        second = render_builtin_visual(
            "scene", "text_card", spec, tmp_path, (720, 1280), FONT
        )
    assert str(first) == second
    assert first.stat().st_mtime_ns == before
    first.unlink()
    regenerated = Path(
        render_builtin_visual("scene", "text_card", spec, tmp_path, (720, 1280), FONT)
    )
    assert regenerated.read_bytes() == original
    assert not list(tmp_path.glob(".*.png"))


def test_content_and_font_changes_invalidate_cached_visual(tmp_path):
    first = render_builtin_visual(
        "scene", "text_card", {"title": "Original"}, tmp_path, (720, 1280), FONT
    )
    second = render_builtin_visual(
        "scene", "text_card", {"title": "Changed"}, tmp_path, (720, 1280), FONT
    )
    third = render_builtin_visual(
        "scene",
        "text_card",
        {"title": "Changed"},
        tmp_path,
        (720, 1280),
        "BeVietnamPro-Bold.ttf",
    )
    assert len({first, second, third}) == 3


def test_corrupt_cache_is_replaced_atomically(tmp_path):
    params = ("scene", "text_card", {"title": "Hello"}, tmp_path, (720, 1280), FONT)
    path = Path(render_builtin_visual(*params))
    path.write_bytes(b"incomplete PNG")
    assert render_builtin_visual(*params) == str(path)
    with Image.open(path) as image:
        image.verify()


def test_screenshot_preserves_entire_supplied_image_and_tracks_file_changes(tmp_path):
    screenshot = tmp_path / "supplied.png"
    source = Image.new("RGB", (400, 200), "red")
    for x in range(200, 400):
        for y in range(200):
            source.putpixel((x, y), (0, 0, 255))
    source.save(screenshot)
    args = (
        "scene",
        "screenshot",
        {"title": "Supplied screen", "screenshot_index": 0},
        tmp_path / "visuals",
        (720, 1280),
        FONT,
    )
    first = render_builtin_visual(*args, screenshot_paths=[screenshot])
    with Image.open(first) as image:
        colors = image.getcolors(image.width * image.height)
        counts = {color: count for count, color in colors}
        assert counts[(255, 0, 0)] == 40000
        assert counts[(0, 0, 255)] == 40000
    Image.new("RGB", (400, 200), "green").save(screenshot)
    second = render_builtin_visual(*args, screenshot_paths=[screenshot])
    assert first != second


@pytest.mark.parametrize(
    "index,paths",
    [
        (0, []),
        (1, ["unused.png"]),
        (0, ["https://example.org/image.png"]),
        (0, ["missing.png"]),
    ],
)
def test_screenshot_cannot_fetch_or_invent_an_asset_path(tmp_path, index, paths):
    with pytest.raises(ValueError, match="supplied"):
        render_builtin_visual(
            "scene",
            "screenshot",
            {"screenshot_index": index},
            tmp_path,
            (720, 1280),
            FONT,
            screenshot_paths=paths,
        )


def test_failed_atomic_write_leaves_no_partial_output(tmp_path):
    with patch.object(
        builtin_visuals.os, "replace", side_effect=OSError("disk unavailable")
    ):
        with pytest.raises(OSError, match="disk unavailable"):
            render_builtin_visual(
                "scene", "text_card", {"title": "Hello"}, tmp_path, (720, 1280), FONT
            )
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), 1e16])
def test_nonfinite_and_excessive_chart_values_are_rejected(value):
    with pytest.raises(ValidationError, match="finite"):
        ChartSpec(labels=["Bad value"], values=[value], source="Test data")


@pytest.mark.parametrize("values", [[-1, 2], [0, 0]])
def test_invalid_pie_totals_are_rejected(values):
    with pytest.raises(ValidationError, match="Pie values"):
        ChartSpec(
            chart_type="pie", labels=["A", "B"], values=values, source="Test data"
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"labels": ["A", "B"], "values": [1]},
        {"labels": [""], "values": [1]},
        {"labels": ["A"], "values": [1], "source": "   "},
    ],
)
def test_charts_require_complete_labeled_sourced_data(kwargs):
    data = {"source": "Test data", **kwargs}
    with pytest.raises(ValidationError):
        ChartSpec(**data)


@pytest.mark.parametrize(
    "edges",
    [
        [{"source": "a", "target": "missing"}],
        [{"source": "a", "target": "a"}],
        [{"source": "a", "target": "b"}, {"source": "a", "target": "b"}],
    ],
)
def test_invalid_diagram_edges_are_rejected(edges):
    with pytest.raises(ValidationError):
        DiagramSpec(
            nodes=[{"id": "a", "label": "A"}, {"id": "b", "label": "B"}], edges=edges
        )


def test_duplicate_diagram_nodes_are_rejected():
    with pytest.raises(ValidationError, match="unique"):
        DiagramSpec(nodes=[{"id": "a", "label": "A"}, {"id": "a", "label": "Also A"}])


@pytest.mark.parametrize(
    "spec",
    [
        {"title": "Hello", "html": "<script>doSomething()</script>"},
        {"screenshot_index": 0, "path": "/etc/passwd"},
        {"screenshot_index": True},
        {"icons": [{"icon": "javascript:run()", "label": "Bad icon"}]},
        {"diagram": {"nodes": [{"id": "a", "label": "A", "code": "run()"}]}},
    ],
)
def test_all_visual_inputs_are_closed_and_declarative(spec):
    with pytest.raises(ValidationError):
        BuiltinVisualSpec.model_validate(spec)


@pytest.mark.parametrize(
    "kind,spec",
    [
        ("text_card", {}),
        ("chart", {"title": "Missing data"}),
        ("diagram", {"title": "Missing graph"}),
        ("icon_composition", {}),
        ("screenshot", {}),
        ("text_card", {"title": "Wrong data", "screenshot_index": 0}),
        ("ai_video", {"title": "Unsupported"}),
    ],
)
def test_visual_types_require_only_their_own_content(kind, spec):
    with pytest.raises(ValueError):
        BuiltinVisualSpec.model_validate(spec).validate_for(kind)


def test_render_revalidates_nested_mutated_values(tmp_path):
    spec = BuiltinVisualSpec.model_validate(chart_spec())
    spec.chart.values[0] = float("nan")
    with pytest.raises(ValidationError):
        render_builtin_visual("scene", "chart", spec, tmp_path, (720, 1280), FONT)


@pytest.mark.parametrize("scene_id", ["../outside", "/absolute", "", "a" * 65])
def test_scene_id_cannot_escape_output_directory(tmp_path, scene_id):
    with pytest.raises(ValueError, match="scene ID"):
        render_builtin_visual(
            scene_id, "text_card", {"title": "Hello"}, tmp_path, (720, 1280), FONT
        )


@pytest.mark.parametrize(
    "size", [(0, 1080), (1080, 9000), (True, 720), (720.0, 1280), (720,)]
)
def test_invalid_output_sizes_are_rejected(tmp_path, size):
    with pytest.raises(ValueError, match="dimensions"):
        render_builtin_visual(
            "scene", "text_card", {"title": "Hello"}, tmp_path, size, FONT
        )


def test_selected_font_must_exist(tmp_path):
    with pytest.raises(ValueError, match="font"):
        render_builtin_visual(
            "scene",
            "text_card",
            {"title": "Hello"},
            tmp_path,
            (720, 1280),
            "nonexistent.ttf",
        )
    assert not (Path(builtin_visuals.utils.font_dir()) / "nonexistent.ttf").exists()


def test_all_allowlisted_icons_render_without_emoji_fonts(tmp_path):
    names = get_args(IconName)
    for start in range(0, len(names), 6):
        spec = {
            "title": "Visual symbols",
            "icons": [
                {"icon": name, "label": name.title()}
                for name in names[start : start + 6]
            ],
        }
        path = render_builtin_visual(
            f"icons{start}", "icon_composition", spec, tmp_path, (720, 1280), FONT
        )
        with Image.open(path) as image:
            image.verify()


def test_chart_bar_heights_preserve_data_ratios(tmp_path):
    path = render_builtin_visual(
        "scene", "chart", chart_spec(values=[10, 20, 30]), tmp_path, (1080, 1920), FONT
    )
    with Image.open(path) as image:
        heights = []
        for color in builtin_visuals._COLORS[:3]:
            rgb = tuple(bytes.fromhex(color[1:]))
            pixels = image.load()
            # Count exact fill pixels at the midpoint of each bar's colored area.
            matches = [
                (x, y)
                for y in range(360, 1920)
                for x in range(1080)
                if pixels[x, y] == rgb
            ]
            xs = [x for x, _ in matches]
            middle = (min(xs) + max(xs)) // 2
            ys = [y for x, y in matches if x == middle]
            heights.append(max(ys) - min(ys))
        assert heights[1] / heights[0] == pytest.approx(2, abs=0.02)
        assert heights[2] / heights[0] == pytest.approx(3, abs=0.02)


def test_short_portrait_flow_is_vertical_with_large_centered_labels(tmp_path):
    spec = {
        "title": "Plant to Bean",
        "diagram": {
            "nodes": [
                {"id": str(i), "label": label}
                for i, label in enumerate(["Plant", "Cherries", "Beans"])
            ],
            "edges": [{"source": "0", "target": "1"}, {"source": "1", "target": "2"}],
        },
    }
    with patch.object(
        builtin_visuals, "_text", wraps=builtin_visuals._text
    ) as draw_text:
        render_builtin_visual("flow", "diagram", spec, tmp_path, (1080, 1920), FONT)
    nodes = [
        call
        for call in draw_text.call_args_list
        if call.args[1] in {"Plant", "Cherries", "Beans"}
    ]
    centers = [
        (
            (call.args[2][0] + call.args[2][2]) / 2,
            (call.args[2][1] + call.args[2][3]) / 2,
        )
        for call in nodes
    ]
    assert [x for x, _ in centers] == [540, 540, 540]
    assert centers[0][1] < centers[1][1] < centers[2][1]
    assert all(call.args[4] >= 60 and call.kwargs["vertical_center"] for call in nodes)


def test_dense_diagram_node_cards_do_not_overlap(tmp_path):
    spec = {
        "title": "Eight connected stages",
        "diagram": {
            "nodes": [{"id": str(i), "label": f"Stage {i}"} for i in range(8)],
            "edges": [
                {"source": str(i), "target": str((i + 1) % 8), "label": "then"}
                for i in range(8)
            ],
        },
    }
    with patch.object(
        builtin_visuals, "_text", wraps=builtin_visuals._text
    ) as draw_text:
        render_builtin_visual("flow", "diagram", spec, tmp_path, (1080, 1920), FONT)
    padding = 1080 * 0.014
    node_boxes = [
        (
            call.args[2][0] - padding,
            call.args[2][1] - padding,
            call.args[2][2] + padding,
            call.args[2][3] + padding,
        )
        for call in draw_text.call_args_list
        if call.args[1].startswith("Stage ")
    ]
    for index, left in enumerate(node_boxes):
        for right in node_boxes[index + 1 :]:
            overlap = min(left[2], right[2]) > max(left[0], right[0]) and min(
                left[3], right[3]
            ) > max(left[1], right[1])
            assert not overlap
