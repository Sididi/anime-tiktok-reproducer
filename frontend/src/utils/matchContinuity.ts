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

const SAME_EPISODE_BACKTRACK_TOLERANCE_SECONDS = 1.0;
const MANUAL_MERGE_HINT_MAX_DURATION_SECONDS = 3.0;

export type ContinuityClaimKind = "non_continuous" | "episode_change";

export interface ContinuityClaim {
  kind: ContinuityClaimKind;
  badge: "Non-continuous" | "Episode change";
  tooltip: string;
  prevClaimSceneIndex: number | null;
  nextClaimSceneIndex: number | null;
}

interface PendingContinuityClaim {
  kind: ContinuityClaimKind;
  badge: ContinuityClaim["badge"];
  tooltip: string;
}

export interface MatchContinuitySummary {
  claimsBySceneIndex: Record<number, ContinuityClaim>;
  claimedSceneIndices: number[];
}

export function normalizeContinuityEpisodeKey(episode: string): string {
  const trimmed = String(episode || "").trim();
  if (!trimmed) {
    return "";
  }

  const basename = trimmed.split(/[/\\]/).pop()?.trim() || trimmed;
  const lowerBasename = basename.toLowerCase();

  for (const extension of KNOWN_MEDIA_EXTENSIONS) {
    if (lowerBasename.endsWith(extension)) {
      return basename.slice(0, -extension.length).trim().toLowerCase();
    }
  }

  return basename.toLowerCase();
}

function isUsableMatch(match: SceneMatch | undefined): match is SceneMatch {
  return Boolean(
    match &&
      match.confidence > 0 &&
      String(match.episode || "").trim() &&
      Number.isFinite(match.start_time),
  );
}

function addEpisodeKey(keys: Set<string>, episode: string | undefined | null) {
  const normalized = normalizeContinuityEpisodeKey(String(episode || ""));
  if (normalized) {
    keys.add(normalized);
  }
}

export function collectMatchEpisodeKeys(match: SceneMatch | undefined): Set<string> {
  const keys = new Set<string>();
  if (!match) {
    return keys;
  }

  if (isUsableMatch(match)) {
    addEpisodeKey(keys, match.episode);
  }

  for (const alternative of match.alternatives || []) {
    addEpisodeKey(keys, alternative.episode);
  }

  for (const candidate of match.start_candidates || []) {
    addEpisodeKey(keys, candidate.episode);
  }
  for (const candidate of match.middle_candidates || []) {
    addEpisodeKey(keys, candidate.episode);
  }
  for (const candidate of match.end_candidates || []) {
    addEpisodeKey(keys, candidate.episode);
  }

  return keys;
}

export function deriveManualMergeHints(
  scenes: Scene[],
  matches: SceneMatch[],
): Set<number> {
  const matchesBySceneIndex = new Map(
    matches.map((match) => [match.scene_index, match]),
  );
  const hintedSceneIndices = new Set<number>();

  for (let position = 1; position < scenes.length; position += 1) {
    const currentScene = scenes[position];
    const currentDuration = Math.max(
      0,
      currentScene.end_time - currentScene.start_time,
    );
    if (currentDuration >= MANUAL_MERGE_HINT_MAX_DURATION_SECONDS) {
      continue;
    }

    const previousScene = scenes[position - 1];
    const currentKeys = collectMatchEpisodeKeys(
      matchesBySceneIndex.get(currentScene.index),
    );
    const previousKeys = collectMatchEpisodeKeys(
      matchesBySceneIndex.get(previousScene.index),
    );

    if (currentKeys.size === 0 || previousKeys.size === 0) {
      continue;
    }

    for (const key of currentKeys) {
      if (previousKeys.has(key)) {
        hintedSceneIndices.add(currentScene.index);
        break;
      }
    }
  }

  return hintedSceneIndices;
}

export function deriveMatchContinuityClaims(
  scenes: Scene[],
  matches: SceneMatch[],
): MatchContinuitySummary {
  const matchesBySceneIndex = new Map(
    matches.map((match) => [match.scene_index, match]),
  );
  const pendingClaimsBySceneIndex = new Map<number, PendingContinuityClaim>();
  const claimedSceneIndices: number[] = [];
  const episodeRankByKey = new Map<string, number>();

  let nextEpisodeRank = 0;
  let previousMatchedEpisodeKey: string | null = null;
  let previousMatchedEpisodeRank: number | null = null;
  let previousMatchedStartTime: number | null = null;

  for (const scene of scenes) {
    const match = matchesBySceneIndex.get(scene.index);
    if (!isUsableMatch(match)) {
      continue;
    }

    const episodeKey = normalizeContinuityEpisodeKey(match.episode);
    if (!episodeKey) {
      continue;
    }

    const seenEpisodeBefore = episodeRankByKey.has(episodeKey);
    let episodeRank = episodeRankByKey.get(episodeKey);
    if (episodeRank === undefined) {
      episodeRank = nextEpisodeRank;
      episodeRankByKey.set(episodeKey, episodeRank);
      nextEpisodeRank += 1;
    }

    let pendingClaim: PendingContinuityClaim | null = null;

    if (
      previousMatchedEpisodeKey !== null &&
      previousMatchedEpisodeRank !== null &&
      previousMatchedStartTime !== null
    ) {
      if (episodeKey === previousMatchedEpisodeKey) {
        if (
          match.start_time + SAME_EPISODE_BACKTRACK_TOLERANCE_SECONDS <
          previousMatchedStartTime
        ) {
          pendingClaim = {
            kind: "non_continuous",
            badge: "Non-continuous",
            tooltip: "Scene timing goes backward within the same episode.",
          };
        }
      } else if (!seenEpisodeBefore) {
        pendingClaim = {
          kind: "episode_change",
          badge: "Episode change",
          tooltip:
            "Scene switches to a different episode; episode order is unknown.",
        };
      } else if (episodeRank < previousMatchedEpisodeRank) {
        pendingClaim = {
          kind: "non_continuous",
          badge: "Non-continuous",
          tooltip: "Scene returns to an earlier episode.",
        };
      }
    }

    if (pendingClaim) {
      pendingClaimsBySceneIndex.set(scene.index, pendingClaim);
      claimedSceneIndices.push(scene.index);
    }

    previousMatchedEpisodeKey = episodeKey;
    previousMatchedEpisodeRank = episodeRank;
    previousMatchedStartTime = match.start_time;
  }

  const claimsBySceneIndex: Record<number, ContinuityClaim> = {};

  for (const [position, sceneIndex] of claimedSceneIndices.entries()) {
    const pendingClaim = pendingClaimsBySceneIndex.get(sceneIndex);
    if (!pendingClaim) {
      continue;
    }
    claimsBySceneIndex[sceneIndex] = {
      ...pendingClaim,
      prevClaimSceneIndex:
        position > 0 ? claimedSceneIndices[position - 1] : null,
      nextClaimSceneIndex:
        position < claimedSceneIndices.length - 1
          ? claimedSceneIndices[position + 1]
          : null,
    };
  }

  return {
    claimsBySceneIndex,
    claimedSceneIndices,
  };
}
