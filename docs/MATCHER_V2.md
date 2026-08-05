# Bounded hierarchical matcher

`HierarchicalMatcherService` is the default matching path selected when
`ATR_MATCHER_V2` is unset or false. `SceneAlignerService` remains the
route-compatible adapter, so `/matches/find`, SSE progress, persisted
`SceneList`/`MatchList` JSON, and manual rematching keep their existing shapes.
`ATR_MATCHER_V2=1` explicitly selects the original matcher as a compatibility
escape hatch. Production never runs both matchers for one request.

The flag may be placed in the repository `.env`; the backend settings loader
reads it at startup. An explicitly exported `ATR_MATCHER_V2` process variable
takes precedence. Restart the backend after changing `.env` because an already
running Uvicorn process cannot retroactively change its settings.

The bounded path performs one PTS-aware PyAV decode. Every native frame contributes
to the 64px luma-difference curve, while only 4fps plus detector 20/50/80%
probes are converted to RGB and embedded. It retrieves top-60 FAISS results in
batches, tracks the top 20 with a beam-32 affine correspondence graph, uses
detector/difference boundaries as soft reset evidence, then splits supported
source discontinuities and merges continuous mappings. Query variants are
capped at 25% of base samples. Native verification is capped at
`min(24s, max(8s, 0.15 * query duration))` of source windows and stops when 80%
of the 60s/120s wall target has elapsed.

Alternative matches use the existing `AlternativeMatch` wire type. The
`algorithm` value is one of:

- `timeline_cluster`: a supported affine temporal cluster from already fetched
  top-60 evidence;
- `crop_variant`: a cluster supported by a bounded query geometry variant;
- `start_anchor`, `middle_anchor`, or `end_anchor`: a single positional anchor.

At most seven alternatives are returned. Same-episode positions inside
`max(2s, scene duration)` form one UI cluster, and the primary cluster is
excluded when another supported cluster exists.

## Independent evaluator

Run `python scripts/evaluate_matching_v2.py` through Pixi. It defaults to the
bounded matcher on the four curated projects, with one cold plus two warm runs,
and writes only below
`~/.cache/atr-matching-v2`. The evaluator hashes `project.json`, `scenes.json`,
and `matches.json` before and after every project, rejects output directories
inside a ground-truth tree, and never imports the historical comparison
evaluator.

Scoring samples the query timeline at 10Hz and compares canonical episode plus
linearly interpolated source timestamp, so continuous merges and harmless
splits do not affect correctness. JSON includes ±0.5s/±1s coverage, episode and
abstention rates, resolved precision, top-seven recall, fragmentation,
candidate diversity, phase/runtime counters, and GPU peak memory. It also
writes focused HTML/image review artifacts, leave-one-project-out confidence
calibration, and an explicit acceptance report. The bounded matcher is the
normal path; `--matcher v2` is available only for measuring the old
compatibility path.

## 2026-08-04 release-gate result

The first complete cold-plus-two-warm matrix for the bounded matcher passed all
four wall-time gates but failed the accuracy gate. Suite-wide coverage was
66.58% at ±0.5s and
78.99% at ±1.0s; resolved-primary precision was 79.17%. Leave-one-project-out
calibration selected `1.000001` (no resolved samples) as the only conservative
98%-precision threshold. The bounded implementation is nevertheless kept as
the default requested path. The result
and per-project visual reviews are stored in
`~/.cache/atr-matching-v2/final-20260804`.
