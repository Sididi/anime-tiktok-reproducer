"""Timing utilities for scene processing.

Contains shared functions for computing timeline positions
across processing and gap resolution services.
"""

from typing import Any, Callable


def compute_adjusted_scene_end_times(
    scenes: list[Any],
    get_scene_index: Callable[[Any], int],
    get_first_word_start: Callable[[Any], float | None],
    get_last_word_end: Callable[[Any], float | None],
) -> dict[int, float]:
    """Compute adjusted end times for scenes to eliminate gaps between them.

    For each scene, the adjusted end time is the start of the next scene's first word,
    ensuring continuous video coverage without gaps from TTS pauses.
    The last scene keeps its original last word end time.

    This solves the problem where TTS has natural pauses between sentences,
    which would otherwise create gaps in the video timeline where no clip
    is placed.

    Args:
        scenes: List of scene objects (transcription scenes or dicts)
        get_scene_index: Function to extract scene_index from a scene
        get_first_word_start: Function to get first word start time from a scene
        get_last_word_end: Function to get last word end time from a scene

    Returns:
        Dictionary mapping scene_index -> adjusted_end_time

    Example:
        If scene 0 has words from 0.0 to 2.5s, and scene 1 has words from 2.8 to 5.0s,
        there's a 0.3s gap. The adjusted end for scene 0 becomes 2.8s (scene 1's start),
        eliminating the gap.
    """
    # Filter scenes that have valid word timings and sort by first word start
    valid_scenes: list[tuple[int, float, float]] = []
    for scene in scenes:
        try:
            start = get_first_word_start(scene)
            end = get_last_word_end(scene)
            if start is not None and end is not None:
                valid_scenes.append((get_scene_index(scene), start, end))
        except (AttributeError, KeyError, IndexError, TypeError):
            continue

    # Sort by first word start time
    valid_scenes.sort(key=lambda x: x[1])

    # Build adjusted end times
    adjusted_ends: dict[int, float] = {}
    for i, (scene_idx, start, original_end) in enumerate(valid_scenes):
        if i < len(valid_scenes) - 1:
            # Use next scene's start as this scene's end
            next_scene_start = valid_scenes[i + 1][1]
            adjusted_ends[scene_idx] = next_scene_start
        else:
            # Last scene keeps its original end
            adjusted_ends[scene_idx] = original_end

    return adjusted_ends
