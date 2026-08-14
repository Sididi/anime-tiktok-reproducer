import type { Scene, SceneMatch } from "@/types";

const KNOWN_MEDIA_EXTENSIONS = [
  ".mkv",
  ".mp4",
  ".mov",
  ".avi",
  ".webm",
  ".m4v",
  ".wav",
  ".mp3",
  ".m4a",
  ".aac",
  ".flac",
  ".ogg",
  ".aiff",
  ".aif",
];

/** Mirror of the backend's canonical_episode_stem: basename without a known media extension. */
export function episodeStem(name: string): string {
  const base = (name || "").trim().split(/[\\/]/).pop() ?? "";
  const lower = base.toLowerCase();
  for (const ext of KNOWN_MEDIA_EXTENSIONS) {
    if (lower.endsWith(ext)) return base.slice(0, -ext.length);
  }
  return base;
}

// A no-match scene's alternatives only vote when they are close enough to
// that scene's best alternative, and always at half the weight of a real
// match: hints may pull in a likely episode, not crowd out confirmed ones.
const ALTERNATIVE_RELATIVE_FLOOR = 0.6;
const ALTERNATIVE_WEIGHT = 0.5;
// Selection: smallest prefix explaining 95% of the score mass, minus
// episodes scoring under 5% of the leader (one-scene misfires).
const CUMULATIVE_MASS_CUTOFF = 0.95;
const NOISE_FLOOR_RATIO = 0.05;

/**
 * Propose the episodes probably really used in the TikTok, from the current
 * matching results. Pure function over already-loaded state — O(scenes ×
 * alternatives), instant even for very large projects.
 *
 * Returns a subset of `episodes` (same values); [] when nothing can be
 * proposed (no matches yet, or none map onto the current episode list).
 */
export function proposeEpisodes(
  matches: SceneMatch[],
  scenes: Scene[],
  episodes: string[],
): string[] {
  if (!matches.length || !episodes.length) return [];

  const byStem = new Map<string, string>();
  for (const episode of episodes) {
    const stem = episodeStem(episode);
    if (stem && !byStem.has(stem)) byStem.set(stem, episode);
  }
  if (!byStem.size) return [];

  const sceneByIndex = new Map<number, Scene>();
  for (const scene of scenes) sceneByIndex.set(scene.index, scene);

  const scores = new Map<string, number>();
  const addScore = (stem: string, value: number) => {
    if (!byStem.has(stem)) return;
    scores.set(stem, (scores.get(stem) ?? 0) + value);
  };

  for (const match of matches) {
    const scene = sceneByIndex.get(match.scene_index);
    const rawDuration = scene?.duration ?? match.end_time - match.start_time;
    const duration = Math.max(
      Number.isFinite(rawDuration) ? rawDuration : 1,
      0.1,
    );

    if (match.episode && !match.was_no_match) {
      addScore(
        episodeStem(match.episode),
        duration * Math.max(match.confidence, 0.2),
      );
      continue;
    }

    const bestByStem = new Map<string, number>();
    for (const alternative of match.alternatives ?? []) {
      const stem = episodeStem(alternative.episode);
      if (!stem) continue;
      const previous = bestByStem.get(stem) ?? 0;
      if (alternative.confidence > previous) {
        bestByStem.set(stem, alternative.confidence);
      }
    }
    if (!bestByStem.size) continue;
    const top = Math.max(...bestByStem.values());
    for (const [stem, confidence] of bestByStem) {
      if (confidence >= ALTERNATIVE_RELATIVE_FLOOR * top) {
        addScore(stem, ALTERNATIVE_WEIGHT * duration * confidence);
      }
    }
  }

  if (!scores.size) return [];

  const ordered = [...scores.entries()].sort(
    (a, b) => b[1] - a[1] || a[0].localeCompare(b[0]),
  );
  const total = ordered.reduce((sum, [, score]) => sum + score, 0);
  const topScore = ordered[0][1];

  const selected: string[] = [];
  let cumulative = 0;
  for (const [stem, score] of ordered) {
    if (cumulative >= CUMULATIVE_MASS_CUTOFF * total) break;
    if (score < NOISE_FLOOR_RATIO * topScore) break;
    selected.push(stem);
    cumulative += score;
  }
  if (!selected.length) selected.push(ordered[0][0]);

  const proposedStems = new Set(selected);
  return episodes.filter((episode) => proposedStems.has(episodeStem(episode)));
}
