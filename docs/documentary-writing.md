# Documentary writing in Case Workspace

The Documentary Writer turns reviewed case evidence into a chapter outline,
cited narration, a factual review and scene footage requirements. It uses the
configured text provider, including the existing keyless Codex provider. It
preserves citations in structured records rather than passing them through the
ordinary topic-script formatter.

For narration informed by a studied reference, the optional
[documentary craft agents](documentary-agents.md) add a private storytelling
blueprint, chapter writing and an independent narrative review through the
writer's programmatic hook. The existing factual review and editorial approval
still apply.

## Prepare the evidence

In **Documentary workspace → Sources & evidence**, import retained legal documents and recordings,
review their permitted uses and index them. In **Facts & timeline**, record the
timeline and review the claims that the documentary will use. Supporting
citations must identify current indexed passages from retained sources. A
source URL or footage discovery result alone does not supply evidence for a
factual narration passage.

Claims retain their assertion class: allegations, testimony and reporting must
remain attributed in the narration. Production scripts and narration cannot
serve as supporting evidence for the next draft.

## Write and review

Open **Script**. Saved scripts appear first with an outline-to-export progress
indicator and the next action for their current stage. **Read**, **Edit**,
**Review**, and **Export** show one task at a time. Choose **New script** to select reviewed claims. Inspect the evidence
packet, choose a title, language and target duration, and generate an outline.
New documentaries target **22–28 minutes**, with **25 minutes** selected by
default in the UI, API and CLI. The writer plans approximately 3,190–4,060
spoken words at its 145-words-per-minute planning pace, aiming for 3,625 words
at the default target. This is a writing estimate; measure the recorded
narration and account for footage, original audio and pauses when editing.
Evidence gaps remain explicit rather than being padded to meet the length.
Existing saved scripts remain readable; regenerating their narration uses the
current target range and clears the previous factual review and approval.

Generate the cited draft from the saved outline. Chapters contain stable scene
IDs, narration passages with claim and citation references, exact source
quotations, footage search queries and missing evidence notes. Footage
requirements are search instructions; acquiring clips and binding storyboard
assets remain explicit actions in **Footage** and **Edit & export**.

Saved drafts show their spoken-word count and an estimated English narration
length. The count excludes headings, citation markers and separate quote
metadata. An estimate outside 22–28 minutes prompts a length review before
recording; other languages show the word count without a timing estimate.
Writing-job status follows the selected script and revision. Older attempts
remain available in the collapsed case-job history.

Run the factual review. Deterministic checks reject invented references,
incorrect quote text and citations outside the selected evidence. The model
review evaluates support and attribution. Its result is a review aid; an editor
still reviews the cited draft and records an approval or rejection. Editing a
draft clears its previous review and approval.

Source versions, evidence hashes, claim revisions and permitted uses are
checked again after generation and before approval or export. If they change,
review the current evidence and create a new draft.

## Local outputs and production

Draft exports include `Outline.json`, `Cited_Draft.md`,
`Footage_Requirements.json` and `Citation_Map.json`. An approved final export
also provides `Final_Script.md`, with clean spoken narration separated from
research citations and original-sound quote inserts. The final script is
registered as a production asset for selection in the existing storyboard
editor. Review its permitted uses in **Sources & evidence** before production.

Files are written to versioned directories inside the case's `05_Production/`
folder under the search storage root. The repository's `storage/` ignore rule
keeps case output untracked. Keep custom storage roots outside the public
checkout or ignore them locally.

For recorded source speech, use case indexing/transcription. For recorded
narration, import aligned word timestamps against the matching final script
and audio versions. The Documentary Writer writes narration; it does not
record or align speech.

## API and CLI

The authenticated API uses the following case-scoped endpoints:

| Operation | Endpoint |
| --- | --- |
| Preview selected evidence | `POST /api/v1/cases/{case_id}/documentaries/evidence` |
| List writing projects | `GET /api/v1/cases/{case_id}/documentaries` |
| Queue a writing stage | `POST /api/v1/cases/{case_id}/documentaries` |
| Get a saved project | `GET /api/v1/cases/{case_id}/documentaries/{document_id}` |
| Save a draft revision | `PUT /api/v1/cases/{case_id}/documentaries/{document_id}` |
| Record human review | `POST /api/v1/cases/{case_id}/documentaries/{document_id}/review` |
| Export draft or final | `POST /api/v1/cases/{case_id}/documentaries/{document_id}/export` |
| Download an exported file | `GET /api/v1/cases/{case_id}/documentaries/{document_id}/files/{filename}` |

Generation accepts `title`, `target_minutes`, `language`, `claim_ids` and optional
`instructions`. `target_minutes` defaults to 25 and must be between 22 and 28,
inclusive. `stage` is `outline`, `draft` or `factual_review`; later stages
also require the saved `document_id`. The request queues a durable
`case_documentary` job. The API starts the existing worker automatically.

Revision, review and export requests include `expected_revision` to guard
against concurrent edits. File downloads also require that revision and use
`final=true` for a final export. Each delivery rechecks current evidence and
file hashes.

The same operations are available through
`python -m app.services.targeted_search.cli`:

```bash
python -m app.services.targeted_search.cli case-documentary-packet CASE_ID
python -m app.services.targeted_search.cli case-documentary-write CASE_ID \
  "Documentary title" --minutes 25 --stage outline
python -m app.services.targeted_search.cli worker --drain
python -m app.services.targeted_search.cli case-documentary-list CASE_ID
python -m app.services.targeted_search.cli case-documentary-write CASE_ID \
  "Documentary title" --stage draft --document DOCUMENT_ID
python -m app.services.targeted_search.cli worker --drain
python -m app.services.targeted_search.cli case-documentary-write CASE_ID \
  "Documentary title" --stage factual_review --document DOCUMENT_ID
python -m app.services.targeted_search.cli worker --drain
python -m app.services.targeted_search.cli case-documentary-get CASE_ID DOCUMENT_ID
python -m app.services.targeted_search.cli case-documentary-review CASE_ID \
  DOCUMENT_ID --revision CURRENT_REVISION --reviewer "Editor" --approve
python -m app.services.targeted_search.cli case-documentary-export CASE_ID \
  DOCUMENT_ID --revision CURRENT_REVISION --final
```

Use `case-documentary-revise CASE_ID DOCUMENT_ID draft.json --revision N` to
save a structured manual edit. Read the current revision after each writing or
review stage. The CLI only queues generation; run `worker` to process the jobs.
