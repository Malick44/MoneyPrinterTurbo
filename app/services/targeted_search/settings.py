"""Lazy, non-secret targeted-search configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Settings:
    root_dir: Path = field(default_factory=lambda: storage_root())
    enabled: bool = True
    allowed_domains: tuple[str, ...] = (
        "youtube.com",
        "youtu.be",
        "krem.com",
        "khq.com",
        "kxly.com",
        "nbcnews.com",
        "nbc.com",
        "oxygen.com",
        "peacocktv.com",
        "tegna.kurator.com",
        "spokesman.com",
    )
    semantic_enabled: bool = False
    rerank_enabled: bool = False
    visual_enabled: bool = False
    ocr_enabled: bool = False
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_revision: str = "main"
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    reranker_revision: str = "main"
    vision_model: str = "ViT-B-32"
    vision_checkpoint: str = ""
    vision_pretrained: str = "laion2b_s34b_b79k"
    asr_model: str = "small"
    asr_revision: str = "main"
    whisperx_alignment_checkpoint: str = ""
    whisperx_language: str = "en"
    local_models_only: bool = True
    caption_languages: tuple[str, ...] = ("en",)
    job_lease_seconds: int = 300
    max_attempts: int = 3
    retry_delay_seconds: int = 15
    max_source_duration_ms: int = 14_400_000
    max_clip_duration_ms: int = 90_000
    max_download_bytes: int = 2_000_000_000
    command_timeout_seconds: int = 1800
    discovery_timeout_seconds: int = 180
    max_collection_sources: int = 500
    max_pending_jobs: int = 200
    visual_sample_seconds: int = 15
    max_visual_frames: int = 240
    max_storage_bytes: int = 20_000_000_000
    pipeline_version: str = "targeted-search-1"

    @property
    def reranker_enabled(self) -> bool:
        return self.rerank_enabled

    @classmethod
    def from_config(cls) -> "Settings":
        from app.config import config

        return cls.from_mapping(config.app)

    @classmethod
    def from_mapping(cls, values: dict[str, Any]) -> "Settings":
        defaults = cls()
        result: dict[str, Any] = {}
        for name in cls.__dataclass_fields__:
            if name == "root_dir":
                result[name] = storage_root()
                continue
            value = values.get(f"targeted_search_{name}", getattr(defaults, name))
            if name == "rerank_enabled":
                value = values.get("targeted_search_reranker_enabled", value)
            if (
                name == "max_source_duration_ms"
                and "targeted_search_max_source_duration_seconds" in values
            ):
                seconds = values["targeted_search_max_source_duration_seconds"]
                if (
                    isinstance(seconds, int)
                    and not isinstance(seconds, bool)
                    and seconds > 0
                ):
                    value = seconds * 1000
            if name in {"allowed_domains", "caption_languages"}:
                if isinstance(value, str):
                    value = value.split(",")
                if isinstance(value, (list, tuple)):
                    value = tuple(
                        str(item).strip().lower() for item in value if str(item).strip()
                    )
                else:
                    value = getattr(defaults, name)
            elif isinstance(getattr(defaults, name), bool):
                value = value if isinstance(value, bool) else getattr(defaults, name)
            elif isinstance(getattr(defaults, name), int):
                if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                    value = getattr(defaults, name)
            elif not isinstance(value, str) or (
                not value.strip() and getattr(defaults, name)
            ):
                value = getattr(defaults, name)
            result[name] = value
        return cls(**result)


def storage_root(root_dir: str | Path | None = None) -> Path:
    if root_dir is not None:
        return Path(root_dir).expanduser().resolve()
    from app.config import config
    from app.utils import utils

    configured = config.app.get(
        "targeted_search_storage_dir", config.app.get("targeted_search_directory", "")
    )
    selected = (
        Path(configured).expanduser()
        if configured
        else Path(utils.storage_dir("targeted_search"))
    )
    if not selected.is_absolute():
        selected = Path(utils.root_dir()) / selected
    return selected.resolve()
