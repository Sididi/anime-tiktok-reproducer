import { useEffect, useRef } from "react";
import { getEventHub } from "@/utils/eventHub";
import { useZoomSearchAlertStore } from "@/stores/zoomSearchAlertStore";
import type { SceneMatch, ZoomSearchJob } from "@/types";

interface ZoomSearchJobsBridgeProps {
  projectId: string;
  // Live completion (not a replay): the job's persisted result for its scene.
  onResultMatch?: (match: SceneMatch, job: ZoomSearchJob) => void;
  // A snapshot replay carried at least one completed, unacknowledged job for
  // this project: the page should refetch persisted matches rather than
  // trust the job's frozen result.
  onReplayedCompletions?: () => void;
}

function isUnseenCompletion(job: ZoomSearchJob): boolean {
  return job.status === "complete" && !job.acknowledged;
}

/**
 * Headless subscriber keeping the zoom-search alert store in sync with the
 * backend job registry through the shared event stream: the hub replays the
 * current snapshot on subscribe (so unacknowledged alerts survive a refresh)
 * and then delivers live updates, filtered to this project.
 *
 * Only LIVE completions apply a job's `result_match` to the page. Snapshot
 * replays (subscribe, SharedWorker hand-over, every stream reconnect) restore
 * alerts only: a replayed `result_match` is a frozen copy that may predate a
 * recompute or a manual edit, and persisted matches are the source of truth.
 *
 * Deliberately NOT registered in MatchValidation's activeStreamControllers:
 * a Recompute aborts those, and this subscription must outlive it.
 */
export function ZoomSearchJobsBridge({
  projectId,
  onResultMatch,
  onReplayedCompletions,
}: ZoomSearchJobsBridgeProps) {
  const upsertJob = useZoomSearchAlertStore((state) => state.upsertJob);
  const resetLiveJobs = useZoomSearchAlertStore((state) => state.resetLiveJobs);
  // Latest-callback refs: the page's handlers may change identity with its
  // state, and re-subscribing would replay the snapshot each time.
  const onResultMatchRef = useRef(onResultMatch);
  const onReplayedCompletionsRef = useRef(onReplayedCompletions);
  useEffect(() => {
    onResultMatchRef.current = onResultMatch;
    onReplayedCompletionsRef.current = onReplayedCompletions;
  });

  useEffect(() => {
    const ingestJob = (job: ZoomSearchJob, live: boolean) => {
      upsertJob(projectId, job);
      if (live && isUnseenCompletion(job) && job.result_match) {
        onResultMatchRef.current?.(job.result_match, job);
      }
    };
    // The live-job map is per-project; drop entries from a previous project.
    resetLiveJobs();

    return getEventHub().subscribe<ZoomSearchJob>(
      "zoom_jobs",
      { projectId },
      (event) => {
        if (event.kind === "snapshot") {
          let replayedCompletion = false;
          for (const item of event.items) {
            ingestJob(item.data, false);
            if (
              item.data.project_id === projectId &&
              isUnseenCompletion(item.data)
            ) {
              replayedCompletion = true;
            }
          }
          if (replayedCompletion) onReplayedCompletionsRef.current?.();
        } else {
          ingestJob(event.item.data, true);
        }
      },
    );
  }, [projectId, upsertJob, resetLiveJobs]);

  return null;
}
