"""Real local PDF, still and stereo-audio case evidence integration checks."""

import dataclasses
import json
import math
import shutil
import struct
import sys
import wave
import zlib
from types import SimpleNamespace
from importlib.machinery import ModuleSpec

import pytest

from app.models.search import SearchError
from app.services.targeted_search import case_media
from app.services.targeted_search.case_workspace import CaseWorkspace
from app.services.targeted_search.media import sha256_file, verified_artifact_path
from app.services.targeted_search.service import SearchService


def make_pdf(path, texts, scanned_image=None):
    pytest.importorskip("pypdf")
    from pypdf import PdfWriter
    from pypdf.generic import (
        DecodedStreamObject,
        DictionaryObject,
        NameObject,
        NumberObject,
    )

    writer = PdfWriter()
    font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
    )
    for text in texts:
        page = writer.add_blank_page(width=612, height=792)
        content = DecodedStreamObject()
        if scanned_image is None:
            lines = (
                text.replace("\\", "\\\\")
                .replace("(", "\\(")
                .replace(")", "\\)")
                .splitlines()
            )
            content.set_data(
                (
                    "BT /F1 12 Tf 35 755 Td "
                    + " ".join(f"({line}) Tj 0 -16 Td" for line in lines)
                    + " ET"
                ).encode()
            )
            page[NameObject("/Resources")] = DictionaryObject(
                {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
            )
        else:
            image = scanned_image.convert("RGB")
            stream = DecodedStreamObject()
            stream.set_data(zlib.compress(image.tobytes()))
            stream.update(
                {
                    NameObject("/Type"): NameObject("/XObject"),
                    NameObject("/Subtype"): NameObject("/Image"),
                    NameObject("/Width"): NumberObject(image.width),
                    NameObject("/Height"): NumberObject(image.height),
                    NameObject("/BitsPerComponent"): NumberObject(8),
                    NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
                    NameObject("/Filter"): NameObject("/FlateDecode"),
                }
            )
            page[NameObject("/Resources")] = DictionaryObject(
                {
                    NameObject("/XObject"): DictionaryObject(
                        {NameObject("/Im0"): writer._add_object(stream)}
                    )
                }
            )
            content.set_data(b"q 612 0 0 792 0 0 cm /Im0 Do Q")
        page[NameObject("/Contents")] = writer._add_object(content)
    writer.set_page_label(0, len(texts) - 1, style="/r", start=4)
    writer.write(path)


def make_audio(path, seconds=3):
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(2)
        stream.setsampwidth(2)
        stream.setframerate(32000)
        data = bytearray()
        for sample in range(seconds * 32000):
            data.extend(
                struct.pack(
                    "<hh",
                    round(12000 * math.sin(2 * math.pi * 997 * sample / 32000)),
                    round(9000 * math.sin(2 * math.pi * 440 * sample / 32000)),
                )
            )
        stream.writeframes(data)


@pytest.fixture
def case(tmp_path):
    from PIL import Image, ImageDraw, ImageFont

    service = SearchService(tmp_path / "search")
    service.settings = dataclasses.replace(
        service.settings,
        asr_model=str(tmp_path / "missing-model"),
        visual_enabled=False,
        semantic_enabled=False,
        ocr_enabled=False,
        local_models_only=True,
    )
    service.repo.settings = service.settings
    workspace = CaseWorkspace(service)
    record = workspace.create_case("Owned synthetic case", "Evidence fixtures")
    folder = service.repo.root / "owned" / "input"
    for name in ["01_Documents", "02_Audio", "03_Images", "04_Maps", "05_Production"]:
        (folder / name).mkdir(parents=True)
    make_pdf(
        folder / "01_Documents" / "witness.pdf",
        [
            "First physical page. Witness observation.",
            "Second physical page. Source statement.",
        ],
    )
    make_audio(folder / "02_Audio" / "recording.wav")
    image = Image.new("RGB", (800, 300), "white")
    ImageDraw.Draw(image).text(
        (40, 60),
        "CASE EVIDENCE 2026",
        font=ImageFont.load_default(size=38),
        fill="black",
    )
    image.save(folder / "03_Images" / "evidence.png")
    (folder / "04_Maps" / "location.geojson").write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "id": "site-1",
                        "properties": {"name": "Supplied site"},
                        "geometry": {
                            "type": "Point",
                            "coordinates": [-103.623, 48.147],
                        },
                    }
                ],
            }
        )
    )
    (folder / "05_Production" / "narration.txt").write_text(
        "Production narration draft.\nNot reviewed source testimony.\n"
    )
    review = {
        "rights_status": "allowed_export",
        "permitted_use": "internal_review,analysis,generated_export,publication,clip_export,archival",
        "reason": "Test-owned synthetic originals",
        "reviewed_by": "fixture-author",
    }
    imported = workspace.import_folder(record["id"], folder, rights_review=review)
    assets = {a["filename"]: a for a in imported["assets"]}
    return workspace, record, folder, assets, review


