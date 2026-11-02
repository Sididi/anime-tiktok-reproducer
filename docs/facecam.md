# Facecam

Facecam is a separate project mode for anime sources. Create it beside Duplicate on the ordinary script page. The confirmation opens an independent French project using Minimal (`zoomed`) and GPT. It keeps upstream source matching but starts with fresh editorial, recording, scheduling, and delivery state.

## Workflow and persisted data

Opening a Facecam does not start generation. Choose the writing model above the story section, then click “Analyser les histoires” to detect the stories present in its source. Model changes are saved before analysis can start. A single story is selected automatically; when several distinct arcs exist, choose one before the hook. Cards show the premise, progression, endpoint and omitted material. Alternative hooks on the same arc are not separate stories. Existing drafts are preserved: detection is explicitly started, and choosing a story resets the draft and its dependent results. Choices persist across reloads; failed detection is retryable.

Then choose/edit one of eight hooks and, optionally, one of eight opinions, generate, edit the full script, approve it for recording, record, clean up failed takes, review the complete recorded script, validate it to place scenes and prepare the audio preview, prepare publication, then process. Writing approval makes no scene-assignment or illustration calls. “Sans point de vue” clears the selected opinion; an empty opinion omits that block when generating and takes the story directly to the title reveal. This choice survives reloads and can be changed before regenerating. Retained original-dialogue markers cannot be removed in the text editor. New scripts end with **« Le nom de l’anime c’est : [TITRE] »**, using the project’s anime name. The reveal is editable like the rest of the narration: shorten the title, change its wording, or remove only the title to keep **« Le nom de l’anime c’est : »**. Autosave, approval and recording review preserve the edited wording; existing drafts are not rewritten.

The final title image starts at the first aligned word of the editable reveal block and lasts through the end of the narration, rounded to the 60 fps timeline. Its duration follows the recorded wording, pauses, cuts and voice speed; it adds no fixed second after a spoken reveal. Camera clips and anime illustrations stop at that start, including when the ending is split by cuts. If recording review removes the entire reveal, the previous one-second silent card remains as a fallback. Older prepared previews without `cover_end` are rebuilt using cached speech alignment before processing; this does not rewrite the script or require another paid writing/illustration call.

## Mandatory publication preparation

**Préparer la publication** requires generated metadata, an explicitly selected title, and a selected
episode thumbnail before processing. The API enforces the same checks, including for direct process,
ZIP export/download and social upload requests. Existing Facecams must complete this step too.

After publication preparation, **Passer au traitement** asks for confirmation when the current audio preview’s final duration is **strictly below 61 seconds**. This measurement includes silence cuts, voice speed and retained original dialogue. Cancel keeps the user on the Facecam page to adjust the recording/settings; confirm proceeds. Exactly 61 seconds needs no warning. Pending audio changes keep the button disabled until the new preview is ready.

Metadata uses the standard anime `MetadataService` prompt, the anime name and current script
(including retained original dialogue), French output, the project's model preset and its **light**
tier, with the same structured eight-title schema as faceless automation. Generation never silently
chooses the first title. The shared title picker and metadata editor support selecting and editing
the platform fields. Choices persist on disk; a changed script/anime or an outdated generation ID
cannot validate or overwrite current metadata. A failed generation preserves the previous choices;
a failed form save preserves the user's edits for retry. Metadata and thumbnail changes invalidate
the ZIP without rerunning audio or scene selection.

Thumbnail proposals use **zero paid API calls**. A bounded local pass samples at most 80 frames,
prioritizing retained montage shots and committed V4 alternatives, then spreading samples across
their associated episodes. It skips the usual episode opening/ending regions for the extra samples,
rejects fades/flat images, evaluates vertical crops for detail and exposure, and removes near-duplicates.
It proposes up to ten distinct images; scarce or unreadable sources can produce fewer. The pass stops
sampling after approximately one minute plus an in-flight decode. Successful proposals are cached.
This is an image-quality heuristic, not a semantic character/face recognition model.

**Sélecteur manuel** browses the same episodes by timestamp, timeline slider and native-frame steps.
It shows the complete image and vertical crop together; horizontal/vertical sliders adjust framing.
Selection extracts the original source frame and exports an RGB/sRGB JPEG at **1080 × 1920 (9:16),
under 2 MB**, without stretching. The ZIP includes `metadata/thumbnail.jpg`, `metadata.json` and
`metadata.html`. Source changes invalidate thumbnails; an app-verified audio-only remux preserves
them. The publication thumbnail is separate from the final anime-reveal card.

Facecam uploads reuse this image for YouTube and Facebook's existing thumbnail endpoints and host
an immutable copy for Instagram (immediate or scheduled) and long-range Facebook scheduling.
Scheduled Instagram explicitly opts in with `use_custom_cover`; standard-project covers and TikTok's
old thumbnail selector stay disabled. Facebook's optional thumbnail attempt is best effort: a refusal
is reported without failing or duplicating the successfully published/scheduled video. The VPS changes
must be deployed with the backend for scheduled Instagram and long-range Facebook covers.

