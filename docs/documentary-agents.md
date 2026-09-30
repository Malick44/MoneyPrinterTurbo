# Documentary craft agents

The optional craft agents turn an analyzed reference's general storytelling
techniques into original case narration. They work with the existing
`DocumentaryWriter` through its `response_generator` hook. They do not change
the API, database, UI, factual-review contract or human approval requirements.
This is an optional programmatic adapter. The current UI and its worker continue
using the existing configured provider; a private runner installs these hooks
explicitly for a reference-based narration run.

Reference analysis and case research have different purposes. Save the watched
video's transcript, observations and analysis privately. Extract a separate
craft blueprint describing opening questions, reveal timing, sentence rhythm,
transitions and ending. The blueprint must omit the reference's case facts,
distinctive phrases, dialogue and scene sequence. Its free-text fields are
bounded and treated as untrusted guidance. A typed shape cannot prove that a
rule contains no factual material; inspect that boundary when preparing it.

The writer uses the retained case evidence packet as its only factual
authority. It builds tension through questions the record can answer, makes
causal qualifications and attribution local, and distinguishes allegations,
testimony and court findings. Suggested document inserts, source audio and
footage are editorial requests until the relevant assets are acquired and
their uses reviewed.

## Private blueprint

`CraftBlueprint` accepts the following fields. Extra fields, including a raw
reference transcript, are rejected.

| Field | Contract |
| --- | --- |
| `name` | 1–200 characters |
| `hook_strategy`, `reveal_strategy`, `ending_strategy` | 1–2,000 characters each |
| `narrative_arc`, `pacing_rules`, `narration_rules`, `transition_rules`, `avoid_rules` | 1–12 rules each; 1–1,200 characters per rule |
| `audiovisual_rules` | Optional 0–12 rules; same rule length bound |

The complete blueprint has a 32,000-byte budget. A simple generic example is:

```json
{
  "name": "Evidence questions",
  "hook_strategy": "Open with a supported object and a precise record question.",
  "narrative_arc": ["Question", "Evidence change", "Answer"],
  "reveal_strategy": "Resolve a question when its supporting evidence arrives.",
  "pacing_rules": ["Develop new evidence before restating an earlier point."],
  "narration_rules": ["Name whose account establishes each claim."],
  "transition_rules": ["Connect the evidence change to the next question."],
  "ending_strategy": "Answer the central question with a scoped outcome.",
  "audiovisual_rules": ["Suggest a retained document insert where appropriate."],
  "avoid_rules": ["Never invent biography, dialogue, audio or private thoughts."]
}
```

Load the JSON with `load_craft_blueprint(path, private_root=...)`. The loader
rejects files outside that explicit root, symlinks, invalid JSON and oversized
input. Choose a Git-ignored root under `storage/` or a directory outside the
public checkout. The loader enforces the path boundary; the caller verifies
that the chosen root is ignored. Keep reference media, case-specific rules,
prompts, model responses, narration and review reports there.

## Writer hook and chapter budgets

The structured callback takes `(prompt, output_type)` and returns a Pydantic
model, dictionary or JSON string. `CodexRuntime.generate` matches this interface;
other structured callbacks can be injected for testing or another provider.

```python
from pathlib import Path

from app.intelligence.runtime import CodexRuntime
from app.services.targeted_search.documentary import DocumentaryWriter
from app.services.targeted_search.documentary_agents import (
    DocumentaryNarrationWriter,
    load_craft_blueprint,
)

private_root = Path("storage/targeted_search/owned/private-agent-runs")
blueprint = load_craft_blueprint(
    private_root / "craft-blueprint.json", private_root=private_root
)
runtime = CodexRuntime()
writer = DocumentaryWriter(workspace)
writer.response_generator = DocumentaryNarrationWriter(
    blueprint,
    runtime.generate,
    chapter_by_chapter=True,
)

options = {"title": "Documentary working draft", "target_minutes": 25}
record = writer.generate(case_id, options)
record = writer.generate(
    case_id,
    {**options, "document_id": record["id"], "stage": "draft"},
    expected_packet_hash=record["packet"]["packet_hash"],
)
```

For `outline`, the callback receives `DocumentaryOutline`. A whole-draft call
receives `DocumentaryDraft`. With `chapter_by_chapter=True`, each draft call
receives `DocumentaryChapter`. The hook requests chapters in outline order,
includes cumulative scene/claim continuity and the preceding chapter's final
passage, checks stable chapter and scene IDs, and assembles the complete draft. The existing writer
then validates every claim, citation, exact quote and source snapshot before
saving it. No partial chapter is automatically persisted.

The cumulative `continuity_context` contains all completed scene and passage
IDs, their prior claim/citation uses, and selected actual-speech anchors. It is
bounded to 12,000 characters and the space remaining within the complete
220,000-character prompt. Optional speech anchors shrink before any IDs are
lost. The calculation includes the output schema, instructions, evidence and
chapter feedback. If essential history cannot fit, the call fails explicitly;
it never trims evidence, current-scene findings or history IDs to fit.
Preceding chapters must preserve outline order and scene IDs. Their reference
IDs must belong to the current packet, and each referenced claim needs a
supporting citation in its passage. Generated history is a writing aid, not
source evidence or a substitute for the writer's final validation.

