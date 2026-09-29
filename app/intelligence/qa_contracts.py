"""Technical media evidence, separate from a model's semantic judgment."""

from typing import Any

from pydantic import Field

from app.intelligence.contracts import Contract, ReviewIssue


class MediaCoverage(Contract):
    full_file_decode: bool = False
    full_video_scan: bool = False
    full_audio_scan: bool = False
    narration_comparison: bool = False


class MediaInspection(Contract):
    passed: bool
    summary: str
    issues: list[ReviewIssue] = Field(default_factory=list)
    coverage: MediaCoverage = Field(default_factory=MediaCoverage)
    metrics: dict[str, Any] = Field(default_factory=dict)
    scope: str = (
        "Every frame of the primary video stream and every sample of the primary "
        "audio stream are decoded when coverage confirms completion. Black frames, "
        "frozen intervals, timing, silence, signal peaks and loudness are technical "
        "measurements. They do not establish semantic motion quality, spoken-word "
        "accuracy, intelligibility, or music suitability; the visual model separately "
        "reviews sampled frames. Narration comparison measures signal envelopes, "
        "not speech recognition."
    )
