# Word-anchored cinematic sound design

**Documentary workspace → Sound** implements four stages. A completed,
currently authorized mix opens first with its player and download. The cue sheet
shows exact placement times, spoken words, and selected sound filenames. Setup,
the effect library, and detailed cue editing are optional panels.

1. **Acoustic alignment:** use the narration WAV's matching word timestamps,
   imported as narration alignment or produced by the optional local WhisperX
   forced-alignment adapter.
2. **Tension analysis:** the configured text provider suggests sparse tension,
   reveal and transition cues tied to actual word indices. It cannot supply
   arbitrary timestamps. Narrative tension scores are editorial estimates.
3. **Sound matching:** an explicitly registered WAV bank supplies exact effect
   files, using categories and tags, optionally supplemented by the configured
   local text-vector model.
4. **Composition:** after reviewing the cue plan, FFmpeg places the effects on a
   48 kHz clock and exports a stereo PCM WAV, an editable JSON cue timeline and
   an OpenTimelineIO timeline.

## Narration and alignment

Import a retained mono or stereo PCM WAV and its final script in **Sources & evidence**, then
review their analysis and internal-review permissions. Import word timestamps
in **Edit & export** with scope **narration**, selecting the matching audio and script.
The alignment binds both original hashes and asset versions. Source-speech
transcripts are separate and cannot substitute for narration alignment.

The optional **Align narration if needed** control runs the existing local
WhisperX adapter before analyzing cues. It requires the installed WhisperX
runtime and a configured local alignment checkpoint. If those are unavailable,
import matching alignment JSON. No automatic checkpoint download occurs.

Unaligned words retain null times. The sound agent cannot place a cue on one of
those words. Analysis is bounded to 15,000 words and up to 60 cues per plan.

## Register and match sounds

Import your WAV effects through **Sources & evidence**. In **Sound → Sound effect library**, give each effect a
category, descriptive tags and a short description, then confirm its editorial
production role. Categories are impact, riser, drone, pulse, ambience,
transition, foley and sting.

Only registered effects with current versions and permitted uses enter the
matcher. Primary case audio and unregistered WAV files do not become effects
automatically. Audio already cited for case facts cannot be repurposed as a
sound effect. Registered effects are excluded from default evidence searches
and cannot support reviewed factual claims.

Taxonomy matching works without a vector model. When semantic search is enabled,
the existing installed local embedding model also scores descriptions and tags;
vectors are tied to each sound's metadata and original version. The selected
WAV identity, hash and matching method appear in the cue record.

Unavailable matches remain visibly disabled, with their reason. Add a suitable
effect or select one explicitly before enabling the cue. The app does not fill
gaps with unrelated sound files.

## Review placements and mix

Each cue shows its anchor word, tension estimate, reason, exact effect and
computed placement. **Start at word** aligns the effect's beginning to the word
start. **End at word** aligns its ending to the word start, useful for a riser.
Offsets can move the cue by up to five seconds. Clips crossing the narration
boundary are trimmed without shifting the narration.

Preview the chosen effect and adjust duration, gain, fades, offsets or enabled
state. Saving creates a new immutable revision. Review the current plan before
rendering. The mix attenuates effects during aligned speech intervals, preserves
the narration track and applies a headroom limiter with delay compensation.
Effect gain remains bounded below narration; the UI also exposes narration
gain, ducking and headroom settings.

The editable JSON includes exact source ranges, gain/fade settings, disabled
cues, speech intervals and provenance. The OTIO timeline preserves overlapping
effects as separate audio tracks with original WAV references. Gain, fades,
ducking and limiting are recorded as editorial metadata: recreate these in an
NLE that does not apply that metadata automatically. The rendered WAV applies
the complete mix.

Downloads check current plan revisions, audio/script/alignment hashes and all
enabled effect permissions. An edited plan or changed input invalidates prior
mix delivery. A script exported by Documentary Writer also retains its exact
documentary approval revision. Outputs stay in ignored local search storage.

## API and CLI

All API routes are authenticated and scoped to a case:

| Operation | Endpoint |
| --- | --- |
| List/register effects | `GET/POST /api/v1/cases/{case_id}/sounds` |
| Check alignment | `POST /api/v1/cases/{case_id}/acoustics/readiness` |
| List/queue analysis | `GET/POST /api/v1/cases/{case_id}/acoustics` |
| Get/update plan | `GET/PUT /api/v1/cases/{case_id}/acoustics/{plan_id}` |
| Queue mix | `POST /api/v1/cases/{case_id}/acoustics/{plan_id}/mix` |
| Download result | `GET /api/v1/cases/{case_id}/acoustics/{plan_id}/files/{artifact_id}` |

Analysis accepts `narration_asset_id`, `script_asset_id`, optional
`transcript_artifact_id`, `title`, `style`, `max_cues` and `auto_align`. Plan edits
accept cue settings and mix settings, plus `expected_revision`. Mix requests
also require `expected_revision`. API mutations start the existing durable
worker; CLI generation and mixing commands queue jobs for `worker` to process.

```bash
python -m app.services.targeted_search.cli case-sound-register CASE_ID EFFECT_ID \
  --category impact --tags low reveal --description "A restrained low impact"
python -m app.services.targeted_search.cli case-acoustic-readiness CASE_ID \
  NARRATION_ID SCRIPT_ID
python -m app.services.targeted_search.cli case-acoustic-analyze CASE_ID \
  NARRATION_ID SCRIPT_ID --max-cues 12
python -m app.services.targeted_search.cli worker --drain
python -m app.services.targeted_search.cli case-acoustic-list CASE_ID
python -m app.services.targeted_search.cli case-acoustic-get CASE_ID PLAN_ID
python -m app.services.targeted_search.cli case-acoustic-edit CASE_ID PLAN_ID \
  cue-settings.json --revision CURRENT_REVISION
python -m app.services.targeted_search.cli case-acoustic-mix CASE_ID PLAN_ID \
  --revision CURRENT_REVISION
python -m app.services.targeted_search.cli worker --drain
```

The mix job result contains the WAV, JSON and OTIO artifact IDs for delivery.