def units(workspace, asset, active=True):
    with workspace.repo.connect() as connection:
        rows = connection.execute(
            "SELECT * FROM evidence_units WHERE asset_id=? AND is_active=? ORDER BY created_at,id",
            (asset["id"], int(active)),
        ).fetchall()
    return [
        {
            **dict(row),
            "locator": json.loads(row["locator_json"]),
            "metadata": json.loads(row["metadata_json"]),
        }
        for row in rows
    ]


def test_native_pdf_page_labels_render_and_exact_passage_roundtrip(case):
    workspace, _, _, assets, _ = case
    asset = assets["witness.pdf"]
    result = case_media.index_asset(workspace, asset["id"])
    assert result["pages"] == 2 and result["state"] == "indexed"
    with workspace.repo.connect() as connection:
        pages = connection.execute(
            "SELECT * FROM document_pages WHERE asset_id=? ORDER BY page_index",
            (asset["id"],),
        ).fetchall()
    assert [p["page_label"] for p in pages] == ["iv", "v"]
    passages = [
        u for u in units(workspace, asset) if u["unit_kind"] == "document_passage"
    ]
    assert len(passages) == 2
    for unit in passages:
        locator = unit["locator"]
        page = pages[locator["page_index"]]
        assert page["text"][locator["text_start"] : locator["text_end"]] == unit["text"]
    preview = case_media.preview_asset(
        workspace, asset["id"], {"kind": "page", "page_index": 1}
    )
    from PIL import Image

    path = case_media.preview_content(workspace, preview["id"])
    assert Image.open(path).width <= 1600
    assert preview["metadata"]["locator"]["page_index"] == 1
    with pytest.raises(SearchError):
        case_media.preview_asset(workspace, asset["id"], {"page_index": 2})
    case_media.index_asset(workspace, asset["id"])
    assert len(units(workspace, asset)) == 4  # Two full pages and two bounded passages.


def test_long_pdf_late_passage_is_indexed_with_exact_offsets(case):
    workspace, record, folder, _, review = case
    text = (
        "\n".join(
            [
                "Bounded source passage with supplied statement " + str(i)
                for i in range(80)
            ]
        )
        + "\nLATE DISTINCT EVIDENCE"
    )
    make_pdf(folder / "01_Documents" / "long.pdf", [text])
    imported = workspace.import_folder(record["id"], folder, rights_review=review)
    asset = next(a for a in imported["assets"] if a["filename"] == "long.pdf")
    case_media.index_asset(workspace, asset["id"])
    passages = [
        u for u in units(workspace, asset) if u["unit_kind"] == "document_passage"
    ]
    assert len(passages) >= 3 and all(len(u["text"]) <= 1200 for u in passages)
    assert any("LATE DISTINCT EVIDENCE" in u["text"] for u in passages)


