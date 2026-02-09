"""Service for detecting and merging continuous anime scenes."""

import json
from bisect import bisect_right

from ..models import Scene, SceneList, SceneMatch, MatchList
from .project_service import ProjectService


class SceneMergerService:
    """Detects continuous scenes in anime source and merges them."""

    # A tighter margin avoids merging intentional quick cuts while still allowing
    # minor timestamp jitter between adjacent source candidates.
    CONTINUITY_GAP_TOLERANCE = 0.30  # seconds
    CONTINUITY_EPSILON = 1e-3  # allow tiny numerical jitter
    MIN_ALT_CONFIDENCE = 0.25
    MIN_RAW_CANDIDATE_CONFIDENCE = 0.30
    CANDIDATE_TIME_ROUNDING = 3
    # Stitch adjacent chains only to heal boundaries where at least one side is
    # no-match and continuity evidence is still strong nearby.
    CHAIN_BRIDGE_WINDOW = 2
    CHAIN_BRIDGE_GAP_TOLERANCE = 1.0
    CHAIN_BRIDGE_MIN_SCORE = 0.22

    @classmethod
    def _get_scene_half_duration(
        cls,
        scene_index: int | None,
        scenes: SceneList | None,
    ) -> float | None:
        """Return half of a scene duration when available."""
        if scene_index is None or scenes is None:
            return None
        if scene_index < 0 or scene_index >= len(scenes.scenes):
            return None

        duration = scenes.scenes[scene_index].duration
        if duration <= 0:
            return None

        return duration / 2.0

    @classmethod
    def detect_continuous_pairs(
        cls,
        scenes: SceneList,
        matches: MatchList,
    ) -> list[tuple[int, int]]:
        """
        Find adjacent scene pairs that are continuous in the anime source.

        For each adjacent pair (N, N+1), checks if scene N's end timing
        is close to scene N+1's start timing in the same episode.

        Returns:
            List of (scene_index, scene_index+1) tuples that are continuous.
        """
        pairs: list[tuple[int, int]] = []

        for i in range(len(scenes.scenes) - 1):
            match_n = matches.matches[i] if i < len(matches.matches) else None
            match_n1 = matches.matches[i + 1] if (i + 1) < len(matches.matches) else None

            if not match_n or not match_n1:
                continue

            # Gather end-candidates for scene N
            n_end_candidates = cls._get_end_candidates(
                match_n,
                scene_index=i,
                scenes=scenes,
            )
            # Gather start-candidates for scene N+1
            n1_start_candidates = cls._get_start_candidates(
                match_n1,
                scene_index=i + 1,
                scenes=scenes,
            )

            if not n_end_candidates or not n1_start_candidates:
                continue

            # Check if the pair has any continuity signal.
            if cls._get_best_pair_continuity(n_end_candidates, n1_start_candidates):
                pairs.append((i, i + 1))

        return pairs

    @classmethod
    def _normalize_confidence(cls, confidence: float) -> float:
        """Normalize confidence into [0, 1] for comparable continuity scoring."""
        return max(0.0, min(1.0, confidence))

    @classmethod
    def _dedupe_candidates(
        cls,
        candidates: list[tuple[str, float, float]],
    ) -> list[tuple[str, float, float]]:
        """
        Deduplicate candidates by (episode, timestamp) and keep best confidence.

        This prevents duplicate alternatives (e.g. best_frame + union_topk with
        identical timing) from overweighting continuity scores.
        """
        merged: dict[tuple[str, float], tuple[str, float, float]] = {}
        for episode, timestamp, confidence in candidates:
            key = (episode, round(timestamp, cls.CANDIDATE_TIME_ROUNDING))
            prev = merged.get(key)
            if prev is None or confidence > prev[2]:
                merged[key] = (episode, timestamp, confidence)
        return list(merged.values())

    @classmethod
    def _get_end_candidates(
        cls,
        match: SceneMatch,
        scene_index: int | None = None,
        scenes: SceneList | None = None,
    ) -> list[tuple[str, float, float]]:
        """Get (episode, end_time, confidence) candidates for a scene's end."""
        candidates: list[tuple[str, float, float]] = []

        # Primary match
        if match.episode and match.confidence > 0:
            candidates.append((
                match.episode,
                match.end_time,
                cls._normalize_confidence(match.confidence),
            ))

        # Alternatives are always included above threshold so top-2/top-3 can
        # contribute to continuity even when primary is not the best temporal fit.
        for alt in match.alternatives:
            if alt.confidence > cls.MIN_ALT_CONFIDENCE:
                candidates.append((
                    alt.episode,
                    alt.end_time,
                    cls._normalize_confidence(alt.confidence),
                ))

        # Add raw top-k frame candidates directly from anime_searcher search.
        for candidate in match.end_candidates:
            if candidate.similarity >= cls.MIN_RAW_CANDIDATE_CONFIDENCE:
                candidates.append((
                    candidate.episode,
                    candidate.timestamp,
                    cls._normalize_confidence(candidate.similarity),
                ))

        # For no-match scenes, middle-frame timings often remain useful even when
        # start/end candidates are noisy. Project middle timestamps to scene end.
        if match.was_no_match:
            half_duration = cls._get_scene_half_duration(scene_index, scenes)
            if half_duration is not None:
                for candidate in match.middle_candidates:
                    if candidate.similarity >= cls.MIN_RAW_CANDIDATE_CONFIDENCE:
                        candidates.append((
                            candidate.episode,
                            candidate.timestamp + half_duration,
                            cls._normalize_confidence(candidate.similarity),
                        ))

        return cls._dedupe_candidates(candidates)

    @classmethod
    def _get_start_candidates(
        cls,
        match: SceneMatch,
        scene_index: int | None = None,
        scenes: SceneList | None = None,
    ) -> list[tuple[str, float, float]]:
        """Get (episode, start_time, confidence) candidates for a scene's start."""
        candidates: list[tuple[str, float, float]] = []

        # Primary match
        if match.episode and match.confidence > 0:
            candidates.append((
                match.episode,
                match.start_time,
                cls._normalize_confidence(match.confidence),
            ))

        # Alternatives are always included above threshold so top-2/top-3 can
        # contribute to continuity even when primary is not the best temporal fit.
        for alt in match.alternatives:
            if alt.confidence > cls.MIN_ALT_CONFIDENCE:
                candidates.append((
                    alt.episode,
                    alt.start_time,
                    cls._normalize_confidence(alt.confidence),
                ))

        # Add raw top-k frame candidates directly from anime_searcher search.
        for candidate in match.start_candidates:
            if candidate.similarity >= cls.MIN_RAW_CANDIDATE_CONFIDENCE:
                candidates.append((
                    candidate.episode,
                    candidate.timestamp,
                    cls._normalize_confidence(candidate.similarity),
                ))

        # For no-match scenes, middle-frame timings often remain useful even when
        # start/end candidates are noisy. Project middle timestamps to scene start.
        if match.was_no_match:
            half_duration = cls._get_scene_half_duration(scene_index, scenes)
            if half_duration is not None:
                for candidate in match.middle_candidates:
                    if candidate.similarity >= cls.MIN_RAW_CANDIDATE_CONFIDENCE:
                        candidates.append((
                            candidate.episode,
                            max(0.0, candidate.timestamp - half_duration),
                            cls._normalize_confidence(candidate.similarity),
                        ))

        return cls._dedupe_candidates(candidates)

    @classmethod
    def _get_best_pair_continuity(
        cls,
        n_end_candidates: list[tuple[str, float, float]],
        n1_start_candidates: list[tuple[str, float, float]],
    ) -> tuple[str, float] | None:
        """
        Resolve the strongest continuity episode for one adjacent scene pair.

        Returns:
            (episode, score) when at least one forward-continuous combination exists.
        """
        episode_scores: dict[str, list[float]] = {}
        episode_support: dict[str, int] = {}

        for ep_n, end_t, end_conf in n_end_candidates:
            for ep_n1, start_t, start_conf in n1_start_candidates:
                if ep_n != ep_n1:
                    continue

                # Require forward progression: next scene starts at/after current scene ends.
                gap = start_t - end_t
                if not (-cls.CONTINUITY_EPSILON <= gap <= cls.CONTINUITY_GAP_TOLERANCE):
                    continue

                # Reward high-confidence candidates and small positive gaps.
                # Gaps near 0s score highest; near tolerance score lower.
                if cls.CONTINUITY_GAP_TOLERANCE > 0:
                    gap_weight = 1.0 - min(max(gap, 0.0) / cls.CONTINUITY_GAP_TOLERANCE, 1.0)
                else:
                    gap_weight = 1.0
                conf_weight = (end_conf + start_conf) / 2.0
                combo_score = conf_weight * gap_weight

                episode_scores.setdefault(ep_n, []).append(combo_score)
                episode_support[ep_n] = episode_support.get(ep_n, 0) + 1

        if not episode_scores:
            return None

        # Use top-ranked evidence only (with diminishing weights) so one episode
        # is not favored purely due to many near-duplicate combinations.
        aggregated_scores: dict[str, float] = {}
        for ep, scores in episode_scores.items():
            sorted_scores = sorted(scores, reverse=True)
            top1 = sorted_scores[0]
            top2 = sorted_scores[1] if len(sorted_scores) > 1 else 0.0
            top3 = sorted_scores[2] if len(sorted_scores) > 2 else 0.0
            aggregated_scores[ep] = top1 + (0.35 * top2) + (0.20 * top3)

        best_episode = max(
            aggregated_scores.keys(),
            key=lambda ep: (aggregated_scores[ep], episode_support.get(ep, 0)),
        )
        return best_episode, aggregated_scores[best_episode]

    @classmethod
    def _build_chain_candidates(
        cls,
        pair_continuity: dict[int, tuple[str, float]],
    ) -> list[tuple[int, int, float, tuple[int, ...]]]:
        """
        Build all contiguous same-episode chain candidates.

        Returns a list of tuples:
            (start_scene_idx, end_scene_idx, chain_score, scene_indices_tuple)
        """
        candidates: list[tuple[int, int, float, tuple[int, ...]]] = []

        for start_idx in sorted(pair_continuity.keys()):
            episode, _ = pair_continuity[start_idx]
            current = start_idx
            running_score = 0.0
            scene_indices = [start_idx]

            while current in pair_continuity and pair_continuity[current][0] == episode:
                running_score += pair_continuity[current][1]
                scene_indices.append(current + 1)
                candidates.append((
                    start_idx,
                    current + 1,  # inclusive end scene index
                    running_score,
                    tuple(scene_indices),
                ))
                current += 1

        return candidates

    @classmethod
    def _select_non_overlapping_chains(
        cls,
        chain_candidates: list[tuple[int, int, float, tuple[int, ...]]],
    ) -> list[list[int]]:
        """
        Select the highest-scoring set of non-overlapping chains.

        Uses weighted interval scheduling on scene-index intervals.
        """
        if not chain_candidates:
            return []

        # Sort by end index for weighted interval scheduling.
        ordered = sorted(chain_candidates, key=lambda c: (c[1], c[0]))
        ends = [c[1] for c in ordered]
        prev_non_overlap: list[int] = []

        # For interval [start, end], compatible prior interval must satisfy prior_end < start.
        for start, _, _, _ in ordered:
            prev_non_overlap.append(bisect_right(ends, start - 1) - 1)

        n = len(ordered)
        dp = [0.0] * (n + 1)
        take = [False] * (n + 1)

        for j in range(1, n + 1):
            _, _, score, _ = ordered[j - 1]
            include_score = score + dp[prev_non_overlap[j - 1] + 1]
            exclude_score = dp[j - 1]
            if include_score > exclude_score + 1e-9:
                dp[j] = include_score
                take[j] = True
            else:
                dp[j] = exclude_score

        selected: list[tuple[int, int, float, tuple[int, ...]]] = []
        j = n
        while j > 0:
            if take[j]:
                selected.append(ordered[j - 1])
                j = prev_non_overlap[j - 1] + 1
            else:
                j -= 1

        selected.reverse()
        return [list(c[3]) for c in selected]

    @classmethod
    def _get_chain_bridge_continuity(
        cls,
        left_chain: list[int],
        right_chain: list[int],
        matches: MatchList,
        scenes: SceneList,
    ) -> tuple[str, float] | None:
        """
        Find continuity between adjacent chains using a small boundary window.

        This is specifically meant to recover merges when one boundary scene has
        weak/incorrect candidates (common for very short cuts), but neighboring
        scenes still provide strong continuity evidence.
        """
        if not left_chain or not right_chain:
            return None

        left_indices = left_chain[-cls.CHAIN_BRIDGE_WINDOW:]
        right_indices = right_chain[:cls.CHAIN_BRIDGE_WINDOW]
        episode_scores: dict[str, float] = {}
        episode_support: dict[str, int] = {}

        for li in left_indices:
            for ri in right_indices:
                if li >= len(matches.matches) or ri >= len(matches.matches):
                    continue

                left_match = matches.matches[li]
                right_match = matches.matches[ri]

                # Bridge only uncertain boundaries: at least one side was no-match.
                if not (left_match.was_no_match or right_match.was_no_match):
                    continue

                end_candidates = cls._get_end_candidates(
                    left_match,
                    scene_index=li,
                    scenes=scenes,
                )
                start_candidates = cls._get_start_candidates(
                    right_match,
                    scene_index=ri,
                    scenes=scenes,
                )

                for ep_left, end_t, end_conf in end_candidates:
                    for ep_right, start_t, start_conf in start_candidates:
                        if ep_left != ep_right:
                            continue

                        gap = start_t - end_t
                        if not (
                            -cls.CONTINUITY_EPSILON
                            <= gap
                            <= cls.CHAIN_BRIDGE_GAP_TOLERANCE
                        ):
                            continue

                        safe_gap = max(gap, 0.0)
                        gap_weight = 1.0 - min(
                            safe_gap / cls.CHAIN_BRIDGE_GAP_TOLERANCE,
                            1.0,
                        )
                        conf_weight = (end_conf + start_conf) / 2.0
                        score = conf_weight * gap_weight

                        if score > episode_scores.get(ep_left, 0.0):
                            episode_scores[ep_left] = score
                        episode_support[ep_left] = episode_support.get(ep_left, 0) + 1

        if not episode_scores:
            return None

        best_episode = max(
            episode_scores.keys(),
            key=lambda ep: (episode_scores[ep], episode_support.get(ep, 0)),
        )
        best_score = episode_scores[best_episode]
        if best_score < cls.CHAIN_BRIDGE_MIN_SCORE:
            return None

        return best_episode, best_score

    @classmethod
    def _stitch_adjacent_chains(
        cls,
        chains: list[list[int]],
        matches: MatchList,
        scenes: SceneList,
    ) -> list[list[int]]:
        """
        Stitch adjacent chains when a noisy boundary prevented direct pairing.

        Adjacent means left[-1] + 1 == right[0]. Stitching is conservative and
        can also absorb singleton scenes (indices that were not part of any
        selected chain) when bridge evidence is strong.
        """
        if not matches.matches:
            return []

        # Build ordered segments: selected chains + uncovered singleton scenes.
        normalized_chains: list[list[int]] = [
            sorted(chain) for chain in chains if chain
        ]
        covered_indices = {idx for chain in normalized_chains for idx in chain}
        segments: list[list[int]] = list(normalized_chains)
        for idx in range(len(matches.matches)):
            if idx not in covered_indices:
                segments.append([idx])
        segments.sort(key=lambda seg: seg[0])

        stitched: list[list[int]] = []
        i = 0
        while i < len(segments):
            current = list(segments[i])
            j = i + 1

            while j < len(segments) and current[-1] + 1 == segments[j][0]:
                bridge = cls._get_chain_bridge_continuity(
                    current,
                    segments[j],
                    matches,
                    scenes,
                )
                if not bridge:
                    break

                current.extend(segments[j])
                j += 1

            stitched.append(current)
            i = j

        # Keep output focused on actual merges.
        return [chain for chain in stitched if len(chain) >= 2]

    @classmethod
    def build_merge_chains(
        cls,
        pairs: list[tuple[int, int]],
        scenes: SceneList,
        matches: MatchList,
    ) -> list[list[int]]:
        """
        Build transitive merge chains from continuous pairs.

        Breaks chain if adjacent pair disagrees on episode.

        Input: [(2,3), (3,4), (7,8)] -> Output: [[2,3,4], [7,8]]
        """
        if not pairs:
            return []

        # Resolve best continuity episode+score per adjacent pair.
        pair_continuity: dict[int, tuple[str, float]] = {}
        for a, b in pairs:
            if b != a + 1:
                continue

            if a >= len(matches.matches) or b >= len(matches.matches):
                continue

            curr_end = cls._get_end_candidates(
                matches.matches[a],
                scene_index=a,
                scenes=scenes,
            )
            next_start = cls._get_start_candidates(
                matches.matches[b],
                scene_index=b,
                scenes=scenes,
            )
            continuity = cls._get_best_pair_continuity(curr_end, next_start)
            if continuity:
                pair_continuity[a] = continuity

        if not pair_continuity:
            return []

        # Build all possible contiguous same-episode chains, then choose a globally
        # optimal non-overlapping subset by continuity score.
        chain_candidates = cls._build_chain_candidates(pair_continuity)
        selected_chains = cls._select_non_overlapping_chains(chain_candidates)
        return cls._stitch_adjacent_chains(selected_chains, matches, scenes)

    @classmethod
    def merge_scenes_and_matches(
        cls,
        scenes: SceneList,
        matches: MatchList,
        chains: list[list[int]],
    ) -> tuple[SceneList, MatchList, dict]:
        """
        Merge scenes and matches according to chains.

        For each chain [i, j, k]:
        - Merged scene: start_time=scenes[i].start_time, end_time=scenes[k].end_time
        - Match: placeholder with was_no_match=True, merged_from=[i, j, k]

        Returns:
            (merged_scenes, merged_matches, backup_dict)
        """
        # Build backup of pre-merge state
        backup = {
            "scenes": [s.model_dump() for s in scenes.scenes],
            "matches": [m.model_dump() for m in matches.matches],
            "chains": chains,
        }

        # Build set of indices that are part of a merge
        merged_indices: set[int] = set()
        chain_map: dict[int, list[int]] = {}  # first_index -> chain
        for chain in chains:
            for idx in chain:
                merged_indices.add(idx)
                chain_map[chain[0]] = chain

        new_scenes: list[Scene] = []
        new_matches: list[SceneMatch] = []

        i = 0
        while i < len(scenes.scenes):
            if i in chain_map:
                chain = chain_map[i]
                first_scene = scenes.scenes[chain[0]]
                last_scene = scenes.scenes[chain[-1]]

                # Create merged scene
                new_scenes.append(Scene(
                    index=len(new_scenes),
                    start_time=first_scene.start_time,
                    end_time=last_scene.end_time,
                ))

                # Create placeholder match for re-matching
                new_matches.append(SceneMatch(
                    scene_index=len(new_matches),
                    episode="",
                    start_time=0,
                    end_time=0,
                    confidence=0,
                    speed_ratio=1.0,
                    was_no_match=True,
                    merged_from=chain,
                ))

                i = chain[-1] + 1
            elif i not in merged_indices:
                # Non-merged scene: keep as-is
                new_scenes.append(Scene(
                    index=len(new_scenes),
                    start_time=scenes.scenes[i].start_time,
                    end_time=scenes.scenes[i].end_time,
                ))

                if i < len(matches.matches):
                    match = matches.matches[i].model_copy()
                    match.scene_index = len(new_matches)
                    new_matches.append(match)
                else:
                    new_matches.append(SceneMatch(
                        scene_index=len(new_matches),
                        episode="",
                        start_time=0,
                        end_time=0,
                        confidence=0,
                        speed_ratio=1.0,
                        was_no_match=True,
                    ))

                i += 1
            else:
                # Part of a chain but not the start - skip (already handled)
                i += 1

        merged_scenes = SceneList(scenes=new_scenes)
        merged_scenes.renumber()

        merged_match_list = MatchList(matches=new_matches)

        return merged_scenes, merged_match_list, backup

    @classmethod
    def save_pre_merge_backup(cls, project_id: str, backup: dict) -> None:
        """Save pre-merge backup to project directory."""
        project_dir = ProjectService.get_project_dir(project_id)
        backup_path = project_dir / "pre_merge_backup.json"
        backup_path.write_text(json.dumps(backup, indent=2))

    @classmethod
    def load_pre_merge_backup(cls, project_id: str) -> dict | None:
        """Load pre-merge backup from project directory."""
        project_dir = ProjectService.get_project_dir(project_id)
        backup_path = project_dir / "pre_merge_backup.json"
        if not backup_path.exists():
            return None
        return json.loads(backup_path.read_text())

    @classmethod
    def undo_merge(
        cls,
        project_id: str,
        scene_index: int,
    ) -> tuple[SceneList, MatchList] | None:
        """
        Undo a merge for a specific scene.

        Restores the original sub-scenes and their matches from backup.

        Returns:
            (restored_scenes, restored_matches) or None if no backup/not a merged scene.
        """
        backup = cls.load_pre_merge_backup(project_id)
        if not backup:
            return None

        current_scenes = ProjectService.load_scenes(project_id)
        current_matches = ProjectService.load_matches(project_id)
        if not current_scenes or not current_matches:
            return None

        # Check the scene at scene_index is a merged scene
        if scene_index >= len(current_matches.matches):
            return None

        target_match = current_matches.matches[scene_index]
        if not target_match.merged_from:
            return None

        original_chain = target_match.merged_from
        original_scenes = [Scene(**s) for s in backup["scenes"]]
        original_matches = [SceneMatch(**m) for m in backup["matches"]]

        # Rebuild: replace the merged scene with original sub-scenes
        new_scenes: list[Scene] = []
        new_matches: list[SceneMatch] = []

        for i, (scene, match) in enumerate(zip(current_scenes.scenes, current_matches.matches)):
            if i == scene_index:
                # Restore original sub-scenes
                for orig_idx in original_chain:
                    if orig_idx < len(original_scenes):
                        orig_scene = original_scenes[orig_idx]
                        new_scenes.append(Scene(
                            index=len(new_scenes),
                            start_time=orig_scene.start_time,
                            end_time=orig_scene.end_time,
                        ))
                    if orig_idx < len(original_matches):
                        orig_match = original_matches[orig_idx].model_copy()
                        orig_match.scene_index = len(new_matches)
                        new_matches.append(orig_match)
                    else:
                        new_matches.append(SceneMatch(
                            scene_index=len(new_matches),
                            episode="",
                            start_time=0,
                            end_time=0,
                            confidence=0,
                            speed_ratio=1.0,
                            was_no_match=True,
                        ))
            else:
                new_scenes.append(Scene(
                    index=len(new_scenes),
                    start_time=scene.start_time,
                    end_time=scene.end_time,
                ))
                match_copy = match.model_copy()
                match_copy.scene_index = len(new_matches)
                new_matches.append(match_copy)

        restored_scenes = SceneList(scenes=new_scenes)
        restored_scenes.renumber()
        restored_matches = MatchList(matches=new_matches)

        # Save
        ProjectService.save_scenes(project_id, restored_scenes)
        ProjectService.save_matches(project_id, restored_matches)

        return restored_scenes, restored_matches
