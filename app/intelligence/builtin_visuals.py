"""Deterministic, offline scene visuals from validated declarative content."""

import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from app.intelligence.visual_contracts import BuiltinVisualSpec
from app.utils import utils


_VERSION = 2
_BG = "#0b1422"
_PANEL = "#15263a"
_TEXT = "#f4f7fc"
_MUTED = "#aabbd0"
_GRID = "#34465a"
_COLORS = [
    "#5bd6ba",
    "#78a9ff",
    "#ffc77c",
    "#d6a0ed",
    "#fb9298",
    "#91d1ed",
    "#cede89",
    "#eeb486",
]


def _font_path(font_name: str) -> Path:
    """Resolve the user's selected font, falling back only when none was set."""
    name = font_name or "STHeitiMedium.ttc"
    # font_dir(subdir) creates a directory if absent. Resolve a filename under
    # the font root without creating a bogus .ttf directory for a typo.
    path = (Path(utils.font_dir()) / name).resolve()
    if not path.is_file():
        raise ValueError(f"Visual font does not exist: {name}")
    return path


def _lines(draw, text, font, width):
    """Wrap text at words where possible, including scripts without spaces."""
    result = []
    for paragraph in text.split("\n"):
        if not paragraph:
            result.append("")
            continue
        line = ""
        for token in re.findall(r"\S+\s*|\s+", paragraph):
            if line and draw.textlength(line + token, font=font) > width:
                result.append(line.rstrip())
                line = ""
            for char in token:
                if line and draw.textlength(line + char, font=font) > width:
                    result.append(line.rstrip())
                    line = ""
                line += char
        if line.strip():
            result.append(line.rstrip())
    return result or [""]


def _text(
    draw,
    text,
    box,
    font_path,
    size,
    fill=_TEXT,
    align="left",
    minimum=None,
    vertical_center=False,
):
    if not text:
        return 0
    x0, y0, x1, y1 = box
    width, height = x1 - x0, y1 - y0
    size = max(8, int(size))
    minimum = min(size, max(8, int(minimum or size * 0.52)))
    for point in range(size, minimum - 1, -1):
        font = ImageFont.truetype(str(font_path), point)
        lines = _lines(draw, text, font, width)
        line_h = math.ceil(point * 1.38)
        if len(lines) * line_h <= height:
            break
    else:
        raise ValueError("Visual text does not fit; shorten the title or labels")
    y = y0 + (height - len(lines) * line_h) / 2 if vertical_center else y0
    for line in lines:
        x = (
            x0 + (width - draw.textlength(line, font=font)) / 2
            if align == "center"
            else x0
        )
        draw.text((x, y), line, font=font, fill=fill, anchor="lt")
        y += line_h
    return len(lines) * line_h


def _number(value):
    # Keep meaningful precision in labels; bar/line geometry uses original data.
    if abs(value) >= 1e9 or (value != 0 and abs(value) < 0.001):
        return f"{value:.5g}"
    return f"{value:,.6f}".rstrip("0").rstrip(".")


def _frame(size, spec, font_path, kind):
    width, height = size
    scale = min(size)
    image = Image.new("RGB", size, _BG)
    draw = ImageDraw.Draw(image)
    margin = int(width * 0.085)
    draw.rounded_rectangle(
        (
            margin,
            height * 0.075,
            margin + scale * 0.075,
            height * 0.075 + scale * 0.008,
        ),
        radius=3,
        fill=_COLORS[0],
    )
    top = height * 0.10
    if spec.title and kind != "text_card":
        used = _text(
            draw,
            spec.title,
            (margin, top, width - margin, height * 0.25),
            font_path,
            scale * 0.057,
        )
        top += used + scale * 0.025
    if spec.body and kind != "text_card":
        used = _text(
            draw,
            spec.body,
            (margin, top, width - margin, min(height * 0.38, top + height * 0.16)),
            font_path,
            scale * 0.03,
            _MUTED,
        )
        top += used + scale * 0.025
    return image, draw, (margin, max(top, height * 0.19), width - margin, height * 0.82)