def test_audio_only_original_channels_waveform_and_exact_excerpt(case):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg integration requires installed executables")
    workspace, _, _, assets, _ = case
    asset = assets["recording.wav"]
    original = case_media.asset_content(workspace, asset["id"])
    digest = sha256_file(original)
    probe = case_media.probe_asset(original)
    assert not probe["has_video"] and probe["audio_streams"][0]["channels"] == 2
    assert probe["audio_streams"][0]["sample_rate"] == 32000
    result = case_media.index_asset(workspace, asset["id"])
    assert (
        result["state"] == "partial" and "model" in result["unavailable"][0]["reason"]
    )
    assert (
        case_media.preview_content(workspace, result["waveform_artifact_id"]).suffix
        == ".png"
    )
    excerpt = case_media.extract_audio_preview(workspace, asset["id"], 375, 1675)
    output = case_media.probe_asset(
        case_media.preview_content(workspace, excerpt["id"])
    )
    assert output["duration_ms"] == 1300 and output["audio_streams"][0]["channels"] == 2
    mono = case_media.extract_audio_preview(
        workspace, asset["id"], 375, 1675, channel=1
    )
    assert (
        case_media.probe_asset(case_media.preview_content(workspace, mono["id"]))[
            "audio_streams"
        ][0]["channels"]
        == 1
    )
    assert mono["metadata"]["source_channel"] == 1
    assert (
        mono["metadata"]["source_start_ms"] == 375
        and mono["metadata"]["output_start_ms"] == 0
    )
    assert sha256_file(original) == digest == asset["sha256"]
    with pytest.raises(SearchError):
        case_media.extract_audio_preview(workspace, asset["id"], 0, 1000, channel=2)


def timing_payload(asset):
    return {
        "audio_sha256": asset["sha256"],
        "asset_version_id": asset["asset_version_id"],
        "model_name": "fixture-aligner",
        "model_revision": "sha256:fixture-model",
        "runtime_version": "fixture-v1",
        "segments": [
            {
                "start": 0.125,
                "end": 1.6,
                "text": "A dollar amount",
                "words": [
                    {
                        "word": "A",
                        "start": 0.125,
                        "end": 0.3,
                        "score": 0.95,
                        "speaker": "SPEAKER_00",
                    },
                    {"word": "$13.60", "custom_unsupported_number": True},
                    {"word": "amount", "start": 1.2, "end": 1.6, "score": 0.87},
                ],
            }
        ],
        "upstream_private_fields": {"retained": [1, "unaligned", None]},
    }


def test_whisperx_words_preserve_null_times_raw_payload_and_review_supersedes(case):
    workspace, _, _, assets, _ = case
    asset = assets["recording.wav"]
    payload = timing_payload(asset)
    result = case_media.import_whisperx(workspace, asset["id"], payload)
    assert result["unaligned_words"] == 1
    record = json.loads(
        case_media.preview_content(workspace, result["artifact_id"]).read_text()
    )
    assert record["raw_input"] == payload
    with workspace.repo.connect() as connection:
        words = connection.execute(
            "SELECT * FROM transcript_words WHERE transcript_artifact_id=? ORDER BY word_index",
            (result["artifact_id"],),
        ).fetchall()
    assert words[1]["start_ms"] is None and words[1]["end_ms"] is None
    assert (
        words[0]["speaker"] == "SPEAKER_00"
        and record["speaker_identity_verified"] is False
    )
    unknown_unit = next(u for u in units(workspace, asset) if u["text"] == "$13.60")
    assert "start_ms" not in unknown_unit["locator"]
    assert (
        unknown_unit["locator"]["word_start_index"] == 1
        and unknown_unit["locator"]["word_end_index"] == 2
    )
    reviewed = case_media.import_reviewed_transcript(
        workspace, asset["id"], payload, "editor"
    )
    assert reviewed["artifact_id"] != result["artifact_id"]
    assert all(
        u["artifact_id"] == reviewed["artifact_id"] for u in units(workspace, asset)
    )
    assert len(units(workspace, asset, False)) == 4
    case_media.import_reviewed_transcript(workspace, asset["id"], payload, "editor")
    assert len(units(workspace, asset)) == 4


