# Premiere narration gain and raw-scene effects

Each entry in `config/voices/config.yaml` accepts `volume_db`, alongside
`display_name`. Its default is `0`; supported values are -96 through +15 dB.
For example, `volume_db: 7.0` applies a +7 dB gain to that voice's narration
clip on A2. The generated JSX uses the project's resolved voice, including
the template/default voice when there is no project override. Regeneration
uses the current voice configuration. Restart a running backend after editing
the cached voice configuration.

This is a Premiere import setting. It does not modify the TTS WAV or the web
preview. Positive gain can clip narration if the original already peaks near
0 dBFS; no narration limiter is added. The importer reads the clip gain back
and stops if Premiere clamps or rejects it instead of accepting the setting.

## Raw scenes

The existing importer selects the source language/channels and places raw-scene
audio. An optional final pass applies the effects from
`assets/Raw Scene Audio.prfpset` directly to those audible clips, across all
tracks from A4 onward. The muted source copy on A1, narration, and music are
excluded. This pass does not move clips, change their source/timeline timing,
adjust their gain, or create sequences or nests. It does not introduce raw-audio
duration/completeness checks that can stop an import.

The backend reads the preset's saved effect order and parameter values without
rewriting them. For the supplied file, Hard Limiter precedes Dynamics Processing.
Mono, stereo, and 5.1 clips use their corresponding saved variants; individually
split source channels use the mono variant. The saved limiter values differ
between variants. The dynamics curve and channel-linking settings also remain
exactly as saved. This is per-clip processing: it does not guarantee equal scene
loudness or a common ceiling for the combined mix.

The normalized parameter values are embedded in the generated JSX. The preset
file is included in new bundles when available, but the JSX does not require it
at import time. If it is missing or unsupported at generation time, the JSX
imports with the original audio and reports a warning.

Effect insertion follows the working video-preset path: resolve display-name
candidates, call QE, and verify the standard DOM component count increased.
A truthy lookup or insertion return is insufficient. Candidates that do nothing
fall through to the next alias or matching audio-catalog entry. The appended
component receives the saved values without requiring its runtime name to equal
the XML UUID or localized label. DOM and QE clip references are reacquired after
mutations; observed runtime identities are retained for retry protection.

This approach is also used by the public
[Premiere CLI audio-effect implementation](https://github.com/stefanwebb/premiere-cli/blob/main/src/premiere_cli/panel/host/commands/apply-audio-effect.jsx)
and [IronFX preset component insertion](https://github.com/IronVisuals/IronFX/blob/main/jsx/IronFX.jsx).
If insertion still fails, the warning reports each candidate, its return value,
before/after component counts, and the actual component identities.

## Nonfatal preset failures

Effect lookup, channel compatibility, parameter layout, and readback failures
produce warnings and continue with the next clip. Newly added effects stay
bypassed until all saved parameters have been written and verified. If applying
the pair fails, the importer disables effects added by that attempt, with
removal as a fallback when supported. Existing components are not rewritten.
Repeated application skips clips that already have either preset effect, avoiding
duplicate processing.

If Premiere rejects both disabling and removing an added effect, the warning
identifies the affected clip for inspection; the import and batch still continue.
Original media, cuts, and clip gain are retained in all these paths. Warnings
are returned through the existing CEP import receipt, not as failed imports.
This behavior applies to the optional raw preset; unrelated import failures
keep their existing handling.

## Premiere validation

Local tests exercise preset parsing, JSX generation, direct clip application,
channel variants, partial failures, retries, and unchanged clip timing/gain using
a simulated host. They do not run Premiere or verify its audio output.

On Premiere, import a project containing raw scenes and check:

1. The original raw clips remain on their existing tracks, with the correct
   language/channels and cuts. No `ATR_RAW_AUDIO__` sequence is created.
2. Effect Controls show Hard Limiter followed by Dynamics Processing on each
   supported raw clip. Compare their settings with manually applying the same
   preset to a clip of the same channel type.
3. Audition quiet and loud scenes with music/narration muted, then in the full
   mix. Check transitions, background noise, and channel balance.
4. Any raw-preset warning leaves the project imported and eligible for the rest
   of batch processing. A no-raw project skips the preset pass entirely.
