import { useEffect, useState } from "react";
import { api } from "@/api/client";
import type { ThumbnailCandidate } from "@/types";

type ThumbnailCandidatesStatus = "loading" | "partial" | "ready" | "error";

/**
 * Polls the thumbnail-candidates endpoint until frames are extracted.
 * The endpoint warms the shared upload_source cache on first call, so
 * mounting this hook is enough to trigger the whole pipeline.
 *
 * Candidates are published on every poll so the modal can render "clean"
 * tiles (fast) while "output" tiles are still being produced. Status stays
 * "loading" until the first candidates arrive, then tracks the backend
 * state: "partial" keeps polling, "ready"/"error" are terminal.
 */
export function useThumbnailCandidates(projectId: string, active: boolean) {
  const [status, setStatus] = useState<ThumbnailCandidatesStatus>("loading");
  const [candidates, setCandidates] = useState<ThumbnailCandidate[]>([]);
  const [detail, setDetail] = useState<string>();

  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    let timer: number | undefined;

    const poll = async () => {
      try {
        const result = await api.getThumbnailCandidates(projectId);
        if (cancelled) return;
        if (result.candidates) {
          setCandidates(result.candidates);
        }
        if (result.state === "ready" && result.candidates?.length) {
          setStatus("ready");
          return;
        }
        if (result.state === "error") {
          setDetail(result.detail);
          setStatus("error");
          return;
        }
        if (result.state === "partial") {
          setStatus("partial");
        }
        // "in_progress" / network errors: keep polling, status stays
        // "loading" until the first candidates arrive.
      } catch {
        // transient network error: keep polling
      }
      if (!cancelled) {
        timer = window.setTimeout(poll, 2000);
      }
    };

    void poll();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [projectId, active]);

  return { status, candidates, detail };
}
