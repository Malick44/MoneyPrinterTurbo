# Implementation report: Codex Production Intelligence

Implemented 2026-09-08 in this checkout. See the [setup and architecture guide](codex-production-intelligence.md) for the diagram, parameters, artifacts, provider capabilities, and troubleshooting.

## Architecture delivered

- Official `openai-codex` Python SDK shared by the generic LLM provider and all seven production roles.
- ChatGPT-only account validation; isolated child environment; official subscription endpoint; no API-key fallback; bounded SDK processes; inherited tools and instructions disabled.
- Pydantic production briefs, scene plans, visual plans, reviews and repairs. Invalid structured output fails at the correct task stage.
- Stage-oriented orchestration around existing audio, subtitle, acquisition, composition, task-state and publishing services. Legacy remains the default.
- Scene-specific acquisition and bounded quality loops, cached scene preparation, frame-aligned timelines, local contact sheets, preserved render backups and actionable failure states.
- WebUI settings/auth status, request-model fields, CLI flags and batch-manifest support.

## Validation

Initial implementation verification on Python 3.11.15 / macOS:

| Check | Result |
| --- | --- |
| `python -X utf8 -m coverage run -m pytest -q test` | **1,192 passed, 19 skipped, 8,631 subtests passed** in 230.02 seconds |
| `python -m coverage report` | **82% coverage**, above the repository's 70% requirement |
| `python -m compileall -q app cli.py main.py webui test` | Passed |
| `ruff check app cli.py main.py webui test` | Passed |
| `uv sync --frozen` / `uv lock --check` | Passed; dependency lock is consistent |
| `git diff --check` | Passed |
| `python -m app.intelligence.smoke --offline` | Passed; zero external AI calls |
| `MPT_CODEX_BIN=/Applications/ChatGPT.app/Contents/Resources/codex python -m app.intelligence.smoke --live` | Passed with blank/default Astra; ChatGPT auth, JSON Schema and local image |
| Local Streamlit health and API OpenAPI schema | Passed |

The suite emitted nine warnings concerning dependency deprecations and Pydantic enum serialization; there were no test failures. Live provider/optional service tests remain skipped according to the repository's existing test policy.

Both offline Codex-mode and legacy pipelines produced real local videos using temporary images and silent audio, with no external AI calls. The offline path also constructs WebUI-shared settings, CLI parameters and a FastAPI request. Real WebUI/API servers started on localhost and exposed healthy endpoints and the Codex request schema.

Live SDK checks passed with the pinned CLI and an explicit compatible model, and with the installed CLI plus the user's blank/current-default Astra model. They verified ChatGPT authentication, structured JSON and SDK local-image input. No external AI-video generation was performed.

## Files added

- [app/intelligence/__init__.py](../app/intelligence/__init__.py)
- [app/intelligence/_codex_launcher.py](../app/intelligence/_codex_launcher.py)
- [app/intelligence/_codex_worker.py](../app/intelligence/_codex_worker.py)
- [app/intelligence/contact_sheets.py](../app/intelligence/contact_sheets.py)
- [app/intelligence/contracts.py](../app/intelligence/contracts.py)
- [app/intelligence/builtin_visuals.py](../app/intelligence/builtin_visuals.py)
- [app/intelligence/visual_contracts.py](../app/intelligence/visual_contracts.py)
- [app/intelligence/media_qa.py](../app/intelligence/media_qa.py)
- [app/intelligence/qa_contracts.py](../app/intelligence/qa_contracts.py)
- [app/intelligence/errors.py](../app/intelligence/errors.py)
- [app/intelligence/execution.py](../app/intelligence/execution.py)
- [app/intelligence/pipeline.py](../app/intelligence/pipeline.py)
- [app/intelligence/roles.py](../app/intelligence/roles.py)
- [app/intelligence/runtime.py](../app/intelligence/runtime.py)
- [app/intelligence/smoke.py](../app/intelligence/smoke.py)
- [docs/codex-production-intelligence.md](../docs/codex-production-intelligence.md)
- [test/services/test_codex_provider.py](../test/services/test_codex_provider.py)
- [test/services/test_codex_runtime.py](../test/services/test_codex_runtime.py)
- [test/services/test_codex_settings.py](../test/services/test_codex_settings.py)
- [test/services/test_intelligence_audit.py](../test/services/test_intelligence_audit.py)
- [test/services/test_intelligence_media.py](../test/services/test_intelligence_media.py)
- [test/services/test_intelligence_pipeline.py](../test/services/test_intelligence_pipeline.py)
- [test/services/test_webui_codex.py](../test/services/test_webui_codex.py)
- [test/services/test_builtin_visuals.py](../test/services/test_builtin_visuals.py)
- [test/services/test_builtin_settings.py](../test/services/test_builtin_settings.py)
- [test/services/test_intelligence_builtin_pipeline.py](../test/services/test_intelligence_builtin_pipeline.py)
- [test/services/test_media_qa.py](../test/services/test_media_qa.py)
- [test/services/test_webui_builtin_visuals.py](../test/services/test_webui_builtin_visuals.py)
- [test/conftest.py](../test/conftest.py)
- This implementation report.

## Files modified