def _text_card(draw, spec, bounds, font_path, size):
    _, height = size
    x0, _, x1, _ = bounds
    if spec.title:
        _text(
            draw,
            spec.title,
            (
                x0,
                height * (0.22 if spec.body else 0.29),
                x1,
                height * (0.46 if spec.body else 0.70),
            ),
            font_path,
            min(size) * 0.095,
            align="center",
            vertical_center=True,
        )
    if spec.body:
        _text(
            draw,
            spec.body,
            (x0, height * (0.47 if spec.title else 0.27), x1, height * 0.77),
            font_path,
            min(size) * 0.064,
            _MUTED,
            align="center",
            minimum=min(size) * 0.022,
            vertical_center=True,
        )


def _arrow(draw, start, end, color, width):
    draw.line((start, end), fill=color, width=width)
    angle = math.atan2(end[1] - start[1], end[0] - start[0])
    length = width * 4.5
    points = [end]
    for offset in (-0.48, 0.48):
        points.append(
            (
                end[0] - length * math.cos(angle + offset),
                end[1] - length * math.sin(angle + offset),
            )
        )
    draw.polygon(points, fill=color)


def _edge_boundary(center, towards, box_width, box_height):
    dx, dy = towards[0] - center[0], towards[1] - center[1]
    factors = []
    if dx:
        factors.append(box_width / (2 * abs(dx)))
    if dy:
        factors.append(box_height / (2 * abs(dy)))
    t = min(factors)
    return center[0] + dx * t, center[1] + dy * t


def _diagram(draw, spec, bounds, font_path, size):
    diagram = spec.diagram
    x0, y0, x1, y1 = bounds
    width, height = x1 - x0, y1 - y0
    n = len(diagram.nodes)
    scale = min(size)
    centers = {}
    node_ids = [node.id for node in diagram.nodes]
    expected_chain = set(zip(node_ids, node_ids[1:]))
    actual_edges = {(edge.source, edge.target) for edge in diagram.edges}
    chain = n <= 5 and expected_chain == actual_edges
    portrait = size[1] > size[0]
    if chain or n <= 2:
        if portrait:
            box_w = width * 0.84
            box_h = min(height / max(1, 2 * n - 1) * 0.90, scale * 0.24)
        else:
            box_w = width * 0.82 / n
            box_h = min(height * 0.65, scale * 0.26)
        for index, node in enumerate(diagram.nodes):
            centers[node.id] = (
                ((x0 + x1) / 2, y0 + height * (index + 0.5) / n)
                if portrait
                else (x0 + width * (index + 0.5) / n, (y0 + y1) / 2)
            )
    else:
        # Place vertices around the perimeter so arrows terminate at visible
        # borders instead of disappearing behind intervening node cards.
        box_w = width * (0.32 if n <= 4 else 0.27 if n <= 6 else 0.22)
        box_h = min(height * (0.19 if n <= 4 else 0.13), scale * 0.17)
        for index, node in enumerate(diagram.nodes):
            angle = (
                -math.pi / 2 + 2 * math.pi * index / n + (math.pi / n if n >= 7 else 0)
            )
            centers[node.id] = (
                (x0 + x1) / 2 + (width - box_w) * 0.49 * math.cos(angle),
                (y0 + y1) / 2 + (height - box_h) * 0.46 * math.sin(angle),
            )
    edge_labels = []
    for edge in diagram.edges:
        source, target = centers[edge.source], centers[edge.target]
        start = _edge_boundary(
            source, target, box_w + scale * 0.009, box_h + scale * 0.009
        )
        end = _edge_boundary(
            target, source, box_w + scale * 0.009, box_h + scale * 0.009
        )
        _arrow(draw, start, end, _MUTED, max(2, int(scale * 0.005)))
        if edge.label:
            x, y = (start[0] + end[0]) / 2, (start[1] + end[1]) / 2
            if chain:
                if portrait:
                    x += scale * 0.19
                else:
                    y -= scale * 0.075
            else:
                # Bring captions inside the ring, clear of the node borders.
                vx, vy = (x0 + x1) / 2 - x, (y0 + y1) / 2 - y
                distance = math.hypot(vx, vy)
                if distance:
                    x += vx / distance * scale * 0.075
                    y += vy / distance * scale * 0.075
            edge_labels.append((edge.label, x, y))
    for label, x, y in edge_labels:
        half_w, half_h = scale * 0.14, scale * 0.043
        draw.rounded_rectangle(
            (x - half_w, y - half_h, x + half_w, y + half_h), radius=5, fill=_BG
        )
        _text(
            draw,
            label,
            (x - half_w, y - half_h, x + half_w, y + half_h),
            font_path,
            scale * 0.029,
            _MUTED,
            "center",
            vertical_center=True,
        )
    for index, node in enumerate(diagram.nodes):
        x, y = centers[node.id]
        box = (x - box_w / 2, y - box_h / 2, x + box_w / 2, y + box_h / 2)
        draw.rounded_rectangle(
            box,
            radius=int(scale * 0.017),
            fill=_PANEL,
            outline=_COLORS[index % len(_COLORS)],
            width=max(2, int(scale * 0.003)),
        )
        pad = scale * 0.014
        _text(
            draw,
            node.label,
            (box[0] + pad, box[1] + pad, box[2] - pad, box[3] - pad),
            font_path,
            scale * (0.058 if chain or n <= 2 else 0.037),
            align="center",
            vertical_center=True,
        )