Format reference: [YouTube's current Shorts thumbnail guidance](https://support.google.com/youtube/answer/72431?hl=en-GB-)
recommends 9:16 and supports custom uploads from desktop Studio on verified accounts.
The implementation uses JPEG for Instagram's `cover_url` path; see the
[Meta media reference](https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media/).
No live platform upload is necessary for the regression tests.

## Full-script recording review and deferred scene placement

After the final transcription of the edited take, **Relire le texte enregistré** displays the complete
script with the original-dialogue markers. Detected omissions/replacements underline the original
words in red; green inline buttons show the recognized wording. Clicking a suggestion accepts that
specific change. All suggestions begin pending: **unaccepted changes keep the original wording**.
The text is directly editable, including undetected additions/removals and the actual spoken closing
phrase. Raw-dialogue markers and section identity remain protected. Undo/redo is available.

Edits autosave a separate `recording_review` draft without changing the approved script or calling a
model. The draft and pending suggestions survive reloads. Saves carry the recording identity, document
revision and review version; stale tabs or replaced recordings cannot overwrite another review. A
failed save keeps the local text and blocks validation until saving succeeds. Even a perfect transcript
with zero differences needs explicit validation.

**Valider le texte et placer les scènes** commits the reviewed wording and starts one resumable job:
word assignment, synchronization with the recording, V4 illustration selection, then the mixed audio
preview. A corrected script receives a new approved revision; fact annotations are remapped, without
claiming a fresh fact check. A blank optional paragraph or omitted closing phrase introduces no invented
words. If a narrated passage now has fewer words than reference shots, only a spread of those shots is
retained before assigning its words.

`placement` records the validated revision/recording, stage and completion. Duplicate validation clicks,
HTTP retries, page reloads and concurrent workers reuse the same placement. Successful assignment
blocks are cached in `facecam/placements/<key>/assignment-*.json`; a later failure does not pay to
reassign those blocks. The committed V4 choice is stored alongside them in `candidates.json`.
Processing and JSX generation only read that choice: they cannot launch a search. Silence, speed and
music changes reuse the selection, adapting preview lengths locally when fewer slots fit. An
app-managed audio-only episode repair also preserves the visual selection. Other episode file
replacements remain rejected as stale. A new take
or an edited review requires explicit validation again. Interrupted jobs offer **Reprendre le placement
des scènes**, preserving completed work. Audio-preview failures preserve completed scene placement.
No real project recording or paid model call is needed by the workflow's regression/browser tests.

## Recording cleanup before transcription review

Every new MP4/MOV import opens **Nettoyer votre prise**, including when the same file is imported again. The editor becomes available after media normalization; initial speech recognition and AI analysis continue in the background. This first transcript is only used to locate possible failed takes. Script comparison, recording review, automatic previews and processing remain gated until **Valider les coupes et continuer** or **Garder toute la prise et continuer**. Existing recordings remain usable; **Modifier les coupes** opens the editor on demand.

The AI looks for substantial abandoned phrases followed by a nearby restart. **Reprise nette** is preselected only when the model and deterministic checks agree: multiple repeated words, enough speech, an interruption or explicit restart cue, and no unresolved ASR span in the removal. **À vérifier** remains unchecked and allows more ambiguous candidates. Script/transcription word differences, natural reformulations, rhetorical repetition and isolated hesitations are not reasons to cut. The prompt is `backend/prompts/facecam/recording_cuts.md`. Suggestions are advisory; ASR can miss repetitions, so a clean analysis does not prove a take has no fails. Failed analysis leaves manual editing available and offers a retry.

ASR can collapse a false start and its restart into one fluent sentence, stretching a word across both attempts. For cut review, words longer than 1.5 s trigger a local recognition pass with surrounding context. Real quiet gaps inside those words split the audio into independent recognition windows. Recovered words use the original recording clock and are cached in `cut_speech_timing.json`, separately from the immutable source timings used by protected silence editing. Recovered proposals always remain unchecked. Ordinary words need no extra recognition. `cut_analysis.json` records the raw model response, validated proposals, prompt hash and recovery windows. See the [comparison with two manual cuts in project 5058388c9ebd](facecam-cuts-5058388c9ebd-2026-10-24.md).

Each proposal has **Écouter** (play the original passage from its exact start to its end) and **Raccord** (start 1.5 s before the cut and audition the transition by skipping the passage, even if unchecked). The controls explain this distinction. The main player switches between **Original** and **Avec les coupes**. These browser auditions skip source intervals without rendering a new file; the rendered result is frame-exact, while interactive audition seeks depend on browser playback. Silence removal remains exclusively in the existing AutoEditor step. Scrolling vertically over the waveform zooms around the pointer, synchronizes the zoom slider and leaves the playhead in place. Horizontal/Shift scrolling remains available for navigation.

WaveSurfer.js supplies the waveform, zoom, time ruler and draggable regions. **Sélectionner une coupe** enables drag selection; existing edges can be resized, and selected bounds can be entered in seconds. **I** and **O** mark a beginning and end, Space controls playback outside native form controls, Delete deselects the active cut, and Ctrl/Cmd+Z undoes an edit. Timed transcript words also jump to their audio. **Tout garder** deselects all removals. Draft selections survive closing/reloading in this browser; validated cuts are persisted on the server. Reuploads get a fresh edit identity and reset these choices.

The server computes cached min/max audio peaks from normalized PCM using bounded chunks. The browser receives peaks and streams a compatible H.264/AAC preview instead of downloading and decoding the complete audio. Validating overlapping removals unions them and rounds inward to the 60 fps camera clock. One edit map cuts PCM at the same 48 kHz sample boundaries and the silent camera in a single FFmpeg pass. It never changes playback speed. Removing the entire take, malformed bounds and stale script/edit versions are rejected.

The immutable original and its source timings remain in the recording directory. Edited media, a browser review video, timings and downstream alignment caches live under `recordings/<source hash>/edits/<edit hash>/`. Actual edited audio is transcribed before comparing it to the script; keeping the whole take reuses its initial recognition. Identical edit maps reuse media and transcription. A failure preserves the previous recording and validated edit. Successful validation clears old preview/processing results, then the usual discrepancy review and protected silence editing operate on the edited take. Exports reference the edited camera and continue to include the untouched original recording.

## Prompter and rehearsal

The recording section includes **Lire au prompteur**. **Ouvrir le prompteur** saves any pending edits and opens a separate, reusable reading window at `/project/{id}/prompter`. It works before script approval for rehearsal; importing a recording still requires approval. Keep the editor on the main monitor and put the reading window on the teleprompter. Its defaults suit the Elgato Prompter's 1024×600 display: white text on black, 46 px text, 32 px/s scrolling, and a reading guide.

The editor controls play/pause, restart, passage selection, font size, speed and optional horizontal mirroring. A passage jump pauses at that passage. Changing font size pauses and returns to the beginning of the current passage. Settings persist in this browser. The reader has its own playback controls and keyboard shortcuts: Space toggles playback, Home resets, Up/Down adjust speed, PageUp/PageDown select a passage, and F requests full screen (F11 is the browser fallback). Scrolling with the mouse pauses playback. Reaching the end stops; reading never loops automatically. Hiding the reader's browser tab pauses it.

The reader displays the saved narration, including the editable closing phrase and anime title. Raw blocks become a colored **Extrait original · ne pas lire** cue; their dialogue is excluded. Removing the title in the editor also removes it from the prompter after saving. A saved text change pauses and resets the reader, including when a new story clears the draft. The two windows communicate within the same browser profile and origin using a project-specific BroadcastChannel. Reloading the editor reconnects to an open reader; a reader opened directly loads the saved script from the API. Opening requires a browser click so popup blocking can be reported. Failed saves close a newly reserved blank window instead of displaying an older draft.

### Validated local Linux display setup (2026-09-29)

Arch Linux / X11 / i3 recognizes the Elgato Prompter via DisplayLink and `evdi`. Both were already installed and running. The Prompter was detected but its X11 output was disabled. Enabling `DVI-I-2-1` at 1024×600 / 60.11 Hz, to the right of `DP-1-0`, produced legible text and smooth scrolling, confirmed through the glass. No additional software mirroring was needed.

The desktop configuration lives outside this repository:

- `~/.config/autorandr/docked-prompter-idle/` stores the normal two-screen layout with the Elgato physically connected but disabled. Automatic profile detection selects this resting state. The existing `docked` profile remains available when the Elgato is unplugged. Both main displays retain 240 Hz.
- `~/.config/autorandr/docked-prompter/` stores the three-screen layout for explicit `autorandr --load docked-prompter`. Profile `block` hooks prevent automatic detection from enabling the Elgato unnecessarily or overwriting a reader session. The existing i3 **Mod+F3** still runs automatic profile detection.
- `~/.config/autorandr/settings.ini` enables EDID matching so connector renumbering does not prevent profile detection. Existing startup and DRM hotplug hooks run autorandr.
- `~/.local/bin/facecam-prompter-session`, started by the user service `facecam-prompter.service` with the i3 session, watches native window events. The first window titled `Prompteur Facecam…` enables the Elgato, identified by EDID, and moves the reader to it in full screen. Closing the last reader removes only the display that the service added, recovering the previous layout without overwriting manual changes to other monitors. An Elgato already enabled before reading stays enabled afterward. The older `place-facecam-prompter` helper remains available for manual placement but its i3 rule has been replaced by the service.
- Backups are under `~/.local/state/elgato-prompter/` and `~/.local/state/display-audit/20260929-195855/`. The latter contains the detailed performance report and live integration checks.

Pausing playback or sending the project does not end the display session: close the reading **window** to restore the previous layout. Browser full screen can additionally hide browser chrome. The watcher needs neither a browser extension nor a backend shell endpoint. Live checks cover real Firefox placement/closure, multiple readers, title changes during reload, service crash recovery, preservation of an already active Elgato, and unchanged modes/positions of the other monitors. The user also confirmed the complete open/close cycle, including the return of desktop smoothness. Physical disconnection/reconnection and a complete reboot have not been tested as part of this setup.

A local GLX cadence probe on the external monitor measured approximately 83–86 iterations/s with the Elgato active and 153–166 with it disabled under the original Picom settings; the user also reported a clear improvement with the Elgato disabled. These are application timing measurements, not a physical measurement of panel refresh or proof of a universal X11 refresh cap. Picom's original settings and PRIME synchronization were retained after comparative tests.

Validation: six Prompter browser scenarios pass in Chromium and Firefox, alongside ten existing Facecam browser tests. They cover remote playback, settings, passage jumps, reopening/reconnection, draft saves and updates, keyboard controls, end-of-text stopping, popup/save failures, missing scripts, and clearing the reader when another story is selected. The production frontend build and lint on the new TypeScript files pass. Automatic i3 placement was verified with the existing Facecam project; the user confirmed the actual display and controls work on the Elgato.

## Narrative generation

“Garder toute l’histoire” is always offered as an optional alternative, including before analysis, after a failed analysis, and on existing projects. It retains every source scene without word trims and keeps all original dialogue. Its preliminary duration uses the source narration word count, the closing phrase with the anime title and native episode dialogue duration; the future hook, opinion, French rewrite and reading pace can change it. Above 70 seconds, the card recommends a shorter story while still allowing selection. This choice is never automatic, persists across reloads, and survives later analysis of the same source. Switching choices resets the draft and dependent results as usual. The complete story has no 65–70-second word budget or condensation retry: extraction and writing are instructed to preserve all narrative branches and events. The script still displays its estimated duration against the recommended 65–70 seconds.

The September 27 revision adds an editorial selection before source extraction and writing. Story detection reads the immutable source and selects a subset of clips for each real arc. It may trim the beginning or end of a narration clip using source ASR word indices (start inclusive, end exclusive). Invalid, reversed, duplicated or unknown scene references fail before persisting options. Raw dialogue can be omitted with an unrelated branch, but every retained raw clip stays whole. Scenes without word timings can only be retained whole or omitted.

Story proposals use open word bounds: `start_word: null` means the beginning of that source scene, and `end_word: null` means its exclusive word count. Missing bounds from prompt-only providers have the same meaning. Analysis resolves these into two explicit indices before persisting a partial clip; an entire clip is stored as `null/null`. For example, `null/3` keeps words 0–2 and `3/null` keeps word 3 through the end. Text extraction and source-to-episode timing use the same normalized range. Empty, reversed or out-of-range cuts are rejected rather than widened to the entire scene, and retained raw clips still require `null/null`.

Detection requests a structured response containing only editorial fields, with word counts and trimming eligibility supplied per scene. Local validation also runs when a provider does not enforce the schema. A malformed proposal receives up to two corrective retries containing the previous response and a diagnostic identifying the failing story/scene or field. Only a completely valid set of stories is persisted; exhausted retries preserve the previous draft and selection. Provider failures propagate without an editorial retry. Source fingerprints and document revisions are checked again before committing a result.

After selection, a model call builds a neutral factual brief in `facecam/narrative_brief.json` from **only the selected, trimmed source text**. Every retained non-raw scene must be covered exactly once, in order, within its body block. Missing, duplicated or crossed raw boundaries reject the brief before any draft changes. The brief is cached by selected source, block structure and extraction-prompt hash; changed references/prompts or a damaged cache trigger extraction again. Source fingerprints reject stale story selections.

Hooks, opinions and body writing receive this brief and the chosen story's scope rather than the source narration. All eight new hook choices are original proposals; no slot is reserved for translating the source opening. Older saved choices remain readable. Opinions receive the selected hook and focus on a supported narrative interest. The writer uses a concrete premise, protagonist, obstacle, escalating consequences and a payoff to the hook. It can omit descriptive gestures without dropping their useful illustration. Instructions live in `backend/prompts/facecam/{stories,brief,narration,hooks,opinions,script,oral_edit}.md`.

The October 23 revision makes the voice conversational, direct and occasionally slangy: concrete verbs, fewer abstract descriptions, brief consequences and connected sentences. Hooks usually aim for 10–22 words, without sacrificing factual nuance. After body writing, one separate oral-edit call receives the factual brief, uncertainties, selected story, fixed hook/opinion, raw boundaries and first draft. It tightens whole passages, removes repetition and checks wording against the facts. Both calls use the project's chosen big-model tier. Full-story mode preserves all branches and events in both passes; it only tightens wording. No source-creator transcripts are injected into either pass.

The October 24 revision follows the creator's requested construction method: identify the real key moments, put them in narrative order, give those moments striking wording and expressive hyperbole, then connect them into a flowing spoken story. Within each body block, the writer follows the events' actual chronology and the selected story's steps and ending; a source opening with a teaser followed by a flashback must not accidentally make that earlier event the conclusion. No fact may move across an original-dialogue boundary. This preparation stays inside the writing call; it adds no model call or response field. Both writing and oral editing explicitly encourage phrases such as “complètement fou” and “absolument dingue”, attached to a concrete event. Editing preserves or strengthens those accents instead of removing them simply to minimize word count. Hooks and opinions use the same voice. An empty opinion still allows expressive narration, while omitting the separate personal recommendation or concluding opinion. Full-story mode prioritizes the strongest moments without dropping secondary events or branches.

The October 29 narrative correction keeps a central desire, problem or contrast as the thread of
the presentation. Greater depth comes from a useful factual detail that clarifies that thread;
it does not require adding the next event. The earlier density instructions overemphasized new
events and capped setup at two sentences/a quarter of the body. Those caps and the instruction
to fill freed space with unused events have been removed. Editing preserves a good draft's
construction, useful transitions and the narrator's brief reactions while tightening weak
descriptions. Selected-story scope and protected dialogue boundaries still apply.
For original introductions, the writer selects the narrative thread before selecting facts,
including when a cached brief reads like an episode list. Premise and protagonist fields are
available for necessary context, alongside the detailed cited setup facts; uncertainties still
take precedence. Fresh extraction also retains that context among cited facts. An invented
example illustrates the approach without including creator transcripts in production prompts.

Narrative validation uses isolated snapshots of Dragon Hatching, a romance and a selected-story
case. The final original examples contain 232 and 236 words, approximately 70.3 and 71.4 seconds;
the selected-story example contains 197 words. Fixed text and near-future narration are retained.
The 155 targeted tests pass; prose quality was reviewed separately and is not guaranteed by those
tests. Inputs, outputs, intermediate trials and the review are under ignored
`backend/data/research/facecam-narrative-rebalance-2026-10-29/`. Research was reused and project
drafts were not replaced. See the [detailed review](facecam-original-2026-10-29.md) for limits.

Both Facecam modes now tell the body's actions, discoveries, decisions and consequences primarily
in French **futur proche** (`va/vont + infinitive`). The shared `narrative_tense.md` instructions are
included in both writing and editing calls, so condensation keeps this tense. Present tense remains
natural for identities, lasting states and world rules; earlier events keep their appropriate past
tense, while hypotheses and risks preserve their grammar and uncertainty. Fixed hooks, opinions,
original audio and the title-reveal phrase keep their wording; hook suggestions can use any suitable
tense. This is a generation instruction, without mechanical conversion of existing drafts or any
additional model call. Factual research and brief extraction retain neutral wording.
Fact checking treats this tense as storytelling rather than evidence of uncertainty, while still
checking the event, chronology and modality. Suggested corrections retain an appropriate existing tense.

Validation: 155 targeted Facecam tests pass. Two small fictional live-model cases exercise writing
and editing with the GPT preset for a source story containing a protected dialogue boundary, and
the Gemini preset for an original introduction. Both retain near-future action sequences, present
states/world rules, past backstory and conditional risks after editing. Prompts and outputs are
under ignored `backend/data/research/facecam-tense-2026-10-29/`; no user project was regenerated.

Expressive emphasis can intensify how an event is told; it does not change a power's capabilities, quantities, chronology or uncertainty. The factual brief remains neutral. Both fact-checking stages distinguish an expressive judgment (“ce pouvoir est complètement fou”) from a factual assertion (“ce pouvoir détruit une planète”); the latter still needs evidence. This editorial update supersedes the October 23 prompts' blanket discouragement of intensifiers.

October 24 validation: 170 targeted tests pass. Live generation used three frozen Facecam briefs with their configured GPT/Gemini presets, three sets of eight hooks, one set of eight opinions, and two full-story examples containing a protected dialogue boundary. Editorial review confirms that expressive phrasing survives the final rewrite and the teaser/flashback case reaches its chosen endpoint; some repetition remains possible. The full-story examples keep their secondary branch and conditional risk. Inputs, prompt snapshots, early iterations and final outputs are retained under ignored `backend/data/research/facecam-key-moments-2026-10-24/evaluation/`, with `review-summary.json` recording the checks. No extra production call was added by this revision.

This normally adds one detection call per source analysis (up to three if correction is needed) and one cached brief extraction per selected clip set. Both use the configured big-model tier. These are editorial extraction calls, not fact checks: source errors can survive into the brief. The existing researched check still runs after generation. Structural validation does not guarantee correct arc detection, good prose or perfect factual accuracy.

Scripts based on a detected arc target 65–70 seconds including retained dialogue and the spoken title reveal. The initial estimate uses 3.35 spoken words/second, including normal pauses. The body budget subtracts the selected hook, optional opinion and closing phrase with the anime title from the total word budget, with a floor of zero. This budget is advisory: even a long hook/opinion or an overlong body is accepted for manual condensation. `min_words` is not a floor to fill with extra descriptions. Generation now makes two body calls: writing, then exactly one oral edit, regardless of length. There is no duration-based retry loop. The first response is archived before validation or revision checks; after editing, the same archive keeps `draft_response` and the latest `response`. Both stages must retain the exact ordered body IDs and nonempty text. A failed edit or concurrent manual change preserves the existing saved script; a stale revision detected after writing skips the editing call. A valid final result becomes an editable draft with its estimated duration; duration does not block approval. A clear, complete story may finish below the indicative budget. The prepared preview reports the actual duration including the card. A timed recording is still needed to establish whether a video exceeds one minute.

Each returned script response is saved atomically under `facecam/generations/` before validation or draft persistence, alongside its input revision, story fingerprint, hook and opinion. A malformed response or concurrent-edit rejection therefore remains recoverable locally without another generation call; recovery must still validate the block layout and current revision. Older rejected responses from before this archive existed are not recreated retroactively.

After recording-review validation, scene assignment physically filters and trims the matched episode clips. Word timestamps on the original TikTok clock map proportionally into each confirmed episode interval; the export later snaps these bounds to source frames. This assumes the existing match's linear time mapping, not a fresh visual cut detector. A cut is therefore as accurate as the source ASR and match. Every output scene is reindexed with a contiguous provisional timeline, while `montage_map` retains original scene IDs, source bounds, episode bounds and cut word indices. The immutable `reference*.json` files are not rewritten. Selected clips keep their original relative order. A single sentence may span several retained shots.

Word assignment keeps scene identity and order in application code. The model only proposes the `N − 1` word boundaries between `N` consecutive narration shots; it cannot reorder, omit or invent scene IDs. Boundaries must be strictly increasing integers, cover the complete approved text and leave at least one word per shot. Original-dialogue markers separate independent assignments. A single shot (or exactly one word per shot) needs no model call. Invalid structured responses receive up to two corrective retries with the validation diagnostic. Persistent failure leaves the draft and pipeline untouched; it never substitutes an arbitrary duration-based split. A recorded passage with fewer words than shots retains a spread of its existing shots before assignment; it is never padded with invented speech. Structural checks preserve words and shots but do not guarantee perfect semantic correspondence.

Hook/outro illustrations reuse retained footage rather than extending into an excluded event. Automatic gap filling is disabled for these editorial selections so it cannot silently undo a cut; the continuous camera track covers narration when an anime illustration runs short at the playback speed floor. Original dialogue retains native timing. Older drafts without a selected story keep their existing montage behavior.

The research method, observations and implementation tradeoffs are documented in [the French corpus analysis](facecam-narration-analysis-2026-09-26.md). Research media/transcripts remain under ignored `backend/data/research/facecam-2026-09-26/`; production does not retrieve or paste these creators' scripts.

The [October 23 oral-style analysis](facecam-oral-style-2026-10-23.md) examines the latest 15 accessible theomcx publications, with a fresh download, two local ASR passes and comparisons on three existing Facecam briefs. Its media, transcripts and generation samples are under ignored `backend/data/research/facecam-2026-10-23/`. Existing project scripts are not regenerated by this change; new hooks/opinions and script generations use the new instructions.

`facecam/state.json` stores revisioned drafts, choices, research citations, approvals, word assignments, recording hashes, the silence-cut map, alignment references, preview identity, and VPS delivery status. `facecam/reference*.json` retain the original script and matches even when final recording validation creates extra hook/outro pipeline scenes. Research runs automatically after generation and otherwise only on request. Contradictions and uncertainty are advisory. Editing a flagged passage removes only its annotation. Untouched annotations keep their citations and follow text offsets during editing, autosave and reload, including across original-dialogue markers. Editor transactions preserve the exact occurrence of repeated quotes; the server validates submitted anchors against existing finding IDs and the saved text. `findings_revision` tracks anchor positions separately from `fact_revision`, which remains the revision actually checked: editing never claims a fresh fact check. Legacy hidden findings are recovered only when their unchanged quote occurs exactly once. Failed research never counts as a successful check.

Fact checks also receive the actual library subtitle dialogue from the first to the last selected matched interval in each episode, padded by 30 seconds on both sides (all original matches for legacy drafts). Dialogue between selected shots is retained: scenes between 02:00 and 06:00 supply subtitles from 01:30 to 06:30. One text track is selected (French preferred, then English, then another available language). Episode names, source timestamps, original scene anchors, and complete overlapping cues go to both research and annotation steps. The panel exposes this snapshot and allows findings to cite individual dialogue excerpts. Missing/image-only subtitles and context-budget limits are disclosed. Dialogue is not proof of an unseen visual detail, and absence from an excerpt is not treated as a contradiction. Existing drafts are not automatically rechecked after this upgrade.

One frame-quantized cut map drives both silent camera clips and narration. Original-dialogue gaps are inserted into this common timeline. Human speech keeps its original speed by default and never passes through the TTS voice alteration stage. Replacing the recording or editing narration invalidates dependent results. Changing music requires a new mixed preview. Processing/export verifies the current revision and settings.

Source-word alignment runs on the uncut recording and is cached independently of silence settings. Long pauses split this alignment into smaller speech runs when adjacent words match in both the script and the recording transcript. This prevents the aligner from extending a word across an entire pause. Scene fragments can span runs; their words, scene order and original-dialogue boundaries are retained. Unmatched words or unresolved speech prevent a split. The existing lexical coverage and word-duration checks remain mandatory, and failed alignment reports are saved beside the cache without publishing invalid timestamps.

Pause anchors and alignment windows recognize hyphenated/spaced equivalents such as `Là-bas` / `Là bas` and `grand-père` / `grand père`. Every component must match; partial compounds and pauses inside a compound cannot establish a split. This changes only the timing anchors, preserving the script's spelling and scene assignments. On October 25, 2026, replaying project `7ae4b8126a0a` reproduced a 4.422 s alignment for `monde.` across the pause before `Là-bas`; recognizing the equivalent spelling reduced that word to 0.160 s and passed the existing quality checks with all 66 scenes and 247 lexical script words retained. Before/after reports are under ignored `backend/data/research/facecam-alignment-7ae4b8126a0a/`.

The recording section exposes **Réglages AutoEditor**: silence threshold, minimum pause, and margins before/after speech. The fast defaults are **−26 dB / 120 ms / 30 ms / 50 ms**. Pressing **Préparer l’aperçu audio** saves those settings for the project and rebuilds the voice, camera cuts, alignment, and mixed preview when they change. Script approval and recording review remain valid. Pending changes disable processing; resetting the sliders restores the fast defaults. Settings are applied and persisted on preview preparation, not on each slider movement.

**Vitesse de la voix** uses the same range as `/script`: 0.90×–1.50×, in 0.01 increments, default **1.00×**. At exactly 1.00×, the speed stage returns the existing audio/camera paths without invoking a filter, re-encoding, copying or even probing them; the speed setting itself does not invalidate existing 1× audio. Other speeds use the shared pitch-preserving `AudioSpeedService` after protected silence cuts and before word alignment. The measured audio duration ratio drives the silent camera retiming and its cut map; the exported `sources/facecam.mp4` uses that retimed master. Original dialogue and music retain their normal speed; the title image follows the retimed spoken reveal. Resetting to 1.00× rebuilds from the original recording, never from a previously accelerated file.

Facecam uses FFmpeg silence detection rather than the TTS auto-editor profile. Source word timings are cached in the recording's `speech_timing.json`, independently of script edits and silence settings. Cuts always exclude each detected word plus 30 ms before/50 ms after it, as well as unresolved speech spans. Frame rounding expands speech protection, and a final partial audio frame is padded instead of truncated. A runtime check rejects a cut that intersects those protected intervals. The playback reconstruction preserves the first word's lead-in and uses identical sample boundaries on both sides of scene joins. The camera receives the same edit map. Protection depends on the source recognizer's timing accuracy; the forced script alignment alone is not used as proof that a cut is safe.

Subtitle presentation uses the final playback scene bounds in both Facecam and faceless exports. At the beginning of a continuous narration passage, the first caption covers a lead-in of at most 500 ms (the existing subtitle silence threshold). A passage beginning at zero therefore displays its opening caption on the first frame, while one following raw dialogue starts no earlier than the narration boundary. Raw scenes, scenes without renderable words, and timeline gaps delimit passages; ordinary shot changes do not. Longer initial silences retain the first word's timing. This changes only caption display, preserving word alignment, audio, camera cuts, and source-dialogue subtitle timings. The combined SRT and Premiere MOGRT timing SRT use the same rule. Existing exports need processing again and a new import to receive it.

Prepared audio stores its settings and an algorithm-version signature; old previews cannot be reused for different settings, a different recording, or an older algorithm. Rebuilds use separate directories to preserve prior files. `cut_report.json` records retained words, removed time, and cut count. On October 8, 2026, project `ba1a7c09ed83` was checked with four thresholds (−30/−28/−26/−24 dB): the chosen −26 dB version retains all 221 source word intervals and their guards, removes 13.48 s from the 76.928 s recording in 26 cuts, and produces 63.45 s of voice (64.45 s including the final card, versus approximately 71.13 s previously). Retranscription finds no deleted words; differences are spelling/homophone variants. The source-word analysis and comparison variants are under ignored `backend/data/research/facecam-autoeditor-ba1a7c09ed83/`.

The AniList identity and cover remain under `facecam/`, outside `output/`. Processing never stops for a missing cover (see below). The final card is 1080×1920 with a readable title, occupies V3 from the first spoken reveal word to the end of the narration at 60 fps, replacing the camera for that entire interval.

Cover lookup searches parenthesized aliases as well as the full library title (for example `Kill Ao (Kill Blue)`), after removing release tags (`S01 1080p WEBRip x265-GROUP`, `[...]`) and splitting `A | B` aliases. If those searches yield no exact identity, up to four longer words from the main title are searched to discover candidates despite differences in word spacing. Results are deduplicated by AniList ID; a partial GraphQL response cannot trigger a selection, and HTTP 429/5xx responses are retried following `Retry-After`.

Identity is resolved in this order, and processing never fails on it:

1. **Series memory.** `data/facecam_cover_identities.json` maps `library_type:series_id` to the AniList ID chosen for any project of that series (automatically or in Facecam), or to a shared copy of an uploaded image under `data/facecam_covers/`. Covers chosen before this registry existed are found in other projects' `facecam/state.json`.
2. **Exact title.** A full title/alias equals an AniList title or synonym after ignoring case, accents, punctuation, `_`/`-`, spacing, `wo`/`o` and long-vowel romanization (`Shoujo`/`Shojo`, `17_26`/`17-26`), word by word so roman numerals stay distinct (`Monogatari II` ≠ `Monogatari`); catalog suffixes `(TV)`/`(2018)` are ignored so remakes are candidates. Discovery terms never establish identity, so `Kill Ao` never equals `Kill Ao 2nd Season`. Several exact entries (remakes, OVAs, films with the same name) are ranked: a match on the library's own name beats one on its parenthesized alias (`Yamada-kun to Lv999 (My Love Story)` ≠ `Ore Monogatari!!`), main title beats synonym, then distance to the library episode count (Gegege no Kitarou 97 eps → 2018), literal spelling, non-special format, AniList popularity.
3. **Constrained judge.** With no exact title, the light LLM receives the library name, episode count, sample episode file names and the candidates; its JSON schema only allows a listed ID or `0` (none).
4. **Anime frame.** Otherwise (no candidate, AniList down, judge refusal) the most detailed frame among the best-matched source shots becomes the cover (`cover.frame = true`). It is never remembered for the series and the AniList lookup is retried at the next processing; the Facecam page shows a notice and still allows choosing or uploading.

When the selected AniList entry spells the same title differently (`Tatsuki Fujimoto 17-26` for the library's `17_26`), the card shows the catalog spelling; otherwise it shows the cleaned library name. Existing selected or uploaded covers remain cached.

Validation on 2026-10-31 against the 401 distinct library names: 352 unique exact titles, 15 ranked, 34 without exact title. Through the full pipeline the hard 49 gave the expected work for every ranked case after fixing `II` and `(TV)` handling; the judge resolved 22 of 34 (e.g. `Ame To Kimi` → `Ame to Kimi to`, `Shimoneta` → its full title, `Gokudolls` → `Back Street Girls: Gokudols`) and returned none for 12 obscure or trailer folders (anime frame), with no wrong pick once adult entries are excluded.

Catalog covers are small (AniList and Kitsu both top out around 460×690), so a cover smaller than the 940×1420 card box is upscaled 4x once with Real-ESRGAN `x4plus_anime_6B` and cached as `facecam/cover_hd-<fingerprint>.jpg`; the card then fills the box. Inference runs on CPU in 128 px padded tiles (≈13 s, ≈1.1 GB peak RSS for a 460×690 cover; whole-image inference peaked at 4.6 GB) so it never takes VRAM from matching or encoding. The weights are downloaded on first use to `data/models/realesrgan/` and checked against a pinned SHA-256. If upscaling fails, a plain Lanczos resize is used.

## Original anime introduction

**Présentation originale** can also be selected immediately under **Choisir l’histoire à raconter**,
without analyzing source stories or generating an initial script. Selecting the option makes no
LLM calls. The first request for hook/opinion suggestions researches and caches the anime brief;
the first script reuses it. A directly typed hook also works, and a failed first draft retains the
brief for retry. Selecting a source story clears this original context.

After generating a script, **Recréer de zéro** preserves the current editor's exact hook and optional
opinion and researches the anime to write a fresh introduction. It targets 60–75 seconds, focuses on
the premise and early developments without major spoilers, and uses continuous narration without original-dialogue
inserts. The previous script, selected story, source narration and scene descriptions never enter its
research/writing calls. Available narration shots remain illustrations assigned after writing.

Research seeks the work's defining premise, supported motivations, rules, limits and personal stakes.
Early events can demonstrate them; the brief must not become an episode-by-episode outline.
Writing and candidates can use `premise` and `protagonist` for introductory context, and `setup_facts`
for detailed rules/events, with `uncertainties` taking precedence. Previously only `setup_facts`
was permitted, which could omit essential context present only in the summary fields of cached
briefs. Fresh briefs now also retain that essential context among their cited facts. The
`narrative_engine` guides construction without serving as evidence for additional claims.
There is no setup-length quota or automatic addition of events after a cut. The writer is explicitly
instructed to reconstruct a coherent introduction even when an older cached brief lists episodes.
Major twists and late resolutions stay excluded; exhausting the facts warrants a shorter script.
The call count and duration target are unchanged. Initial cached briefs are still reused without
automatic paid refresh; **Recréer de zéro** performs fresh research using these instructions.

`generation_mode: original` persists through edits, approval and reloads; old states default to `story`.
New proposals and regenerations stay in the original mode until a source story is selected again.
Sources are exposed beside the script. Previous state, research and generation responses are archived
under `facecam/generations/original-*.json`; failed or stale generations preserve the current draft.
Changing the script invalidates approval and prepared output. The usual advisory fact check runs afterward.

Both stages of **Vérifier les faits** (web research and structured annotations) use the dedicated
`fact_check` entry in `config/llm/config.yaml`: `google/gemini-3.8-flash`, low reasoning, with a
4,096-token output cap. They previously used the writing preset's big model. This also applies to
automatic checks after generation and to projects using Grok, whose existing big/light preset
entries are identical. There is no automatic fallback to the writing model on failure. Web search
still runs and may incur its own costs; writing and initial anime research keep their existing models.

The [50-publication analysis and implementation notes](facecam-original-2026-10-29.md) explain the
editorial approach, scope, provenance and limitations. The [reviewed inventory](facecam-original-corpus-2026-10-29.csv)
links every publication; creator transcripts remain local research artifacts and never enter production prompts.

## Editor delivery

Facecam exports `SPM_facecam_<anime>_<project_id>.zip` with ZIP64, streamed files, complete episodes, original recording, silent normalized camera, real narration, music, cover/title card, presets, expanded subtitle MOGRTs/timings, JSX, Windows launcher, script, and French instructions. Media references are checked before delivery. It bypasses shared-source prewarming, folder synchronization and Premiere Link. Only the ZIP is uploaded or updated; editor files are preserved.

Premiere exports provide **V1 · Illustrations 100 %** (formerly V4).
V1 presents **the current shot plus up to two alternatives per narration scene**. It deliberately
shortens the first shot and divides that scene's available timeline interval between the proposals.
Every proposal plays at 100%; a short source leaves a gap. Slots never cross the scene's end, the
next scene's start or the final card. Scenes too short for 0.5-second previews receive fewer choices.
Original-dialogue picture spans remain protected. Keep the desired clip, remove the others and extend
it in Premiere: video-only subclips retain access to the complete source episode.

The search covers **only local episodes already present in the project's immutable matches**. It
first reads the prepared narration's final timing and skips raw dialogue, outro and scenes with no
room for another 0.5-second preview. One text request uses the complete script, neighboring narration
and local subtitles to locate a promising 12-second window for each eligible scene. Existing matches
provide fallback locations, including when subtitles are absent. This can find shots outside the
project's original matches, without indexing entire episodes.

Only the selected windows are decoded, by seeking into the original local videos. Local cuts delimit
frame-indexed candidates. At most two subsequent vision requests inspect contact sheets containing
two actual frames per candidate and choose up to two alternatives per scene. The model cannot supply
timestamps: selected IDs resolve to measured native source frames. Overlapping original shots are
removed before vision; unknown IDs and incorrect scene assignments are rejected. Images must match
the narration's characters, appearance and action; subtitles alone are not visual evidence.

Work is bounded to **three model requests, 24 windows of 12 seconds, 96 proposed shots and 32 eligible
scenes** per selection. If there are more eligible scenes, those with room for more alternatives take
priority, then narration order. CPU decoding uses two workers. The default active-work deadline is
180 seconds (`ATR_FACECAM_ILLUSTRATION_TIMEOUT_SECONDS`), with at most 60 seconds for each model request.
Failures stop explicitly; there are no automatic paid retries. This targeted approach may miss suitable
shots elsewhere in an episode and does not claim an exhaustive visual understanding of it.

`facecam_illustrations` in `config/llm/config.yaml` independently selects **Gemini 3.8 Flash**, with
minimal reasoning. Request output limits are 1,536 tokens for localization and 3,072 for visual
selection. Local window images are shared between projects in `backend/data/cache/facecam_windows/`.
Selections, raw model responses and reports live under `facecam/targeted_selection/<fingerprint>/`.
Source identity, subtitle, narration, eligible-slot, prompt or model changes invalidate the relevant
cache. Repeating processing with unchanged inputs reuses completed results without model calls.
Original source matches are not modified. The reviewed script supplies the authoritative narration
assignment. Legacy projects must explicitly validate the recording review before new processing;
existing assignments can be reused when their words are unchanged.

**The entire targeted search shares a $0.15 spending ceiling**, including localization, vision and
manual retries for the same narration/source context. It can be lowered with
`ATR_FACECAM_ILLUSTRATION_BUDGET_USD` but cannot be configured above $0.15. Prompt, model and search-limit
changes do not reset its ledger, stored separately in `facecam/targeted_budgets/<context>/usage.json`.
The older per-episode and selection budget settings apply only to the retired exhaustive index path;
normal Facecam processing no longer invokes that path.

Before each request the application checks current OpenRouter rates, caps provider prices and reserves
conservative spending using UTF-8 byte counts, bounded image allowances and maximum output tokens.
Reported actual costs replace reservations afterward; unknown costs and timeouts keep their reservation.
The next request is blocked if its reservation would exceed the shared ceiling. This is an application
spending guard, not a provider-side hard billing guarantee. Completed stages survive failures so that
retries do not repeat successful requests. No fallback to the writing model is used.

V1 receives the foreground effects; V2 and V4 stay empty. V3 holds the camera and final card,
V5 the subtitles, V6–V7 optional overlays. The blurred background, white border and automatic
retimed montage are no longer created. New candidates preserve their episode's own frame rate,
including true 24 fps versus 23.976 fps. Export packages the exact selection embedded in the JSX,
without another LLM call, and rejects media outside the project's allowed episodes. Existing exports
need processing/JSX regeneration to receive this track layout.

Visual descriptions are sampled evidence, not an exhaustive understanding of every motion between
frames. Automated tests cover placement, cache invalidation, malformed selections, budget accounting,
source clocks and packaging. A real Premiere import on Windows remains necessary to validate the
editor interaction in Adobe itself.

The previous exhaustive implementation was retired after project `7ae4b8126a0a` required 1,232 shot
analyses and 65 requests for **$0.93384075**. It proposed 127 alternatives, of which 93 did not fit the
final timeline. The targeted implementation filters those scenes before any paid work and never
calls the full-episode visual index. Its regression tests include aggregate cost accounting, retry
reservations, no-reset ledgers, deadline enforcement and real 24 fps random-access decoding.

Targeted evaluation on October 29, 2026, on the same project and three allowed episodes: the final
configuration made **3 model requests for $0.03242775 in 65.24 seconds**, including uncached local
window decoding. It reviewed 44 proposed shots in 24 windows and selected **20 alternatives for 17
scenes**. All 20 fit V4; none was discarded during placement, and all 66 original clips were retained.
A cached selection plus placement took 1.57 seconds with no model request. Both development trials
together cost **$0.06498225** in the same ledger; an initial HTTP 404 routing rejection did not reach a
model and is recorded separately at zero cost. Eight final proposals were checked against their actual
sampled images; this is a spot check, not a full editorial-quality evaluation. The stricter visual
prompt rejects indirect theme associations such as a skill-status card for a narrated sword swing.
The targeted search can therefore offer fewer alternatives than the previous exhaustive selection.
The benchmark left the delivered JSX, ZIP and project state intact. Evidence is under the ignored
local directory `backend/data/research/facecam-targeted-7ae4b8126a0a/`. The relevant backend regression
suite passed 332 tests; Premiere interaction still requires the Windows acceptance check below.

Install the independent [Facecam Import extension](../premiere-extension/facecam-import/README.md). Build its distributable with `python scripts/package_facecam_extension.py`. Extracted projects must work without shared source folders or the primary extension. Premiere and its Facecam Import panel must be open.

Only the individual project folder receives an `anyone` / `writer` permission with `allowFileDiscovery=false`. Parent permissions are never modified. This is edit/delete/upload access within the project folder.

The VPS stores delivery tasks in its persistent data volume under `facecam/`. Its Discord interaction handler routes persisted task IDs after restart. Confirmation checks MP4s only in the registered folder root, rejects zero/multiple/empty files privately, renames the sole file `output.mp4`, records completion before updating Discord, and disables the button. Per-project locks serialize clicks; retries recover partial failures. Re-export retains completion. “Demander un nouveau montage” archives the old file under `montages_precedents` and reopens the task.

The local project manager synchronizes delivery readiness from the VPS. Completion does not publish or schedule anything. Account YAML `supported_types` requires explicit `facecam` opt-in; anime-only accounts remain incompatible. Source library operations still use `anime`.

## Configuration and rollout

Deploy `server/` first, preserving its data volume. Set these on the VPS (never in the editor extension):

- `ATR_FACECAM_DRIVE_CLIENT_ID`
- `ATR_FACECAM_DRIVE_CLIENT_SECRET`
- `ATR_FACECAM_DRIVE_REFRESH_TOKEN`
- Optional `ATR_DISCORD_FACECAM_CHANNEL_ID`

The Drive credentials must access the project folders. Without an explicit channel ID, the bot finds or creates `montage-facecam` in the configured guild and needs the corresponding Discord permissions. Authenticated `GET /api/internal/facecam/health` verifies Drive access and resolves the channel before any package delivery.

Then update the local backend/frontend and distribute Facecam Import. Both active/example GPT presets use Astra with low reasoning for the big tier and Luna for the light tier. Ordinary generation uses Chat Completions. Research uses Responses with `openrouter:web_search`, preserving returned citations. The explicit Astra output budget avoids reserving the provider's entire maximum response. Insufficient OpenRouter credits surface as retryable generation errors.

## Validation

Automated tests cover revision conflicts, independent copies, raw boundaries (including first/last scenes), exact ordered word assignment, advisory research failures, recording differences, actual offset-stream normalization, cut-map composition, ZIP contents, folder-scoped ACLs, concurrent Discord confirmations, restart/retry recovery, re-export and reopening. Browser tests cover both eight-choice steps, editing/autosave/reload, preserved raw markers, annotation expiry and manual rechecking.

Story-selection tests additionally cover single/multiple arcs, source fingerprints, concurrent edits, failed detection, changing an arc, cuts at both ends of a scene, rejection of raw-word cuts and invalid indices, physical scene removal, coordinate conversion, source immutability, word-budget retries, and reindexed voice/montage correspondence. Browser tests cover explicit detection after model selection, the one-story shortcut, required multi-story choice, retry and preservation of old drafts until a new choice.

The [atomezen comparison](facecam-atomezen-condensation-2026-09-27.md) motivated this rework. A live evaluation against the configured model on September 27 was blocked before generation by OpenRouter HTTP 402 (API key credit limit). This is not a completed model-quality evaluation; deterministic and mocked-model tests do not establish the quality of real story detection.

September 27 checks: 64 Facecam backend tests, 41 related export/timing/duplication regression tests, 10 browser tests, and the production frontend build pass. The duplication tests require `ATR_DUPLICATE_SAME_LANGUAGE_SPACING_ENABLED=true`; the local default disables that unrelated rule, so those tests were rerun with the flag enabled only for the test process. The real source project was also projected deterministically through the internal cut in scene 42: its selected text ends after `de Goblins`, at source time 70.415 s, before the receptionist's refusal. Script, transcription and matches of `d038484047ac` remain unchanged. Research evidence is under ignored `backend/data/research/facecam-stories-2026-09-27/`.

Acceptance on Windows still requires importing an extracted package into Premiere 2025+ and checking camera visibility, subtitle stacking, raw dialogue, one-frame A/V tolerance, and the title image from the first spoken reveal word through the end of the narration. This cannot be established by Linux unit tests alone.

### Rollout verification — 2026-09-16

VPS backend deployed at `/opt/tiktok/server`, with the existing persistent volume retained. The prior image is tagged `tiktok-server:before-facecam-20260916`; the private backup is under `server/.deploy-backups/facecam-20260916/`. Channel `1549888612722671696` is configured explicitly. Drive authentication and direct channel access pass the deployed preflight.

Verified locally: 267 backend regression tests; 22 Facecam tests; ordinary duplication tests with the spacing feature enabled; Facecam browser creation/editing/selection tests and ordinary script-repair browser tests; production frontend build. The new VPS delivery tests pass. A full VPS suite also exposed one unrelated existing Premiere Link test whose fixed August 2026 expiry date is now in the past (`test_post_while_disconnected_stores_and_marks_waiting`).

Live checks passed for Astra's eight hooks/eight opinions, script writing and exact-word scene assignment, structured metadata, researched citations, and Luna generation. A temporary Drive folder confirmed repeatable writer ACL creation with discovery disabled and unchanged parent permissions; it was removed afterward. No test delivery messages or publication jobs were created.