The writer should explain a necessary distinction once at its relevant
connection, apply it, and develop a new supported observation, decision or
consequence. Material attribution and uncertainty remain local. A callback
may reuse an earlier fact or object when its evidentiary role changes or it
closes the governing question; repeated claim IDs are allowed.

By default, chapters share the target word budget in proportion to their scene
counts. Override that with `chapter_word_targets={chapter_id: word_count, ...}`
when constructing the hook. The mapping must cover every outline chapter with
positive integer counts totaling 3,190–4,060 words. This corresponds to the
22–28 minute English planning band at 145 words per minute; 25 minutes targets
3,625 words. Headings, citation markers and production fields are excluded
from spoken counts. The hook does not fabricate or silently retry short drafts
to achieve a budget. Inspect the resulting counts and evidence coverage before
requesting an explicit revision. Measure actual runtime after recording.

`build_writer_prompt(...)` is also available for callers that want to control
individual chapter calls and checkpoint their responses. Supply all preceding
chapters in outline order when requesting a later chapter. Complete assembled
drafts still require the existing writer's validation and revision checks.

A caller can wrap the structured callback to save each prompt and response.
The prompt contains one instruction line followed by JSON; its
`chapter_request` supplies `chapter_id`, `target_spoken_words` and continuity
context. Save artifacts privately. Model failures propagate to the caller;
the craft hook has no automatic retry or rewrite loop.

Factual-review stages pass the original `DocumentaryWriter` prompt directly to
the callback with `DocumentaryFactualReview`. The reference blueprint and craft
feedback are absent from that factual assessment.

## Independent narrative review

```python
from app.services.targeted_search.documentary_agents import (
    DocumentaryNarrativeReviewer,
    validate_narrative_review,
)

reviewer = DocumentaryNarrativeReviewer(blueprint, runtime.generate)
assessment = reviewer.review(record["draft"], options=record["options"])
validate_narrative_review(assessment, record["draft"], blueprint)
private_report = assessment.model_dump()
```

The model returns `NarrativeReview`: an overall craft score from 0 to 10,
`ready_for_editorial_review` or `revise`, and one assessment for each scene.
Every scene contains one assessment for each of its passages. Findings identify
hook, tension, pacing, repetition, transition, clarity, ending, reference
boundary, victim attention, attribution, duration or production honesty
problems and concrete changes. The reviewer also checks oral readability,
promise/payoff closure and causal implications created by adjacent passages.
Unknown IDs, duplicate IDs, missing
coverage and references to another scene's passages are rejected. A major issue
or a passage requiring revision must produce the overall verdict `revise`.
Optional cosmetic preferences may remain minor notes on an `effective`
passage. A minor label does not make a material comprehension, source,
causality or production problem optional: those findings still require a
passage and overall revision verdict. The reviewer prioritizes consequential
changes without suppressing other findings or automatically granting readiness
from a score or an all-minor issue list.

The helper attaches protected `draft_hash`, `blueprint_hash`,
`review_kind="model_narrative_assessment"` and `human_approved=false`, returning
a `NarrativeAssessment`. The model cannot supply those fields. Revalidate that
assessment against the current draft and blueprint before displaying or using
it. Any change to either makes the report stale. Scores measure craft; they
do not establish factual support or rights clearance.

To revise, construct a writer hook with `narrative_feedback=assessment` and run
the draft stage on the document containing that exact previous draft. The
feedback hashes are checked before prompting. The revised narration remains
subject to the evidence and stable-structure checks. Generating or saving it
clears the existing factual assessment and human approval. Run a new narrative
assessment and the independent factual review after the change.

Chapter calls first validate the complete assessment against the complete
previous draft and blueprint, including all coverage and hashes. They then
send a clearly labeled `chapter_scoped_narrative_feedback` projection containing
every current-chapter scene/passages finding, all global notes and the protected
review provenance. Included and omitted scene IDs make the scope explicit;
`full_assessment_hash` identifies the validated complete report. The full
assessment remains unchanged. A stale or incomplete report from another
chapter is rejected before projection. This keeps unrelated chapter reviews
out of each prompt without dropping authoritative evidence. The complete
220,000-character prompt cap still applies and never silently truncates the
evidence packet or a required current-chapter finding.

Availability notes should identify the specific missing record or media.
Retained source text, an acquired PDF, an authenticated original exhibit image,
playback-reviewed footage and publication rights are different states. Explicit
inventory or feedback can establish which document is available. The compact
source packet alone cannot establish production inventory or public-use rights;
avoid blanket missing-asset notes and mark unresolved status honestly. Reviewers
check both narration and production gaps/queries for such conflicts.

Craft reports are private advisory artifacts, not database workflow states.
The reviewer does not persist or approve the document. When orchestrating it
outside `DocumentaryWriter.generate`, keep the existing packet/revision checks
around the run and re-read the current document before saving or exporting.
An editor must still approve the exact supported draft through the existing
human-review workflow before a final script export.

Run the focused regression checks with:

```bash
python -m pytest test/services/test_documentary_agents.py \
  test/services/test_documentary_writer.py \
  test/services/test_documentary_adversarial.py -q
```