def _chart(draw, spec, bounds, font_path, size):
    chart = spec.chart
    x0, y0, x1, y1 = bounds
    width = x1 - x0
    scale = min(size)
    footer_height = scale * 0.12
    source = f"Source: {chart.source}"
    if chart.unit:
        source = f"Unit: {chart.unit}  |  {source}"
    _text(
        draw,
        source,
        (x0, y1 - footer_height + scale * 0.015, x1, y1),
        font_path,
        scale * 0.021,
        _MUTED,
    )
    y1 -= footer_height
    if chart.chart_type == "pie":
        pie_size = min(width * 0.78, (y1 - y0) * 0.51)
        left = (x0 + x1 - pie_size) / 2
        pie_box = (left, y0, left + pie_size, y0 + pie_size)
        total = sum(chart.values)
        start = -90
        for index, value in enumerate(chart.values):
            end = start + value / total * 360
            if value > 0:
                draw.pieslice(
                    pie_box,
                    start,
                    end,
                    fill=_COLORS[index],
                    outline=_BG,
                    width=max(1, int(scale * 0.004)),
                )
            start = end
        legend_y = y0 + pie_size + scale * 0.025
        row_h = (y1 - legend_y) / len(chart.labels)
        for index, (label, value) in enumerate(zip(chart.labels, chart.values)):
            top = legend_y + row_h * index
            marker = min(scale * 0.016, row_h * 0.5)
            draw.rounded_rectangle(
                (x0, top + marker / 2, x0 + marker, top + marker * 1.5),
                radius=2,
                fill=_COLORS[index],
            )
            _text(
                draw,
                f"{label}  {_number(value)}  ({value / total:.1%})",
                (x0 + marker * 2, top, x1, top + row_h),
                font_path,
                min(scale * 0.026, row_h * 0.62),
            )
        return
    # Both Cartesian chart types include zero. Negative values retain their sign
    # and extend below the zero baseline; no truncated-axis exaggeration.
    plot_x0 = x0 + width * 0.18
    plot_x1 = x1 - width * 0.025
    plot_y0 = y0 + scale * 0.065
    plot_y1 = y1 - scale * 0.13
    low, high = min(0, min(chart.values)), max(0, max(chart.values))
    if low == high:
        high = 1
    span = high - low
    low = low - span * 0.12 if low < 0 else low
    high = high + span * 0.12 if high > 0 else high

    def y_for(value):
        return plot_y1 - (value - low) / (high - low) * (plot_y1 - plot_y0)

    for tick in range(5):
        value = low + (high - low) * tick / 4
        y = y_for(value)
        draw.line(
            (plot_x0, y, plot_x1, y), fill=_GRID, width=max(1, int(scale * 0.001))
        )
        _text(
            draw,
            _number(value),
            (x0, y - scale * 0.012, plot_x0 - scale * 0.017, y + scale * 0.022),
            font_path,
            scale * 0.021,
            _MUTED,
        )
    draw.line(
        (plot_x0, y_for(0), plot_x1, y_for(0)),
        fill=_MUTED,
        width=max(1, int(scale * 0.002)),
    )
    slot = (plot_x1 - plot_x0) / len(chart.labels)
    points = [
        (plot_x0 + slot * (index + 0.5), y_for(value))
        for index, value in enumerate(chart.values)
    ]
    if chart.chart_type == "line" and len(points) > 1:
        draw.line(points, fill=_COLORS[0], width=max(2, int(scale * 0.005)))
    for index, (label, value) in enumerate(zip(chart.labels, chart.values)):
        x, y = points[index]
        if chart.chart_type == "bar":
            bar_w = slot * 0.61
            draw.rectangle(
                (x - bar_w / 2, min(y, y_for(0)), x + bar_w / 2, max(y, y_for(0))),
                fill=_COLORS[index],
            )
        else:
            r = scale * 0.007
            draw.ellipse(
                (x - r, y - r, x + r, y + r), fill=_COLORS[0], outline=_BG, width=1
            )
        # Values are printed directly so precise source data survives axis rounding.
        label_y = y - scale * 0.04 if value >= 0 else y + scale * 0.009
        _text(
            draw,
            _number(value),
            (x - slot * 0.49, label_y, x + slot * 0.49, label_y + scale * 0.03),
            font_path,
            scale * 0.021,
            align="center",
            minimum=9,
        )
        _text(
            draw,
            label,
            (x - slot * 0.47, plot_y1 + scale * 0.017, x + slot * 0.47, y1),
            font_path,
            scale * 0.025,
            _MUTED,
            "center",
            minimum=max(8, int(scale * 0.013)),
        )


