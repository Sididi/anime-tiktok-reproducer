# Matching precision rework — Approach B (alternative, source-window re-ranking)

**Status:** Alternative design (not selected as main). Provided for the user
to evaluate / implement later if Approach A's accuracy turns out
insufficient and a heavier compute budget becomes acceptable.

**Sibling docs:**
- `2026-04-27-matching-precision-rework-design.md` — Approach A, main.
- `2026-04-27-matching-precision-rework-hybrid.md` — Hybrid A+B fallback.

---

## 1. Problem statement (recap)

Same as Approach A: wrong-shot matches and chain over-merging are the
dominant `/matches` failure modes. The brainstorming session surfaced
that the deepest fix is **dense source-side verification**: decode the
matched source episode in a small window, re-embed those frames at
native FPS, and check whether the TikTok scene's frames really do trace
a continuous trajectory through that window.

Approach B adopts that fix as the primary mechanism. The cost is
~3–5× the per-scene compute of today (well above the user's "tight
budget" choice in brainstorming). Use B when accuracy is critical and
extra minutes per project are acceptable.

---

## 2. Approach overview

Two-stage matching:

1. **Stage 1 — Cheap retrieval.** Same as today's first pass: extract 3
   TikTok frames per scene, run SSCD top-K (`top_n = 50`, twice today's
   K, since we're going to use this as a ranking funnel rather than the
   final answer). Group candidates by episode → produce 1–3 *hypothesis
   regions* per scene (an episode + a coarse source range).
2. **Stage 2 — Dense source verification.** For each hypothesis region:
   1. Open the source episode video.
   2. Decode and embed every native-FPS frame in the hypothesis window
      `[src_lo − 1.0, src_hi + 1.0]` (typically 3–5 seconds → ~75–125
      frames at 24 fps).
   3. Take ≥ 5 TikTok frames densely from the scene (same probe layout
      as Approach A §3.1 — reused from there).
   4. For each TikTok frame, find the *single best matching frame* in the
      source window by SSCD cosine; record `(probe_time,
      best_source_time, similarity)`.
   5. Fit a monotonic line (via RANSAC, same as A §3.3) through the
      points; the fit's `score` is this hypothesis' verification score.
3. Pick the hypothesis with the best verification score as the primary
   match. The line evaluated at scene start/end gives `start_time`
   and `end_time` directly (no separate `_refine_boundaries` step
   needed — Stage 2 already operates at native FPS).

The continuity merger uses a related mechanism (§5).

---

## 3. Stage 1 — Cheap retrieval and hypothesis grouping

### 3.1 Probe extraction

3 frames per scene at `start + 0.125s`, `(start + end) / 2`,
`end − 0.125s` (current behavior). Stage 2's dense probes come *later*;
Stage 1 only needs enough signal to enumerate hypotheses.

### 3.2 Retrieval

`_search_image_batch` with `top_n = 50`, `flip = False`, `series =
anime_name`.

### 3.3 Hypothesis grouping

For each candidate, define a 5-second hypothesis region centered on the
candidate (`hyp = (episode, candidate.timestamp − 2.5,
candidate.timestamp + 2.5)`).

Cluster candidates that fall inside an existing hypothesis region (same
episode, source ranges overlap by ≥1s). Merge clusters' regions to their
union, capped at 8s wide.

Sort clusters by `(supporting_probe_count desc, max_similarity desc)`.
Keep the top **3** clusters. Drop the rest.

This caps Stage 2 work at 3 verifications per scene.

---

## 4. Stage 2 — Dense source verification

### 4.1 Source window decoding

For each hypothesis region `(episode, src_lo, src_hi)`:

1. Resolve the source episode path via
   `AnimeLibraryService.resolve_episode_path` (already used by
   `_refine_boundaries`).
2. Open the source video, seek to `src_lo`.
3. Decode native-FPS frames sequentially up to `src_hi`. Use the existing
   `_collect_frames_in_window` helper, raising the `max_frames` cap from
   48 to 200.
4. Return list of `(source_time, PIL_image)` tuples.

Cache decoded frames per `(episode_path, src_lo_quantized,
src_hi_quantized)` (quantized to 0.1s) so multiple scenes investigating
overlapping windows on the same episode don't decode twice. LRU cache,
size ~40 windows.

### 4.2 Source frame embedding

Embed all decoded source frames in one batch via the existing
`SSCDEmbedder.embed_batch`. Cache the embedding array alongside the
window cache.

### 4.3 Dense TikTok probes

Use the same probe layout as Approach A §3.1 (5–7 frames depending on
scene duration). One `VideoCapture` pass.

Embed all probe frames in one batch.

### 4.4 Per-probe nearest neighbor in source window

For each probe embedding `q_i`:

```python
similarities = source_embeddings @ q_i        # cosine, since L2-normalized
best_idx     = argmax(similarities)
best_time    = source_times[best_idx]
best_sim     = similarities[best_idx]
```

This is `O(N_source_frames)` per probe, ~125 ops per probe. Negligible
beside the SSCD embedding cost.

### 4.5 Trajectory fit on dense observations

The points `(probe_time_i, best_time_i, best_sim_i)` go into the same
RANSAC line fit from Approach A §3.3, with two changes:

- `RANSAC_INLIER_TOL_SECONDS = 0.15` (much tighter than A's 0.6, because
  we're now at native frame resolution — no 0.5s index quantization).
- The score weights similarity heavily: `inlier_similarity ≥ 0.50` for
  the inlier set as a whole (i.e. drop hypotheses where even the best
  source frames are weakly similar — that's a real wrong-region signal
  available only because we re-embedded the source).

### 4.6 Hypothesis selection

Hypothesis with the highest fit score wins. The selected fit's line
evaluated at `scene.start_time` and `scene.end_time` gives the final
boundaries (no separate refinement step — Stage 2 is already
frame-accurate).

If all 3 hypotheses score below a confidence floor (e.g.
`avg_inlier_similarity < 0.30`), commit to the top one anyway (per the
"always commit" policy) but mark `confidence` accordingly so the UI can
flag it.

### 4.7 Cost

Per scene:
- Source decoding: 75–200 frames × N_hypotheses (up to 3). Worst case
  600 frames decoded. With cv2 hardware decode ≈ 0.5–2s.
- SSCD embed of source frames: 200 × 3 = 600 embeds. At ~3ms/embed on
  GPU = 1.8s.
- TikTok probe embeds: 5–7 frames, one batch = 0.05s.
- Dense NN search: trivial.
- RANSAC: trivial.

Total: ~2–4s per scene worst case, often faster with the window cache.

---

## 5. Continuity merger — joint window verification

The same window-decoding machinery powers the continuity check. For each
adjacent pair `(N, N+1)`:

1. Tier 1 (cheap, like A §4.1): same episode, source-gap small,
   slope-consistent.
2. Tier 2 — joint window fit:
   - Take the *union window* covering both scenes' Pass 2 source ranges,
     padded by 1s.
   - Decode + embed the union window (reuses Stage 2 cache when
     possible).
   - Take 4–6 TikTok probes spanning both scenes (e.g. 2–3 from each).
   - Run dense NN + line fit on the joint set.
   - Continuity passes iff a single line fits *all* probes to within
     `RANSAC_INLIER_TOL_SECONDS = 0.15` (i.e. one continuous source
     trajectory covers both scenes' frames).
3. Score = joint-fit `inlier_count × avg_inlier_similarity`.

This is structurally stronger than A's cut-pair verifier: instead of
checking 2 frames at the cut, it tests whether *all* TikTok frames from
both scenes fit one continuous source motion. Over-merge becomes very
hard to fool because a fake continuity would need 4–6 frames to
coincidentally land on a straight line in source.

Cost: ~2–4 extra source-window verifications per project (most pairs
already have their windows cached).

---

## 6. Pass 2 — re-match merged scenes

Stage 2 already operates at native FPS, so the explicit "re-match merged
scenes" pass becomes lighter:

- Take the merged scene's full duration; use Approach A §3.1 probe rules
  (likely 5–7 probes).
- Reuse the joint window from §5 if it was already decoded; otherwise
  decode `[source_lo − 1, source_hi + 1]` from the merged-from primaries.
- Run dense NN + line fit on the probe set.
- The fit evaluated at merged-scene start/end gives the final
  boundaries.

No separate boundary refinement step.

---

## 7. Code organization

### 7.1 New module: `backend/app/services/source_window_cache.py`

Owns:
- `SourceWindow(episode_path, src_lo, src_hi, frames, embeddings,
  source_times)` dataclass.
- LRU cache with 40-entry capacity, keyed on `(episode_path,
  round(src_lo, 1), round(src_hi, 1))`.
- `get_or_decode(...)` that decodes + embeds and caches.

This isolates the heavy-IO surface so it can be tuned / cleared
independently.

### 7.2 `backend/app/services/anime_matcher.py`

**Add:**
- `_enumerate_hypotheses(per_probe_candidates) -> list[Hypothesis]`
  (§3.3).
- `_verify_hypothesis(video_path, scene, hypothesis, library_type) ->
  TrajectoryFit` (§4).
- `Hypothesis`, `TrajectoryFit` dataclasses.

**Modify:**
- `match_scenes`: call Stage 1 → Stage 2 → pick best.

**Remove:**
- `_find_temporal_match`, `_refine_boundaries` (subsumed by Stage 2).

**Keep:**
- `_search_image_batch`, `_compute_alternatives`, `_collect_frames_in_window`
  (used by the cache module).

### 7.3 `backend/app/services/scene_merger.py`

**Add:**
- `_verify_joint_window(...)` — §5 Tier 2.

**Modify:**
- `detect_continuous_pairs`: Tier 1 (cheap) + Tier 2 (joint window).

**Remove:**
- All the candidate-aggregation / stitching machinery removed by
  Approach A. Same removal list.

---

## 8. Tunables

| Constant                          | Value          | Where        |
|-----------------------------------|----------------|--------------|
| `STAGE1_TOP_K`                    | 50             | matcher      |
| `HYPOTHESIS_WINDOW_HALF_SECONDS`  | 2.5            | matcher      |
| `HYPOTHESIS_MERGE_OVERLAP`        | 1.0            | matcher      |
| `MAX_HYPOTHESIS_WIDTH`            | 8.0            | matcher      |
| `MAX_HYPOTHESES_PER_SCENE`        | 3              | matcher      |
| `RANSAC_INLIER_TOL_SECONDS`       | 0.15           | matcher (tight, native FPS) |
| `INLIER_SIMILARITY_FLOOR`         | 0.50           | matcher      |
| `WINDOW_CACHE_SIZE`               | 40             | source cache |
| `WINDOW_CACHE_TIME_QUANTUM`       | 0.1            | source cache |
| `JOINT_WINDOW_PAD_SECONDS`        | 1.0            | merger       |

---

## 9. Backwards compatibility

Same as Approach A — public Pydantic models unchanged, `confidence`
semantics still in `[0, 1]`, all manual-merge / undo / API surfaces
preserved.

---

## 10. Validation plan

Identical to Approach A §10. The metrics targets are higher (because
the algorithm is more expensive, accuracy expectations rise):

- ≥90% scenes with correct episode.
- ≥85% scenes with correct boundaries within ±0.3s.
- ≥80% chain composition F1.
- 0 over-merges spanning ≥3 originally-distinct scenes.

---

## 11. Risks and mitigations

| Risk                                    | Likelihood | Mitigation                                                          |
|-----------------------------------------|-----------|---------------------------------------------------------------------|
| Per-scene latency exceeds budget        | high      | Aggressive window caching; fall back to A's cheap fit on cache miss timeout. |
| Source IO hot path on cold cache        | medium    | Pre-decode windows for all top-1 hypotheses in parallel before fit. |
| Memory pressure from cached embeddings  | low       | LRU eviction; ~200 embeds × 256-dim float32 ≈ 200 KB per window — 40 windows ≈ 8 MB. |
| Wrong hypothesis from Stage 1 makes Stage 2 verify the wrong region | low | Top-3 hypotheses; per-region inlier-similarity floor rejects bad regions. |
| Joint-window continuity verifier conservative on hard fades | medium | Same ε-retry trick as A §4.2; or fall back to A's cut-pair check.   |

---

## 12. Implementation milestones

1. `SourceWindow` + cache module (with tests, since IO and embeddings
   make it the riskiest piece).
2. Stage 1 hypothesis enumeration.
3. Stage 2 dense verification per hypothesis.
4. Hypothesis selection in `match_scenes`.
5. Joint-window continuity verifier.
6. Merger refactor.
7. Validation run + metrics; tune cache size / hypothesis count.

---

## 13. When to choose Approach B over A

- Approach A's validation metrics fall below targets in §10.
- Wrong-shot matches still dominate after A is shipped (e.g. >15%
  of scenes have wrong primary).
- The user accepts ~3–5× per-scene compute (multiple minutes per
  project).
- Source IO is fast (local SSD or RAM-cached library) — not over a
  network mount.

If only some of these apply, prefer the Hybrid in
`2026-04-27-matching-precision-rework-hybrid.md`.
