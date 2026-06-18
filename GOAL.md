/goal Improve this repository’s anime scene detection and matching pipeline for maximum practical precision and speed. The task is complete only when the four curated ground-truth projects pass strict fresh-pipeline validation.

Ground-truth projects:

- dcd74148c7ec
- 85de83ca6323
- 411f73d26c1d
- 5e85164d9ff8

Environment:

- Arch Linux
- Laptop RTX 4070 with 8 GB VRAM
- 32 GB RAM
- Use GPU where it helps, but assume 2 or 3 matching jobs may run concurrently. GPU memory use must be adaptive and conservative.
- Adding dependencies is allowed when justified by measurable precision or speed gains.

Hard rules:

- Always validate from fresh scene detection. Do not use ground-truth scenes as input for final validation.
- Do not modify, save into, normalize, delete, or rewrite the four ground-truth project folders.
- Use temp copies or generated output files outside the ground-truth folders for experiments and validation.
- Do not edit the anime_searcher submodule or change how series are indexed.
- Do not hardcode project IDs, exact fixture timings, episode names, or fixture-specific exceptions.
- Generalize the algorithm for future projects.

Relevant code to inspect:

- backend/app/services/scene_detector.py
- backend/app/services/scene_merger.py
- backend/app/services/anime_matcher.py
- backend/scripts/evaluate_matching_against_ground_truth.py
- backend/tests/test_anime_matcher_cache.py
- backend/tests/test_matching_episode_options.py
- backend/tests/test_scene_merger_manual_merge.py

Acceptance criteria:
For each ground-truth project, run fresh scene detection, any automatic scene fusion/merge phase used by production, then matching.

A project passes only if:

- The generated scene list has the correct scene cuts/fusions relative to ground truth.
- Scene start/end timings are within +/-0.3s for every scene, except at most 3 scenes may be within +/-1.0s.
- The primary/best selected source match has the correct episode and source start/end timing within the same tolerance rules, except at most 2 scenes may have a wrong primary match.
- For those at-most-2 wrong-primary scenes, the correct source timing must still be present in the AI candidate data exposed to the user, such as alternatives/start/end candidates, so the user can pick it directly without manually searching.
- No scene may require manually finding source timings.

First, make or update a strict non-mutating validator that encodes these exact rules. The existing evaluator may be too loose; do not rely on a 90% pass threshold or a broad default tolerance if that conflicts with the criteria above.

Work loop:

1. Establish a baseline on all four projects using fresh scene detection. Record accuracy failures and elapsed time per project.
2. Profile the pipeline before optimizing. Identify time spent in scene detection, frame decode/seeking, SSCD embedding, FAISS search, crop/zoom recovery, local refinement, continuity merging, repeated I/O, and cache misses.
3. Improve precision first. Focus on zoom/crop robustness, source temporal consistency, boundary refinement, false scene fusion, missed fusion, dense short scenes, candidate ranking, and continuity-based recovery.
4. Improve speed aggressively after precision is reliable. Use batching, GPU-aware embedding, adaptive batch sizing for 8 GB VRAM, bounded caches, fewer repeated VideoCapture seeks, reusable frame/embedding/search results, and lower redundant crop/refinement work.
5. Add or update focused tests for evaluator behavior, scene detection/merge behavior, candidate ranking, caching, crop/zoom recovery, and any new performance-critical logic.
6. Iterate until all four projects pass the strict validator.
7. Run regression tests.
8. Verify the four ground-truth folders are unchanged with git diff.

Suggested commands:

- pixi run pytest backend/tests/test_anime_matcher_cache.py backend/tests/test_matching_episode_options.py backend/tests/test_scene_merger_manual_merge.py
- pixi run python backend/scripts/evaluate_matching_against_ground_truth.py dcd74148c7ec 85de83ca6323 411f73d26c1d 5e85164d9ff8
- If the existing evaluator is replaced or supplemented, run the stricter validator instead.
- git diff -- backend/data/projects/dcd74148c7ec backend/data/projects/85de83ca6323 backend/data/projects/411f73d26c1d backend/data/projects/5e85164d9ff8

Final report:

- Baseline vs final elapsed time per project.
- Final validation metrics per project.
- Summary of algorithmic changes.
- Tests run.
- Any new dependency added and why.
- Explicit confirmation that the four ground-truth folders were not modified.
- If blocked by missing videos, model files, indexes, CUDA/FAISS issues, or reproducibility problems, report the exact path/error and stop instead of claiming success.
