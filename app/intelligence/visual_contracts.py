"""Closed, declarative inputs for the local visual renderer.

These contracts contain content and data only. No renderer accepts executable
code, markup, URLs, or a model-selected filesystem path.
"""

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


BUILTIN_VISUAL_TYPES = frozenset(
    {"diagram", "chart", "text_card", "icon_composition", "screenshot"}
)
IconName = Literal[
    "check",
    "clock",
    "cloud",
    "document",
    "heart",
    "lightbulb",
    "lock",
    "person",
    "play",
    "search",
    "star",
    "trend",
    "warning",
    "globe",
]


class VisualContract(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class DiagramNode(VisualContract):
    id: str = Field(min_length=1, max_length=32, pattern=r"^[a-zA-Z0-9_-]+$")
    label: str = Field(min_length=1, max_length=100)


class DiagramEdge(VisualContract):
    source: str = Field(min_length=1, max_length=32)
    target: str = Field(min_length=1, max_length=32)
    label: str = Field(default="", max_length=40)


class DiagramSpec(VisualContract):
    nodes: list[DiagramNode] = Field(min_length=1, max_length=8)
    edges: list[DiagramEdge] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def valid_edges(self):
        ids = [node.id for node in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError("Diagram node IDs must be unique")
        for edge in self.edges:
            if edge.source not in ids or edge.target not in ids:
                raise ValueError("Diagram edges must reference existing node IDs")
            if edge.source == edge.target:
                raise ValueError("Diagram self-edges are not supported")
        pairs = [(edge.source, edge.target) for edge in self.edges]
        if len(pairs) != len(set(pairs)):
            raise ValueError("Diagram edges must not be duplicated")
        return self


class ChartSpec(VisualContract):
    chart_type: Literal["bar", "line", "pie"] = "bar"
    labels: list[str] = Field(min_length=1, max_length=8)
    values: list[float] = Field(min_length=1, max_length=8)
    source: str = Field(min_length=1, max_length=220)
    unit: str = Field(default="", max_length=24)

    @model_validator(mode="after")
    def valid_data(self):
        if len(self.labels) != len(self.values):
            raise ValueError("Chart labels and values must have equal lengths")
        if any(not label.strip() or len(label) > 64 for label in self.labels):
            raise ValueError("Chart labels must contain 1 to 64 characters")
        if not self.source.strip():
            raise ValueError("Charts require a source description")
        if any(not math.isfinite(value) or abs(value) > 1e15 for value in self.values):
            raise ValueError("Chart values must be finite and within +/-1e15")
        if self.chart_type == "pie" and (
            any(value < 0 for value in self.values) or sum(self.values) <= 0
        ):
            raise ValueError("Pie values must be nonnegative with a positive total")
        return self


class IconItem(VisualContract):
    icon: IconName
    label: str = Field(min_length=1, max_length=100)


class BuiltinVisualSpec(VisualContract):
    title: str = Field(default="", max_length=140)
    body: str = Field(default="", max_length=600)
    diagram: DiagramSpec | None = None
    chart: ChartSpec | None = None
    icons: list[IconItem] = Field(default_factory=list, max_length=6)
    screenshot_index: int | None = Field(default=None, ge=0, strict=True)

    def validate_for(self, visual_type: str) -> "BuiltinVisualSpec":
        """Validate a spec against its scene's visual type without changing it."""
        kind = getattr(visual_type, "value", visual_type)
        if kind not in BUILTIN_VISUAL_TYPES:
            raise ValueError(f"Unsupported built-in visual type: {kind}")
        present = {
            "diagram": self.diagram is not None,
            "chart": self.chart is not None,
            "icon_composition": bool(self.icons),
            "screenshot": self.screenshot_index is not None,
        }
        if kind == "text_card":
            if not (self.title.strip() or self.body.strip()):
                raise ValueError("Text cards need a title or body")
        elif not present[kind]:
            raise ValueError(f"{kind} requires its structured visual content")
        if any(exists for key, exists in present.items() if key != kind):
            raise ValueError("Visual spec contains content for a different visual type")
        return self