def test_narration_alignment_is_separate_and_pins_script_version(case):
    workspace, record, folder, assets, review = case
    audio, script = assets["recording.wav"], assets["narration.txt"]
    payload = {**timing_payload(audio), "script_sha256": script["sha256"]}
    result = case_media.import_whisperx(
        workspace, audio["id"], payload, "narration", script["id"]
    )
    assert units(workspace, audio) == []
    alignment = workspace.repo.get("artifacts", result["artifact_id"])
    assert alignment["kind"] == "narration_alignment"
    case_media.preview_content(workspace, alignment["id"])
    (folder / "05_Production" / "narration.txt").write_text("Revised script version.")
    workspace.import_folder(record["id"], folder, rights_review=review)
    with pytest.raises(SearchError, match="superseded"):
        case_media.preview_content(workspace, alignment["id"])
    with pytest.raises(SearchError, match="script hash"):
        case_media.import_whisperx(
            workspace, audio["id"], payload, "narration", script["id"]
        )
    with pytest.raises(SearchError, match="superseded"):
        case_media.align_audio(
            workspace,
            audio["id"],
            script["id"],
            "narration",
            payload,
            script_asset_version_id=script["asset_version_id"],
            input_script_sha256=script["sha256"],
        )


def test_transcript_mismatch_bounds_and_current_rights_reject(case):
    workspace, _, _, assets, _ = case
    asset = assets["recording.wav"]
    payload = timing_payload(asset)
    for changed in [
        {**payload, "audio_sha256": "0" * 64},
        {**payload, "asset_version_id": "old-version"},
        {**payload, "words": [{"word": "outside", "start": 2.8, "end": 4}]},
        {**payload, "words": [{"word": "one-null", "start": 0.2}]},
    ]:
        with pytest.raises(SearchError):
            case_media.import_whisperx(workspace, asset["id"], changed)
    preview = case_media.extract_audio_preview(workspace, asset["id"], 0, 500)
    workspace.search_service.set_policy(
        asset["source_id"], "blocked", "analysis", "Revoked test rights", "reviewer"
    )
    for call in [
        lambda: case_media.asset_content(workspace, asset["id"]),
        lambda: case_media.preview_content(workspace, preview["id"]),
        lambda: case_media.index_asset(workspace, asset["id"]),
        lambda: case_media.import_whisperx(workspace, asset["id"], payload),
    ]:
        with pytest.raises(SearchError):
            call()


def test_image_ocr_regions_use_normalized_source_coordinates(case):
    workspace, _, _, assets, _ = case
    if not case_media.capabilities()["ocr"]:
        pytest.skip("Optional Tesseract OCR unavailable")
    workspace.settings = dataclasses.replace(workspace.settings, ocr_enabled=True)
    asset = assets["evidence.png"]
    result = case_media.index_asset(workspace, asset["id"])
    assert result["state"] == "indexed"
    regions = [u for u in units(workspace, asset) if u["unit_kind"] == "image_region"]
    assert any("CASE" in u["text"] for u in regions)
    for unit in regions:
        assert all(0 <= v <= 1 for v in unit["locator"]["bbox"])
        assert unit["metadata"]["reviewed"] is False
    preview = case_media.preview_asset(
        workspace, asset["id"], {"kind": "image", "bbox": [0, 0, 0.5, 1]}
    )
    from PIL import Image

    assert Image.open(case_media.preview_content(workspace, preview["id"])).size == (
        400,
        300,
    )


