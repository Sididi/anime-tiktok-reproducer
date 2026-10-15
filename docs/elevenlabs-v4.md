# ElevenLabs v4 integration

Verified on 2026-09-29 against the official documentation, the live
[OpenAPI schema](https://api.elevenlabs.io/openapi.json), `GET /v1/models`, and
small real generations using the configured Sébastien voice.

## API contract

Use `model_id: "eleven_v4"` with `POST /v1/text-to-dialogue`. Single-speaker
narration is one `inputs` entry containing `text` and `voice_id`.
`output_format` remains a query parameter; our existing `pcm_48000` output works.

| Parameter | Contract and implementation |
| --- | --- |
| `seed` | Integer 0–4294967295, including zero. Best-effort repeatability, not guaranteed identical audio. Reject invalid values before sending. |
| `settings.stability` | 0–1; use the voice's configured stability, default 0.5. |
| `settings.similarity` | 0–1; map the existing YAML `similarity_boost` key, default 0.75. |
| `previous_text` | At most 100 characters; send the end of the preceding normalized block when its audio request ID is unavailable. |
| `future_text` | At most 100 characters; send the beginning of the next normalized block. The Python helper calls this `next_text` and maps it to `future_text` for v4. |
| `previous_request_ids` | At most three, chronological order, nearest last. Use recent IDs returned by this run. |
| `next_request_ids` | At most three, nearest first; supported by the helper for regenerating a middle block with existing following audio. |
| `language_code` | Send the prepared script's language (`fr`, `en`, etc.). |

Sources: [Create dialogue](https://elevenlabs.io/docs/api-reference/text-to-dialogue/convert)
and the live OpenAPI schema linked above. Continuity fields are marked
model-dependent in the schema; the live v4 checks below confirm request acceptance.

V4 uses only Stability and Similarity voice settings. Do not send legacy `style`,
`speed`, or `use_speaker_boost` fields. Audio tags can guide delivery, and SSML is
unsupported. `eleven_v4_turbo` is the realtime variant; this offline narration
integration uses `eleven_v4` and rejects Turbo with a clear error.
Sources: [Eleven v4](https://elevenlabs.io/docs/overview/capabilities/text-to-speech/eleven-v4),
[models and endpoints](https://elevenlabs.io/docs/overview/models).

The model catalogue advertises 10,000 characters, but the Dialogue endpoint
recommends at most 2,000 total input characters for reliable generation. Our
helper enforces 2,000 including audio tags. The existing segmenter uses a
550-character target for both v3 and v4, with an 800-character hard cap;
opting a v3 voice into v4 therefore preserves its segment layout.
Sources: [model limits](https://elevenlabs.io/docs/overview/models#character-limits),
[Dialogue quickstart](https://elevenlabs.io/docs/eleven-api/guides/cookbooks/text-to-dialogue).

## Generation policy

Each Automate run chooses one random unsigned 32-bit seed, persists it in
`Project.elevenlabs_seed`, and reuses it for all blocks. Starting another run
chooses a new seed, as before. A seed alone cannot reproduce a generation:
text, settings, model, voice, language, and context also matter.

Blocks are generated sequentially. The first has following text context only;
intermediate blocks use up to three preceding audio IDs plus following text;
the last has no following context. We choose audio context over text on each
side rather than submitting both. Missing response IDs clear the audio context
chain so the next block falls back to its immediate predecessor's text.

Neighbor text comes from the normalized spoken script. The delivery tag
`[TikTok narrator, rapid]` (or the voice's `audio_control_tags` override) is added only to the block being generated,
not its neighbors or stored transcripts. Existing post-generation speed,
silence processing, PCM-to-WAV wrapping, and merging remain in place.

Each run writes `tts_manifest.json` beside its script and audio parts. It records
each part's response request ID and synthesis arguments (including the seed,
model, voice, and context), without credentials. These are helper arguments:
the service maps settings and clips text context when constructing the HTTP body.
This preserves the information needed for future block regeneration. There is
no new single-block regeneration UI or endpoint in this change; normal runs
cannot send future audio IDs because that audio does not exist yet.

## Configuration

The base model remains `eleven_multilingual_v2`. All existing voice profiles
retain their previous models: Luca, Nicolas Petit, Sébastien, Paul K., and Noé
use v3; Victor, Maxime, and Adam use Multilingual v2. No profile enables v4 by
default. V3 remains supported through the older TTS endpoint without continuity
arguments.

Enable v4 explicitly for an individual voice by setting `model_id: "eleven_v4"`
in that voice's entry in `config/voices/config.yaml`. Keep the global
`ATR_ELEVENLABS_MODEL_ID` base at `eleven_multilingual_v2`.

Per-voice `model_id` takes priority over `ATR_ELEVENLABS_MODEL_ID`. Existing
environment overrides still take priority over the application default for
voices without a model override. Restart the backend to reload settings and
the cached voice configuration.

Legacy settings remain in existing voice profiles to support switching models;
v4 only forwards stability and similarity. `default_tts_speed` continues to
control the post-generation speed slider independently of model settings.

Set `audio_control_tags: ["TikTok narrator"]` on an individual voice to replace
the default delivery tags with `[TikTok narrator]`, as configured for `bass_v4`.
Use tag names without brackets; multiple names are joined inside one bracketed
prefix. An empty list disables the prefix. Omitting the field (or using `null`)
keeps `[TikTok narrator, energetic, rapid]` for v3 and `[TikTok narrator, rapid]`
for v4. Other models do not receive these tags.

## Validation

Live checks returned nonempty 48 kHz PCM and response request IDs for three
successive blocks, a middle-block regeneration with both neighboring audio IDs,
and a tagged block with 100 characters of text context on each side. Both seed
boundaries (0 and 4294967295) were accepted. These establish API compatibility,
not a perceptual guarantee about continuity or voice quality.

Offline tests cover the HTTP body, unsupported-setting exclusion, context
truncation and ordering, seed validation, v2/v3 compatibility, missing request
IDs, complete automation, seed/manifest persistence, and PCM WAV output:

```sh
.pixi/envs/default/bin/python -m pytest -q \
  backend/tests/test_elevenlabs_service.py \
  backend/tests/test_script_automation_v4.py \
  backend/tests/test_script_automation_seed.py \
  backend/tests/test_script_automation_pcm.py \
  backend/tests/test_script_automation_voice_persistence.py
```
