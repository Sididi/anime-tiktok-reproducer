import { useEffect, useRef } from "react";
import { api } from "@/api/client";
import { readSSEStream } from "@/utils/sse";
import { useZoomSearchAlertStore } from "@/stores/zoomSearchAlertStore";
import type { ZoomSearchJob } from "@/types";

// SSE events are wrapped so a job in the "error" state never puts a
// top-level `status: "error"` on the wire (readSSEStream throws on those).
interface ZoomJobEnvelope {
  kind?: string;
  job?: ZoomSearchJob;
  // Never sent by the server (the envelope exists precisely so job errors
  // don't surface as a top-level status); present for readSSEStream's
  // generic constraint.
  status?: string;
}

interface ZoomSearchJobsBridgeProps {
  projectId: string;
}

/**
 * Headless subscriber keeping the zoom-search alert store in sync with the
 * backend job registry: one snapshot on mount (so unacknowledged alerts
 * survive a refresh), then a reconnecting SSE stream.
 *
 * Deliberately NOT registered in MatchValidation's activeStreamControllers:
 * a Recompute aborts those, and this subscription must outlive it.
 */
export function ZoomSearchJobsBridge({ projectId }: ZoomSearchJobsBridgeProps) {
  const upsertJob = useZoomSearchAlertStore((state) => state.upsertJob);
  const resetLiveJobs = useZoomSearchAlertStore((state) => state.resetLiveJobs);
  const abortRef = useRef<AbortController | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    let disposed = false;
    // The live-job map is per-project; drop entries from a previous project.
    resetLiveJobs();

    api
      .listZoomSearchJobs(projectId)
      .then(({ jobs }) => {
        if (disposed) return;
        jobs.forEach((job) => upsertJob(projectId, job));
      })
      .catch(() => {
        // Snapshot failures are non-fatal; the stream below still connects.
      });

    const connectSSE = () => {
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;

      const scheduleReconnect = () => {
        if (
          disposed ||
          controller.signal.aborted ||
          reconnectTimerRef.current !== null
        ) {
          return;
        }
        reconnectTimerRef.current = setTimeout(() => {
          reconnectTimerRef.current = null;
          connectSSE();
        }, 3000);
      };

      api
        .streamZoomSearchJobs(projectId, controller.signal)
        .then((resp) => {
          readSSEStream<ZoomJobEnvelope>(
            resp,
            (event) => {
              if (event.kind === "zoom_job" && event.job) {
                upsertJob(projectId, event.job);
              }
            },
            { signal: controller.signal },
            // Reconnect on clean end too: the server closing the stream
            // (restart, proxy timeout) resolves rather than rejects.
          ).then(scheduleReconnect, scheduleReconnect);
        })
        .catch(scheduleReconnect);
    };

    connectSSE();
    return () => {
      disposed = true;
      abortRef.current?.abort();
      if (reconnectTimerRef.current !== null) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
    };
  }, [projectId, upsertJob, resetLiveJobs]);

  return null;
}