def test_scanned_pdf_optional_ocr_page_and_regions(case):
    workspace, record, folder, _, review = case
    if not case_media.capabilities()["ocr"]:
        pytest.skip("Optional Tesseract OCR unavailable")
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (612, 792), "white")
    ImageDraw.Draw(image).text(
        (30, 80),
        "SCANNED CASE RECORD",
        font=ImageFont.load_default(size=30),
        fill="black",
    )
    make_pdf(folder / "01_Documents" / "scan.pdf", [""], scanned_image=image)
    imported = workspace.import_folder(record["id"], folder, rights_review=review)
    asset = next(a for a in imported["assets"] if a["filename"] == "scan.pdf")
    partial = case_media.index_asset(workspace, asset["id"])
    assert partial["pages_needing_ocr"] == [0] and partial["state"] == "partial"
    workspace.settings = dataclasses.replace(workspace.settings, ocr_enabled=True)
    result = case_media.index_asset(workspace, asset["id"])
    assert result["state"] == "indexed"
    regions = [
        u for u in units(workspace, asset) if u["unit_kind"] == "document_region"
    ]
    assert any("SCANNED" in u["text"] for u in regions)
    assert all(all(0 <= v <= 1 for v in u["locator"]["bbox"]) for u in regions)


def test_geojson_features_keep_supplied_coordinates_without_geocoding(case):
    workspace, _, _, assets, _ = case
    asset = assets["location.geojson"]
    result = case_media.index_asset(workspace, asset["id"])
    assert result["features"] == 1
    unit = units(workspace, asset)[0]
    assert unit["metadata"]["geometry"]["coordinates"] == [-103.623, 48.147]
    assert (
        unit["metadata"]["coordinates_inferred"] is False
        and unit["metadata"]["supplied_crs"] is None
    )
    for invalid in [
        {"type": "Point", "coordinates": [[1, 2]]},
        {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1]]]},
    ]:
        with pytest.raises(SearchError):
            case_media._validate_geometry(invalid)


def test_changed_audio_version_and_corrupt_preview_are_denied(case):
    workspace, record, folder, assets, review = case
    asset = assets["recording.wav"]
    payload = timing_payload(asset)
    preview = case_media.extract_audio_preview(workspace, asset["id"], 0, 500)
    make_audio(folder / "02_Audio" / "recording.wav", seconds=4)
    workspace.import_folder(record["id"], folder, rights_review=review)
    with pytest.raises(SearchError, match="superseded"):
        case_media.preview_content(workspace, preview["id"])
    with pytest.raises(SearchError, match="audio hash"):
        case_media.import_whisperx(workspace, asset["id"], payload)
    current = workspace.get_asset(asset["id"])
    new_preview = case_media.extract_audio_preview(workspace, current["id"], 0, 500)
    verified_artifact_path(workspace.repo, new_preview).write_bytes(b"changed")
    with pytest.raises(SearchError, match="digest"):
        case_media.preview_content(workspace, new_preview["id"])


def test_missing_whisperx_runtime_returns_actionable_capability(case):
    workspace, _, _, assets, _ = case
    if case_media.capabilities()["whisperx"]:
        pytest.skip("Environment has optional WhisperX runtime")
    with pytest.raises(SearchError, match="WhisperX runtime") as error:
        case_media.align_audio(workspace, assets["recording.wav"]["id"])
    assert error.value.status_code == 503