- [README-en.md](../README-en.md)
- [README-ja.md](../README-ja.md)
- [README.md](../README.md)
- [app/models/llm_provider.py](../app/models/llm_provider.py)
- [app/models/schema.py](../app/models/schema.py)
- [app/services/llm.py](../app/services/llm.py)
- [app/services/task.py](../app/services/task.py)
- [cli.py](../cli.py)
- [config.example.toml](../config.example.toml)
- [pyproject.toml](../pyproject.toml)
- [requirements.txt](../requirements.txt)
- [test/README.md](../test/README.md)
- [test/services/test_llm.py](../test/services/test_llm.py)
- [test/services/test_webui_i18n.py](../test/services/test_webui_i18n.py)
- [uv.lock](../uv.lock)
- [webui/Main.py](../webui/Main.py)
- [webui/i18n/en.json](../webui/i18n/en.json)
- [webui/i18n/zh.json](../webui/i18n/zh.json)

## Follow-up: built-in visuals and full-file inspection

The built-in media source now executes diagrams, charts, text cards, icon
compositions and layouts around uploaded screenshot images without a media API
key. The WebUI, API and CLI share validation; model output remains declarative,
and screenshot paths stay confined to managed uploads. Visual repairs regenerate
only changed scenes. Partial acquisition retains completed asset references.

Every Codex-mode final video now receives full-file technical inspection of its
primary video/audio streams,
including decoding, timing, black/freeze intervals, silence, signal levels and
narration-reference comparison. Coverage and per-output/per-pass measurements are
saved beside the video. Technical failures preserve artifacts and stop the run,
even when semantic review is disabled. Technical cautions remain evidence and do
not automatically reopen accepted scenes for repair.

Manual script controls follow the selected Codex workflow, onboarding explains
subscription sign-in, and narrow browser windows stack the production form.
Portrait evidence is now 480 pixels wide, with bounded pages and manifests for
longer productions. Every planned sample is retained and all pages reach the reviewer.

Follow-up verification on 2026-09-08:

| Check | Result |
| --- | --- |
| Complete test suite | **1,317 passed, 19 skipped, 8,631 subtests passed**, 9 existing warnings, 125.31 seconds |
| Ruff, compilation, whitespace checks | Passed |
| Live browser generation | Completed and played successfully: 1080×1920, 4.83 seconds, no browser media error |
| Real Codex reviews | Plan 9.5/10; materials 9.5/10; final render 9/10, with the 8.5 threshold retained |
| Full-file technical inspection | Passed, zero errors/warnings; video decode, video scan, audio scan and narration-reference comparison completed |
| Narrow browser / onboarding | Single-column form at 652px; correct subscription instructions and key-free visual source visible |

Browser test output: [sample video](../storage/tasks/fef4f232-be36-41ff-81ca-a59e759d1d15/final-1.mp4)
and [technical inspection](../storage/tasks/fef4f232-be36-41ff-81ca-a59e759d1d15/media-inspection.json).
This used real subscription planning/review and local built-in visuals with
intentional silence, no background music and no subtitles. It called no external
media-generation provider. An earlier browser sample correctly stopped at the
quality gate, exposing typography and portrait evidence issues that were then fixed.

Tests now isolate their default workflow and configuration writes from the running
WebUI. A missing-font regression no longer creates an invalid directory in the
bundled font list. The initial coverage figure above predates these follow-up changes;
the current full-suite result does not claim a newly measured coverage percentage.

## Remaining limitations

- Codex-mode execution uses capabilities of the selected MPT media source. Confirmed LoomLoom batches use Legacy mode.
- Codex semantic QA samples still frames, supported by full-file technical video/audio scans. Signal checks do not establish spoken meaning, intelligibility, complete semantic motion quality or exact speech synchronization. Matching narration subtitles improve timing; otherwise duration allocation is estimated.
- Built-in screenshots require supplied images. Charts require factual supplied data and a source description. Styling uses the renderer's fixed palette and automatic layout.
- Targeted render repair retains audio, subtitles, music, accepted assets and cached scenes, but final assembly may run again. There is no general cross-process checkpoint-resume API.
- New models may require a newer CLI than the SDK bundles. Use the explicit `MPT_CODEX_BIN` override; MPT does not silently substitute another model or authentication method.
- Third-party media, speech, music and publishing services retain their existing key and billing requirements.

## Exact local commands

For this machine, use the installed current CLI so the existing default Astra model works:

```bash
cd /Users/malickdes/AIWORKSPACE/MoneyPrinterTurbo
uv sync --frozen
export MPT_CODEX_BIN="/Applications/ChatGPT.app/Contents/Resources/codex"
uv run python -m app.intelligence.runtime login
uv run python -m app.intelligence.runtime status
uv run streamlit run webui/Main.py --server.address 127.0.0.1 --server.port 8501
```

Select **Production Intelligence → Codex** in the WebUI. The model can remain blank.
In a separate terminal, repeat the directory/environment setup and start the API:

```bash
uv run uvicorn app.asgi:app --host 127.0.0.1 --port 8080
```

CLI planning-only example and explicit smoke paths:

```bash
uv run python cli.py --video-subject "Why leaves change color" --production-intelligence codex --stop-at script
uv run python -m app.intelligence.smoke --offline
uv run python -m app.intelligence.smoke --live
```
