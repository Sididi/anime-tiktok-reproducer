import { create } from "zustand";
import type { ZoomSearchJob } from "@/types";

export interface ZoomSearchAlert {
  jobId: string;
  projectId: string;
  sceneIndex: number;
  changed: boolean;
  applied: boolean;
  message: string;
}

export type ZoomSearchSceneStatus = "queued" | "running";

interface ZoomSearchState {
  // Completion alerts, oldest first; deduped by jobId. An alert survives
  // until the owner clicks it, plays the scene, or fixes its timing.
  alerts: ZoomSearchAlert[];
  // Live (queued/running) job status per scene index, current project only —
  // drives the per-card button spinner.
  jobsBySceneIndex: Record<number, { jobId: string; status: ZoomSearchSceneStatus }>;
  upsertJob: (projectId: string, job: ZoomSearchJob) => void;
  // Removes the scene's alerts and returns the dismissed job ids so the
  // caller can ack them server-side (fire-and-forget).
  dismissScene: (projectId: string, sceneIndex: number) => string[];
  clearProject: (projectId: string) => void;
  // Live-job map is scoped to the mounted project; reset it when the
  // /matches page (re)subscribes for a project.
  resetLiveJobs: () => void;
}

export const useZoomSearchAlertStore = create<ZoomSearchState>((set, get) => ({
  alerts: [],
  jobsBySceneIndex: {},

  upsertJob: (projectId, job) =>
    set((state) => {
      const live = { ...state.jobsBySceneIndex };
      if (
        job.project_id === projectId &&
        (job.status === "queued" || job.status === "running")
      ) {
        live[job.scene_index] = { jobId: job.id, status: job.status };
      } else if (live[job.scene_index]?.jobId === job.id) {
        delete live[job.scene_index];
      }

      let alerts = state.alerts;
      if (job.status === "complete" && !job.acknowledged) {
        if (!alerts.some((alert) => alert.jobId === job.id)) {
          alerts = [
            ...alerts,
            {
              jobId: job.id,
              projectId: job.project_id,
              sceneIndex: job.scene_index,
              changed: job.changed === true,
              applied: job.applied === true,
              message: job.message,
            },
          ];
        }
      } else if (
        job.status === "cancelled" ||
        job.status === "error" ||
        job.acknowledged
      ) {
        alerts = alerts.filter((alert) => alert.jobId !== job.id);
      }
      return { jobsBySceneIndex: live, alerts };
    }),

  dismissScene: (projectId, sceneIndex) => {
    const dismissed = get().alerts.filter(
      (alert) => alert.projectId === projectId && alert.sceneIndex === sceneIndex,
    );
    if (dismissed.length > 0) {
      set((state) => ({
        alerts: state.alerts.filter(
          (alert) =>
            !(alert.projectId === projectId && alert.sceneIndex === sceneIndex),
        ),
      }));
    }
    return dismissed.map((alert) => alert.jobId);
  },

  clearProject: (projectId) =>
    set((state) => ({
      alerts: state.alerts.filter((alert) => alert.projectId !== projectId),
      jobsBySceneIndex: {},
    })),

  resetLiveJobs: () => set({ jobsBySceneIndex: {} }),
}));
