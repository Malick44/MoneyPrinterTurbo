"""Exercise complete local decodes, including defects absent from sampled frames."""

import subprocess
import sys

import pytest

from app.intelligence import media_qa
from app.intelligence.contracts import ProductionPlan, ScenePlan
from app.utils import utils


def plan(visual_type="stock_video", duration=2.4):
    value = ProductionPlan(
        title="Inspection fixture",
        summary="A short technical fixture",
        scenes=[
            ScenePlan(
                scene_id="scene_1",
                narration="An audible test fixture.",
                purpose="Validate the entire file",
                target_duration=duration,
                visual_intent="A visible scene",
                preferred_visual_type=visual_type,
            )
        ],
    )
    return value, [{"scene_id": "scene_1", "start": 0, "end": duration}]


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    folder = tmp_path_factory.mktemp("media_qa")
    binary = utils.get_ffmpeg_binary()

    def make(
        name,
        video="testsrc2=size=160x90:rate=10:duration=2.4",
        audio="sine=frequency=440:sample_rate=48000:duration=2.4",
    ):
        target = folder / name
        command = [binary, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
        if video:
            command += ["-f", "lavfi", "-i", video]
        if audio:
            command += ["-f", "lavfi", "-i", audio]
        if video:
            command += [
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
            ]
        if audio:
            command += ["-c:a", "pcm_s16le" if target.suffix == ".wav" else "aac"]
        if target.suffix == ".mp4":
            command += ["-movflags", "+faststart"]
        subprocess.run(
            command + [str(target)], check=True, capture_output=True, timeout=20
        )
        return target

    moving = make("moving.mp4")
    frozen = make("frozen.mp4", "color=c=red:size=160x90:rate=10:duration=2.4")
    silent = make("silent.mp4", audio="anullsrc=r=48000:cl=stereo:d=2.4")
    no_audio = make("no-audio.mp4", audio=None)
    short_audio = make(
        "short-audio.mp4", audio="sine=frequency=440:sample_rate=48000:duration=0.7"
    )
    black = make("black.mp4", "color=c=black:size=160x90:rate=10:duration=2.4")
    # A defect late in the file: MP4 headers and opening frames remain readable.
    truncated = folder / "truncated.mp4"
    payload = moving.read_bytes()
    truncated.write_bytes(payload[: int(len(payload) * 0.65)])
    reference = make("reference.wav", video=None)
    silent_reference = make(
        "silent-reference.wav", video=None, audio="anullsrc=r=48000:cl=stereo:d=2.4"
    )
    clipped = make(
        "clipped.mp4", audio="aevalsrc=0.9999*sgn(sin(2*PI*440*t)):s=48000:d=2.4"
    )
    return dict(
        moving=moving,
        frozen=frozen,
        silent=silent,
        no_audio=no_audio,
        short_audio=short_audio,
        black=black,
        truncated=truncated,
        reference=reference,
        silent_reference=silent_reference,
        clipped=clipped,
    )


def categories(result):
    return {issue.category for issue in result.issues}


def test_full_video_and_audio_are_decoded_and_measured(media):
    result = media_qa.inspect_render(
        media["moving"], *plan(), narration_path=media["reference"]
    )
    assert result.passed, result.model_dump()
    assert result.coverage.full_file_decode
    assert result.coverage.full_video_scan and result.coverage.full_audio_scan
    assert result.coverage.narration_comparison
    assert result.metrics["video"]["frames"] == 24
    assert result.metrics["video"]["duration_seconds"] == pytest.approx(2.4, abs=0.11)
    assert result.metrics["audio"]["duration_seconds"] == pytest.approx(2.4, abs=0.04)
    assert result.metrics["audio"]["rms_dbfs"] > -30
    assert result.metrics["audio"]["integrated_loudness_lufs"] is not None
    assert "energy_windows" not in result.metrics["audio"]
    assert "NaN" not in result.model_dump_json()


def test_truncated_asset_never_passes_even_with_readable_opening(media):
    result = media_qa.inspect_render(media["truncated"], *plan())
    assert not result.passed
    assert categories(result) & {
        "video_decode",
        "video_timing",
        "audio_decode",
        "audio_timing",
    }


def test_expected_motion_freeze_is_scene_mapped(media):
    result = media_qa.inspect_render(media["frozen"], *plan())
    assert result.passed  # A deliberate still shot remains a review warning.
    assert "frozen_video" in categories(result)
    assert (
        next(i for i in result.issues if i.category == "frozen_video").scene_id
        == "scene_1"
    )
    assert result.metrics["freeze_intervals"][0]["end"] >= 2


def test_explicit_motion_expectation_distinguishes_local_movies_from_images(media):
    production, timeline = plan("local_asset")
    timeline[0]["expected_static"] = False
    result = media_qa.inspect_render(media["frozen"], production, timeline)
    assert "frozen_video" in categories(result)


@pytest.mark.parametrize(
    "kind",
    [
        "local_asset",
        "ai_image",
        "chart",
        "diagram",
        "text_card",
        "screenshot",
        "icon_composition",
    ],
)
def test_intentional_static_scene_keeps_evidence_without_false_rejection(media, kind):
    result = media_qa.inspect_render(media["frozen"], *plan(kind))
    assert result.passed
    assert "frozen_video" not in categories(result)
    assert result.metrics["freeze_intervals"][0]["expected_static"] is True


def test_black_frames_are_reported_without_rejecting_intentional_dark_designs(media):
    result = media_qa.inspect_render(media["black"], *plan("text_card"))
    assert result.passed
    assert "black_frames" in categories(result)
    assert result.metrics["black_intervals"][0]["scene_id"] == "scene_1"


def test_missing_audio_requires_expected_audio(media):
    result = media_qa.inspect_render(media["no_audio"], *plan())
    assert not result.passed
    assert "audio_missing" in categories(result)
    result = media_qa.inspect_render(media["no_audio"], *plan(), expected_audio=False)
    assert result.passed
    assert result.coverage.full_file_decode
    assert not result.coverage.full_audio_scan


def test_silence_is_allowed_for_no_voiceover_and_silent_reference(media):
    result = media_qa.inspect_render(media["silent"], *plan())
    assert not result.passed and "audio_silent" in categories(result)
    result = media_qa.inspect_render(media["silent"], *plan(), expected_audio=False)
    assert result.passed and result.metrics["silence_intervals"]
    assert result.metrics["audio"]["rms_dbfs"] is None
    result = media_qa.inspect_render(
        media["silent"], *plan(), narration_path=media["silent_reference"]
    )
    assert result.passed
    assert result.metrics["expected_audio"] is False
    assert result.metrics["narration_comparison"]["reference_is_silent"] is True


def test_audio_ending_early_is_detected_against_scene_timeline(media):
    result = media_qa.inspect_render(media["short_audio"], *plan())
    assert not result.passed
    assert "audio_timing" in categories(result)
    assert result.metrics["audio"]["duration_seconds"] < 1


def test_render_duration_must_cover_accepted_scene_boundaries(media):
    result = media_qa.inspect_render(media["moving"], *plan(duration=4))
    assert not result.passed
    assert "video_timing" in categories(result)


def test_clipping_signal_is_measured_and_warned(media):
    result = media_qa.inspect_render(media["clipped"], *plan())
    assert result.passed
    assert "audio_clipping" in categories(result)
    assert result.metrics["audio"]["near_full_scale_sample_ratio"] > 0.001


def test_reference_activity_comparison_detects_dropped_voice(media):
    result = media_qa.inspect_render(
        media["silent"], *plan(), narration_path=media["reference"]
    )
    assert not result.passed
    assert result.coverage.narration_comparison
    assert (
        result.metrics["narration_comparison"]["reference_activity_missing_ratio"] == 1
    )
    assert "narration_activity" in categories(result)


def test_invalid_timeline_and_remote_media_fail_without_starting_decoder(monkeypatch):
    monkeypatch.setattr(
        media_qa, "_probe", lambda _: pytest.fail("decoder must not start")
    )
    result = media_qa.inspect_render("https://example.org/secret.mp4", *plan())
    assert not result.passed and "media_input" in categories(result)


def test_invalid_timeline_fails_closed(media):
    production, timeline = plan()
    timeline[0]["start"] = 0.5
    result = media_qa.inspect_render(media["moving"], production, timeline)
    assert not result.passed and "media_input" in categories(result)


def test_decoder_failure_cannot_claim_full_coverage(media, monkeypatch):
    def failed_video(*args):
        return {
            "black_intervals": [],
            "freeze_intervals": [],
            "duration_seconds": 1.0,
            "frames": 10,
            "complete": False,
            "events_truncated": False,
            "status": "timeout",
        }

    monkeypatch.setattr(media_qa, "_scan_video", failed_video)
    result = media_qa.inspect_render(media["moving"], *plan())
    assert not result.passed and not result.coverage.full_file_decode
    assert "video_decode" in categories(result)
    assert str(media["moving"]) not in result.model_dump_json()


def test_audio_comparison_tolerates_background_music_levels():
    audio = {
        "energy_windows": [0.02, 0.03, 0.01, 0.1, 0.005],
        "duration_seconds": 0.5,
        "rms_dbfs": -20,
    }
    rendered = dict(
        audio, energy_windows=[value + 0.1 for value in audio["energy_windows"]]
    )
    result = media_qa._compare_audio(rendered, audio)
    assert result["energy_envelope_correlation"] == pytest.approx(1)
    assert result["reference_activity_missing_ratio"] == 0


def test_mapping_splits_an_interval_across_unequal_scenes():
    result = media_qa._mapped(
        [{"start": 0.5, "end": 3.5}],
        [
            {"scene_id": "a", "start": 0, "end": 1, "expected_static": False},
            {"scene_id": "b", "start": 1, "end": 4, "expected_static": True},
        ],
    )
    assert result == [
        {"scene_id": "a", "start": 0.5, "end": 1, "expected_static": False},
        {"scene_id": "b", "start": 1, "end": 3.5, "expected_static": True},
    ]


def test_stderr_never_reaches_artifacts(tmp_path):
    broken = tmp_path / "secret-token-DO-NOT-ECHO.mp4"
    broken.write_text("broken private input")
    result = media_qa.inspect_render(broken, *plan())
    assert not result.passed
    assert "secret-token" not in result.model_dump_json()
    assert "private input" not in result.model_dump_json()


def test_stalled_decoder_is_terminated():
    ok, status = media_qa._run(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        0.05,
        lambda _: None,
    )
    assert not ok and status == "timeout"