def test_video_asr_preserves_words_and_populates_existing_footage_caption_index(
    case, monkeypatch
):
    from app.services.targeted_search.media import executable, run_command

    workspace, record, folder, _, review = case
    output = folder / "03_Images" / "footage.mp4"
    run_command(
        [
            executable("ffmpeg"),
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-loop",
            "1",
            "-i",
            str(folder / "03_Images" / "evidence.png"),
            "-i",
            str(folder / "02_Audio" / "recording.wav"),
            "-t",
            "3",
            "-r",
            "25",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-threads",
            "2",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(output),
        ],
        timeout=30,
    )
    imported = workspace.import_folder(record["id"], folder, rights_review=review)
    asset = next(a for a in imported["assets"] if a["filename"] == "footage.mp4")
    calls = []

    class FixtureWhisper:
        def __init__(self, *args, **kwargs):
            assert kwargs["local_files_only"] is True

        def transcribe(self, path, **kwargs):
            calls.append(kwargs)
            analysis_probe = case_media.probe_asset(path)
            assert analysis_probe["audio_streams"][0]["channels"] == 1
            assert analysis_probe["audio_streams"][0]["sample_rate"] == 16000
            words = [
                SimpleNamespace(
                    word="Synthetic", start=0.125, end=0.6, probability=0.94
                ),
                SimpleNamespace(
                    word="footage testimony", start=0.6, end=1.2, probability=0.95
                ),
            ]
            return iter(
                [
                    SimpleNamespace(
                        start=0.125,
                        end=1.2,
                        text="Synthetic footage testimony",
                        words=words,
                    )
                ]
            ), SimpleNamespace(language="en")

    monkeypatch.setitem(
        sys.modules,
        "faster_whisper",
        SimpleNamespace(
            WhisperModel=FixtureWhisper, __spec__=ModuleSpec("faster_whisper", None)
        ),
    )
    result = case_media.index_asset(workspace, asset["id"])
    assert calls[0]["word_timestamps"] is True and result["transcript"]["words"] == 2
    source = workspace.repo.get("sources", asset["source_id"])
    assert source["duration_ms"] == 3000 and source["metadata"]["asset_kind"] == "video"
    assert workspace.get_asset(asset["id"])["metadata"]["duration_ms"] == 3000
    with workspace.repo.connect() as connection:
        chunks = connection.execute(
            "SELECT * FROM transcript_chunks WHERE source_id=?", (asset["source_id"],)
        ).fetchall()
        caption = connection.execute(
            "SELECT * FROM captions WHERE source_id=?", (asset["source_id"],)
        ).fetchone()
    assert chunks and "footage testimony" in chunks[0]["text"]
    assert json.loads(caption["metadata_json"])["source_sha256"] == asset["sha256"]
    monkeypatch.setattr(
        case_media,
        "transcribe_audio",
        lambda *a: pytest.fail("Existing source captions must be retained"),
    )
    second = case_media.index_asset(workspace, asset["id"])
    assert second["existing_caption_ids"] == [caption["id"]]


def test_production_audio_index_does_not_create_source_transcript(case, monkeypatch):
    workspace, record, folder, _, review = case
    shutil.copyfile(
        folder / "02_Audio" / "recording.wav", folder / "05_Production" / "voice.wav"
    )
    imported = workspace.import_folder(record["id"], folder, rights_review=review)
    asset = next(a for a in imported["assets"] if a["filename"] == "voice.wav")
    assert asset["metadata"]["role"] == "production"
    monkeypatch.setattr(
        case_media,
        "transcribe_audio",
        lambda *a: pytest.fail("Narration must not be source evidence"),
    )
    result = case_media.index_asset(workspace, asset["id"])
    assert result["narration_alignment_required"] is True
    assert not any(
        u["unit_kind"] == "transcript_segment" for u in units(workspace, asset)
    )


def test_native_index_persistence_rejects_version_change_between_check_and_write(
    case, monkeypatch
):
    workspace, record, folder, assets, review = case
    asset = assets["witness.pdf"]
    original_add = workspace.add_evidence_unit
    changed = []

    def changed_before_write(*args, **kwargs):
        if not changed:
            changed.append(True)
            assert kwargs["metadata"]["asset_version_id"] == asset["asset_version_id"]
            make_pdf(
                folder / "01_Documents" / "witness.pdf",
                ["Replacement evidence belongs to a new version."],
            )
            workspace.import_folder(record["id"], folder, rights_review=review)
        return original_add(*args, **kwargs)

    monkeypatch.setattr(workspace, "add_evidence_unit", changed_before_write)
    with pytest.raises(SearchError):
        case_media.index_asset(workspace, asset["id"])
    current = workspace.get_asset(asset["id"])
    assert current["asset_version_id"] != asset["asset_version_id"]
    assert units(workspace, current) == []
