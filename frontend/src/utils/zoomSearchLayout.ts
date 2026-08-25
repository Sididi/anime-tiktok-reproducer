import type { Scene, ZoomSearchJob } from "@/types";

const FINGERPRINT_EPS_S = 0.001;

/**
 * True when the job's scene fingerprint still describes the current scene
 * layout (same scene count, same bounds for that index). Jobs without a
 * fingerprint (older backend frames) are trusted.
 */
export function zoomJobMatchesSceneLayout(
  job: ZoomSearchJob,
  scenes: Scene[],
): boolean {
  const fingerprint = job.scene_fingerprint;
  if (!fingerprint) return true;
  if (scenes.length !== fingerprint.count) return false;
  const scene = scenes.find((item) => item.index === job.scene_index);
  if (!scene) return false;
  return (
    Math.abs(scene.start_time - fingerprint.start) <= FINGERPRINT_EPS_S &&
    Math.abs(scene.end_time - fingerprint.end) <= FINGERPRINT_EPS_S
  );
}
