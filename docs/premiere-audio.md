# Premiere narration gain and raw-scene leveling

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

The original language/channel selection and raw-scene source timing remain in
the existing placement path. The importer finds the resulting raw subclips
across all tracks from A4 onward, including 5.1 blocks that could not be moved
into the usual reserved zone.

It creates `ATR_RAW_AUDIO__<project_id>`, a stereo subsequence containing only
those clips. Source references and in/out/timeline timing must match exactly;
video, narration, or music in that subsequence causes the import to stop.
Original source clips remain editable inside it. The main timeline gets stereo
audio nests on A4 for the raw regions. Adjacent raw scenes share one region so
the dynamics processor is not restarted at every cut. No WAV excerpts or
measurement renders are created.

Effects run on the stereo nests, after Premiere's downmix:

1. **Dynamics Processing** uses a linked stereo, piecewise linear leveling curve.
2. **Hard Limiter** uses the stereo values from `assets/Raw Scene Audio.prfpset`,
   exported for the requested -10 dB ceiling and 0 dB input boost.

The initial dynamics curve is defined in
`backend/app/services/premiere_audio.py`:

| Input (dB) | Output (dB) |
|---:|---:|
| -100 | -100 |
| -60 | -60 |
| -40 | -18 |
| -20 | -12 |
| 0 | -10 |

This is an initial listening-test setting. It brings dialogue toward the target
while leaving the deepest noise floor alone; it does not normalize each scene's
exact peak to -10 dB or hold silence at that level. Overlapping music can make
the master meter exceed the raw-audio target.

The exported file contains stereo/mono/5.1 alternatives and lists the limiter
first. The backend selects only the stereo records, reverses the effect order,
and configures the dynamics curve. Normalized parameter values are embedded in
the JSX, so exporting the original sample unchanged would not apply this curve.
The sample `.prfpset` is also included in new bundles. It is not rewritten.

## Premiere validation still required

Local tests exercise parsing, generation, gain math, readback failures and nest
isolation using a simulated host. They cannot verify Premiere's QE operations or
its DSP. First validate on Premiere Pro 2025 before using batch export:

1. Regenerate/import a project containing stereo and 5.1 raw scenes. Verify
   the nested sequence contains the correct language and every retained channel,
   with no shifted cuts. The main sequence must remain the active export target.
2. Inspect the A4 nest's Effect Controls: Dynamics Processing precedes Hard
   Limiter; its graph matches the table above. Check the limiter displays -10 dB
   and 0 dB input boost. These UI checks also validate the serialized control
   mappings on the installed Premiere version.
3. Mute music and narration, then audition quiet and loud raw scenes. Listen for
   pumping/background noise, check stereo balance and watch the master meter.
4. Test a voice at +7 dB and +15 dB; confirm A2's Volume shows those values. Set
   it back to 0 if the boost is not wanted. Check music retains its configured gain.
5. Check a no-raw-scene project: it must create no raw-audio subsequence.

An unsupported effect layout, rejected value, or failed nest operation stops
the import with an error. If failure happens after nesting, the original raw
clips are retained in `ATR_RAW_AUDIO__<project_id>` for inspection. Send the
error and `[ATR]` log when a Premiere-specific check fails.

References: [Adobe's sequence/subsequence sample](https://github.com/Adobe-CEP/Samples/blob/master/PProPanel/jsx/PPRO/Premiere.jsx)
and [Adobe's audio effects reference](https://helpx.adobe.com/premiere/desktop/add-audio-effects/apply-audio-effects/audio-effects-library.html).
