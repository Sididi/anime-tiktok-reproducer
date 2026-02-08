"""Service for detecting and merging continuous anime scenes."""

import json
from pathlib import Path

from ..models import Scene, SceneList, SceneMatch, MatchList, AlternativeMatch
from .project_service import ProjectService


class SceneMergerService:
    """Detects continuous scenes in anime source and merges them."""

    CONTINUITY_GAP_TOLERANCE = 2.0  # seconds
    MIN_ALT_CONFIDENCE = 0.5

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
            n_end_candidates = cls._get_end_candidates(match_n)
            # Gather start-candidates for scene N+1
            n1_start_candidates = cls._get_start_candidates(match_n1)

            if not n_end_candidates or not n1_start_candidates:
                continue

            # Check if any combination shows continuity
            if cls._is_continuous(n_end_candidates, n1_start_candidates):
                pairs.append((i, i + 1))

        return pairs

    @classmethod
    def _get_end_candidates(cls, match: SceneMatch) -> list[tuple[str, float]]:
        """Get (episode, end_time) candidates for a scene's end."""
        candidates: list[tuple[str, float]] = []

        # Primary match
        if match.episode and match.confidence > 0:
            candidates.append((match.episode, match.end_time))

        # Alternatives with sufficient confidence
        if match.was_no_match or not candidates:
            for alt in match.alternatives:
                if alt.confidence > cls.MIN_ALT_CONFIDENCE:
                    candidates.append((alt.episode, alt.end_time))

        return candidates

    @classmethod
    def _get_start_candidates(cls, match: SceneMatch) -> list[tuple[str, float]]:
        """Get (episode, start_time) candidates for a scene's start."""
        candidates: list[tuple[str, float]] = []

        # Primary match
        if match.episode and match.confidence > 0:
            candidates.append((match.episode, match.start_time))

        # Alternatives with sufficient confidence
        if match.was_no_match or not candidates:
            for alt in match.alternatives:
                if alt.confidence > cls.MIN_ALT_CONFIDENCE:
                    candidates.append((alt.episode, alt.start_time))

        return candidates

    @classmethod
    def _is_continuous(
        cls,
        n_end_candidates: list[tuple[str, float]],
        n1_start_candidates: list[tuple[str, float]],
    ) -> bool:
        """Check if any combination of candidates shows continuity."""
        for ep_n, end_t in n_end_candidates:
            for ep_n1, start_t in n1_start_candidates:
                if ep_n == ep_n1 and abs(start_t - end_t) <= cls.CONTINUITY_GAP_TOLERANCE:
                    return True
        return False

    @classmethod
    def _get_continuity_episode(
        cls,
        n_end_candidates: list[tuple[str, float]],
        n1_start_candidates: list[tuple[str, float]],
    ) -> str | None:
        """Get the episode that shows continuity, or None."""
        for ep_n, end_t in n_end_candidates:
            for ep_n1, start_t in n1_start_candidates:
                if ep_n == ep_n1 and abs(start_t - end_t) <= cls.CONTINUITY_GAP_TOLERANCE:
                    return ep_n
        return None

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

        # Build adjacency map
        adj: dict[int, int] = {}
        for a, b in pairs:
            adj[a] = b

        # Traverse chains
        chains: list[list[int]] = []
        visited: set[int] = set()

        for start_idx, _ in pairs:
            if start_idx in visited:
                continue

            # Find chain start (not preceded by another pair)
            chain_start = start_idx
            # Build chain forward
            chain = [chain_start]
            visited.add(chain_start)
            current = chain_start

            while current in adj:
                next_idx = adj[current]
                if next_idx in visited:
                    break

                # Check episode consistency
                match_curr = matches.matches[current]
                match_next = matches.matches[next_idx]
                curr_end = cls._get_end_candidates(match_curr)
                next_start = cls._get_start_candidates(match_next)
                ep = cls._get_continuity_episode(curr_end, next_start)

                if ep is None:
                    break

                # Check that this episode is consistent with the chain's episode
                if len(chain) >= 2:
                    prev_match = matches.matches[chain[-2]]
                    prev_end = cls._get_end_candidates(prev_match)
                    curr_start = cls._get_start_candidates(match_curr)
                    chain_ep = cls._get_continuity_episode(prev_end, curr_start)
                    if chain_ep and chain_ep != ep:
                        break

                chain.append(next_idx)
                visited.add(next_idx)
                current = next_idx

            if len(chain) >= 2:
                chains.append(chain)

        return chains

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
