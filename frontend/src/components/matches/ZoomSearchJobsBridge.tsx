import { useEffect } from "react";
import { getEventHub } from "@/utils/eventHub";
import { useZoomSearchAlertStore } from "@/stores/zoomSearchAlertStore";
import type { SceneMatch, ZoomSearchJob } from "@/types";

interface ZoomSearchJobsBridgeProps {
  projectId: string;
  onResultMatch?: (match: SceneMatch) => void;
}

/**
 * Headless subscriber keeping the zoom-search alert store in sync with the
 * backend job registry through the shared event stream: the hub replays the
 * current snapshot on subscribe (so unacknowledged alerts survive a refresh)
 * and then delivers live updates, filtered to this project.
 *
 * Deliberately NOT registered in MatchValidation's activeStreamControllers:
 * a Recompute aborts those, and this subscription must outlive it.
 */
export function ZoomSearchJobsBridge({
  projectId,
  onResultMatch,
}: ZoomSearchJobsBridgeProps) {
  const upsertJob = useZoomSearchAlertStore((state) => state.upsertJob);
  const resetLiveJobs = useZoomSearchAlertStore((state) => state.resetLiveJobs);

  useEffect(() => {
    const ingestJob = (job: ZoomSearchJob) => {
      upsertJob(projectId, job);
      if (
        job.status === "complete" &&
        !job.acknowledged &&
        job.result_match
      ) {
        onResultMatch?.(job.result_match);
      }
    };
    // The live-job map is per-project; drop entries from a previous project.
    resetLiveJobs();

    return getEventHub().subscribe<ZoomSearchJob>(
      "zoom_jobs",
      { projectId },
      (event) => {
        if (event.kind === "snapshot") {
          // Project loading reads the persisted matches and remains the
          // source of truth on refresh; the snapshot restores alerts.
          event.items.forEach((item) => ingestJob(item.data));
        } else {
          ingestJob(event.item.data);
        }
      },
    );
  }, [projectId, upsertJob, resetLiveJobs, onResultMatch]);

  return null;
}