def _icon(draw, name, box, color, stroke):
    """Draw an allowlisted vector symbol, never font-dependent emoji."""
    x0, y0, x1, y1 = box
    width, height = x1 - x0, y1 - y0

    def p(x, y):
        return x0 + x * width, y0 + y * height

    def line(points):
        draw.line([p(x, y) for x, y in points], fill=color, width=stroke, joint="curve")

    def ellipse(coords):
        a, b, c, d = coords
        draw.ellipse((*p(a, b), *p(c, d)), outline=color, width=stroke)

    if name == "check":
        line([(0.13, 0.52), (0.4, 0.8), (0.88, 0.2)])
    elif name == "clock":
        ellipse((0.08, 0.08, 0.92, 0.92))
        line([(0.5, 0.25), (0.5, 0.5), (0.72, 0.62)])
    elif name == "cloud":
        line(
            [
                (0.2, 0.75),
                (0.1, 0.7),
                (0.05, 0.56),
                (0.12, 0.4),
                (0.3, 0.36),
                (0.36, 0.18),
                (0.54, 0.13),
                (0.73, 0.24),
                (0.77, 0.4),
                (0.92, 0.48),
                (0.94, 0.65),
                (0.82, 0.75),
                (0.2, 0.75),
            ]
        )
    elif name == "document":
        line(
            [
                (0.2, 0.08),
                (0.63, 0.08),
                (0.82, 0.28),
                (0.82, 0.92),
                (0.2, 0.92),
                (0.2, 0.08),
            ]
        )
        line([(0.63, 0.08), (0.63, 0.28), (0.82, 0.28)])
        line([(0.33, 0.48), (0.69, 0.48)])
        line([(0.33, 0.65), (0.69, 0.65)])
    elif name == "heart":
        line(
            [
                (0.5, 0.9),
                (0.09, 0.48),
                (0.08, 0.28),
                (0.21, 0.12),
                (0.39, 0.12),
                (0.5, 0.26),
                (0.61, 0.12),
                (0.79, 0.12),
                (0.92, 0.28),
                (0.91, 0.48),
                (0.5, 0.9),
            ]
        )
    elif name == "lightbulb":
        ellipse((0.22, 0.05, 0.78, 0.66))
        line([(0.36, 0.63), (0.36, 0.79), (0.64, 0.79), (0.64, 0.63)])
        line([(0.39, 0.92), (0.61, 0.92)])
    elif name == "lock":
        draw.arc((*p(0.25, 0.05), *p(0.75, 0.65)), 180, 360, fill=color, width=stroke)
        draw.rounded_rectangle(
            (*p(0.15, 0.4), *p(0.85, 0.94)), radius=stroke, outline=color, width=stroke
        )
        line([(0.5, 0.57), (0.5, 0.77)])
    elif name == "person":
        ellipse((0.32, 0.04, 0.68, 0.4))
        draw.arc((*p(0.1, 0.47), *p(0.9, 1.27)), 180, 360, fill=color, width=stroke)
        line([(0.1, 0.87), (0.1, 0.94), (0.9, 0.94), (0.9, 0.87)])
    elif name == "play":
        line([(0.25, 0.08), (0.87, 0.5), (0.25, 0.92), (0.25, 0.08)])
    elif name == "search":
        ellipse((0.07, 0.07, 0.7, 0.7))
        line([(0.62, 0.62), (0.94, 0.94)])
    elif name == "star":
        points = []
        for index in range(10):
            angle = -math.pi / 2 + index * math.pi / 5
            radius = 0.46 if index % 2 == 0 else 0.2
            points.append(
                (0.5 + radius * math.cos(angle), 0.5 + radius * math.sin(angle))
            )
        line(points + [points[0]])
    elif name == "trend":
        line([(0.08, 0.85), (0.38, 0.54), (0.57, 0.66), (0.91, 0.19)])
        line([(0.61, 0.19), (0.91, 0.19), (0.91, 0.5)])
    elif name == "warning":
        line([(0.5, 0.04), (0.97, 0.92), (0.03, 0.92), (0.5, 0.04)])
        line([(0.5, 0.35), (0.5, 0.61)])
        ellipse((0.48, 0.73, 0.52, 0.77))
    elif name == "globe":
        ellipse((0.06, 0.06, 0.94, 0.94))
        ellipse((0.28, 0.06, 0.72, 0.94))
        line([(0.06, 0.5), (0.94, 0.5)])
        line([(0.17, 0.24), (0.83, 0.24)])
        line([(0.17, 0.76), (0.83, 0.76)])
    else:
        raise ValueError(f"Unsupported icon: {name}")


