/goal

Transform this MoneyPrinterTurbo checkout so Codex becomes the production-intelligence layer while MoneyPrinterTurbo remains the deterministic media-production engine.

Do not merely replace the existing OpenAI provider. Build a Codex-controlled production workflow above the existing MoneyPrinterTurbo pipeline.

Core requirements:

1. Use the official OpenAI Codex Python SDK (`openai-codex`), not the OpenAI Python API, for Codex intelligence.

2. Codex authentication must use the user's existing ChatGPT/Codex subscription login.
   - Require/prefer ChatGPT authentication.
   - Do not require `OPENAI_API_KEY`.
   - Never silently fall back to API-key billing.
   - Isolate the Codex runtime from `OPENAI_API_KEY`, `CODEX_API_KEY`, and conflicting API-provider environment variables without breaking those variables for the rest of MoneyPrinterTurbo.
   - If ChatGPT authentication is unavailable, fail with a clear actionable message rather than changing authentication methods.

3. Preserve all existing MoneyPrinterTurbo workflows and providers. Legacy operation must continue working.

4. Add `Codex (ChatGPT subscription)` as a first-class LLM provider using MoneyPrinterTurbo's existing `LLMProviderSpec` / adapter architecture:
   - no API key
   - no base URL
   - model optional; blank means use Codex's current default
   - generic MPT functions such as script rewriting and social metadata must be able to use it.

5. Add a new `app/intelligence/` subsystem. Keep intelligence logic out of `task.py` as much as practical.

Create structured Pydantic contracts for at least:

- ProductionBrief
- ProductionPlan
- ScenePlan
- VisualPlan
- ProductionReview
- ReviewIssue
- RepairAction

A ProductionPlan must contain scene-level narration, purpose, target duration, visual intent, preferred visual type, search query/generation prompt, on-screen text, transitions and continuity information.

Supported visual intents/types should include at least:

- stock_video
- ai_video
- ai_image
- local_asset
- diagram
- chart
- screenshot
- text_card
- icon_composition

Do not pretend unsupported execution providers exist. Represent unsupported media types cleanly so future providers can implement them.

6. Build Codex production roles on top of a reusable Codex runtime:

- ProductionDirector
- ScriptEditor
- VisualDirector
- PlanReviewer
- MaterialReviewer
- RenderReviewer
- RepairPlanner

Use Codex structured output / JSON schemas where available. Validate every result through the Pydantic contracts. Do not parse loosely formatted prose when structured output can be used.

7. Add an intelligence mode to the video-generation flow.

Desired pipeline:

brief
→ Codex production plan
→ adversarial plan review
→ repair if needed
→ narration/script
→ scene visual plan
→ existing MPT TTS
→ existing MPT subtitles
→ existing MPT material acquisition
→ material contact sheet
→ Codex material review
→ replace/retry only rejected materials when practical
→ existing MPT video composition
→ rendered-frame contact sheet
→ Codex final visual QA
→ targeted repair/re-render
→ final video.

Do not regenerate already-good stages unnecessarily.

8. Reuse MoneyPrinterTurbo's existing stage-oriented architecture rather than rewriting it.

Preserve existing stages and intermediate outputs:
script
terms
audio
subtitle
materials
video.

Add intelligence artifacts alongside them.

9. Persist useful artifacts in each task directory, including:

production-brief.json
production-plan.json
production-review.json
material-review.json
render-review.json
repair-history.json
materials-contact-sheet.jpg
render-contact-sheet.jpg

Do not store authentication credentials or tokens in task artifacts or logs.

10. Implement contact-sheet generation using local media tooling.

For material review:
- extract representative thumbnails from selected videos/images
- label them by scene
- produce a single reviewable contact sheet.

For final render review:
- choose representative frames based on scene/timeline boundaries rather than only arbitrary fixed intervals where practical
- create a labeled contact sheet.

Pass the contact-sheet image to Codex using the SDK's local-image input support along with the relevant ProductionPlan.

11. Add bounded quality loops.

Configuration should support roughly:

production_intelligence = legacy | codex
codex_model_name = optional
codex_reasoning_effort = configurable
codex_review_enabled = true/false
codex_material_review_enabled = true/false
codex_render_review_enabled = true/false
codex_quality_threshold = default around 8.5
codex_max_repair_passes = default 2

Never create an unbounded regeneration loop.

12. Material repair should be scene-aware.

When the reviewer rejects one scene, prefer:
- changing that scene's search query,
- selecting another asset,
- regenerating that scene's media,
- or changing that scene's visual modality

instead of regenerating the entire production.

Maintain compatibility with MoneyPrinterTurbo's existing sequential material-matching behavior.

13. Keep rendering deterministic.

Codex should decide WHAT should be produced.
MoneyPrinterTurbo should decide HOW to execute media operations.

Codex must not directly replace FFmpeg, TTS, subtitle rendering, material download infrastructure, task state, publishing or existing provider implementations.

14. Add WebUI controls for Production Intelligence.

Expose:
- Legacy / Codex
- Codex authentication status
- optional model selection
- reasoning effort
- plan review toggle
- material review toggle
- final render review toggle
- quality threshold
- max repair passes.

Do not show an OpenAI API-key field for the Codex subscription provider.

If practical, expose a "Connect Codex / Sign in with ChatGPT" action using the SDK authentication flow; otherwise provide an exact local-login instruction.

15. Preserve API and CLI usability.

The new intelligence settings must be representable through the existing request/parameter model so AI agents can control them without the WebUI.

16. Add proper failure states.

Examples:

codex_auth
production_plan
plan_review
visual_plan
material_review
render_review
repair

Return actionable error messages through the existing task-state mechanism.

Codex review failure must not destroy already generated usable artifacts.

17. Tests are required.

Add unit tests for:
- Codex provider metadata
- subscription-only auth behavior
- no silent API-key fallback
- structured-output validation
- production-plan generation
- plan review and repair
- max repair-pass enforcement
- material review
- render review
- task integration
- legacy pipeline regression
- credential redaction
- failure-state reporting.

Mock Codex calls in normal unit tests. Tests must not require a real ChatGPT account.

18. Add a small explicit integration smoke-test path that a developer who is already signed into Codex can run manually.

19. Update documentation with:

- architecture diagram
- how Codex subscription authentication works
- local setup
- how to enable Production Intelligence
- what still requires third-party API keys
- artifact descriptions
- troubleshooting.

20. Do not use `OPENAI_API_KEY` for the Codex intelligence path.

Do not stop after producing a plan.

Inspect the current repository first, implement the feature, run targeted tests, run the full relevant test suite, and fix failures.

Then run a local smoke test far enough to prove:
- the WebUI/API can construct Codex intelligence settings,
- the production pipeline reaches the Codex intelligence boundary correctly,
- legacy generation remains functional.

Do not make paid external AI-video calls during automated testing.

At completion report:
- architecture implemented
- files added/modified
- tests executed and results
- remaining limitations
- exact commands to sign into Codex and run MoneyPrinterTurbo locally.