def _icons(draw, spec, bounds, font_path, size):
    x0, y0, x1, y1 = bounds
    scale = min(size)
    count = len(spec.icons)
    columns = (1 if count <= 3 else 2) if size[1] > size[0] else min(count, 3)
    rows = math.ceil(count / columns)
    gap = scale * 0.025
    cell_w = (x1 - x0 - gap * (columns - 1)) / columns
    cell_h = (y1 - y0 - gap * (rows - 1)) / rows
    for index, item in enumerate(spec.icons):
        x = x0 + index % columns * (cell_w + gap)
        y = y0 + index // columns * (cell_h + gap)
        draw.rounded_rectangle(
            (x, y, x + cell_w, y + cell_h), radius=int(scale * 0.02), fill=_PANEL
        )
        icon_size = min(cell_h * 0.4, cell_w * 0.3, scale * 0.13)
        icon_x = x + (cell_w - icon_size) / 2
        icon_y = y + cell_h * 0.12
        _icon(
            draw,
            item.icon,
            (icon_x, icon_y, icon_x + icon_size, icon_y + icon_size),
            _COLORS[index],
            max(2, int(icon_size * 0.065)),
        )
        _text(
            draw,
            item.label,
            (x + gap, icon_y + icon_size + gap, x + cell_w - gap, y + cell_h - gap),
            font_path,
            scale * 0.032,
            align="center",
        )


def _screenshot(image, draw, screenshot_path, bounds, font_path, size):
    x0, y0, x1, y1 = map(int, bounds)
    scale = min(size)
    chrome = int(scale * 0.04)
    with Image.open(screenshot_path) as opened:
        source = ImageOps.exif_transpose(opened).convert("RGBA")
        source.thumbnail(
            (x1 - x0 - chrome, y1 - y0 - chrome * 2), Image.Resampling.LANCZOS
        )
    left = (x0 + x1 - source.width) // 2
    top = (y0 + y1 - source.height) // 2
    border = int(scale * 0.012)
    draw.rounded_rectangle(
        (
            left - border,
            top - chrome,
            left + source.width + border,
            top + source.height + border,
        ),
        radius=border,
        fill=_PANEL,
        outline=_GRID,
        width=1,
    )
    for index, color in enumerate(["#fb9298", "#ffc77c", "#5bd6ba"]):
        r = max(2, int(scale * 0.004))
        x = left + border + index * r * 4
        y = top - chrome / 2
        draw.ellipse((x - r, y - r, x + r, y + r), fill=color)
    image.paste(source, (left, top), source)


def render_builtin_visual(
    scene_id: str,
    visual_type: str,
    spec: BuiltinVisualSpec | dict,
    output_dir: str | Path,
    size: tuple[int, int],
    font_name: str,
    screenshot_paths=(),
) -> str:
    """Render one PNG atomically, reusing identical content from its cache.

    Screenshot paths come from the caller's user-supplied image list; the model
    can only choose an index. No remote media or external generation is used.
    """
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", scene_id):
        raise ValueError("Invalid scene ID for visual output")
    if (
        len(size) != 2
        or any(type(value) is not int or not 320 <= value <= 3840 for value in size)
        or math.prod(size) > 16_000_000
    ):
        raise ValueError("Visual dimensions must be integers from 320 to 3840")
    kind = getattr(visual_type, "value", visual_type)
    # Round-trip revalidates nested content even if a list was mutated in place.
    spec = BuiltinVisualSpec.model_validate(
        spec.model_dump() if isinstance(spec, BuiltinVisualSpec) else spec
    )
    spec.validate_for(kind)
    font_path = _font_path(font_name)
    screenshot_path = None
    screenshot_digest = None
    if kind == "screenshot":
        if spec.screenshot_index >= len(screenshot_paths):
            raise ValueError("Screenshot index does not match a supplied image asset")
        screenshot_path = Path(screenshot_paths[spec.screenshot_index]).resolve()
        if not screenshot_path.is_file() or screenshot_path.suffix.lower() not in {
            ".jpg",
            ".jpeg",
            ".png",
            ".webp",
            ".bmp",
            ".tiff",
            ".tif",
        }:
            raise ValueError("Screenshot asset must be a supplied local image")
        with Image.open(screenshot_path) as source:
            source.verify()
        screenshot_digest = hashlib.sha256(screenshot_path.read_bytes()).hexdigest()
    fingerprint = {
        "version": _VERSION,
        "type": kind,
        "spec": spec.model_dump(),
        "size": list(size),
        "font": hashlib.sha256(font_path.read_bytes()).hexdigest(),
        "screenshot": screenshot_digest,
    }
    digest = hashlib.sha256(
        json.dumps(fingerprint, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:24]
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / f"{scene_id}-{kind}-{digest}.png"
    if target.is_file():
        try:
            with Image.open(target) as cached:
                if cached.size == tuple(size) and cached.format == "PNG":
                    cached.verify()
                    return str(target)
        except (OSError, SyntaxError):
            pass
    # Lay out low-resolution previews at a readable design scale before
    # downsampling, so fixed minimum font sizes do not break small outputs.
    factor = max(1, 720 / min(size))
    design_size = tuple(round(value * factor) for value in size)
    image, draw, bounds = _frame(design_size, spec, font_path, kind)
    if kind == "text_card":
        _text_card(draw, spec, bounds, font_path, design_size)
    elif kind == "diagram":
        _diagram(draw, spec, bounds, font_path, design_size)
    elif kind == "chart":
        _chart(draw, spec, bounds, font_path, design_size)
    elif kind == "icon_composition":
        _icons(draw, spec, bounds, font_path, design_size)
    else:
        _screenshot(image, draw, screenshot_path, bounds, font_path, design_size)
    if design_size != tuple(size):
        resized = image.resize(size, Image.Resampling.LANCZOS)
        image.close()
        image = resized
    handle, temporary = tempfile.mkstemp(
        prefix=f".{scene_id}-", suffix=".png", dir=destination
    )
    try:
        with os.fdopen(handle, "wb") as stream:
            image.save(stream, format="PNG", optimize=False)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
        image.close()
    return str(target)
