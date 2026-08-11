import { useEffect, useState } from "react";
import { api } from "@/api/client";
import type { ThumbnailCandidate } from "@/types";

type ThumbnailCandidatesStatus = "loading" | "ready" | "error";

/**
 * Polls the thumbnail-candidates endpoint until frames are extracted.
 * The endpoint warms the shared upload_source cache on first call, so
 * mounting this hook is enough to trigger the whole pipeline.
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
        if (result.state === "ready" && result.candidates?.length) {
          setCandidates(result.candidates);
          setStatus("ready");
          return;
        }
        if (result.state === "error") {
          setDetail(result.detail);
          setStatus("error");
          return;
        }
        // "in_progress" / "missing": keep polling, the backend is warming up.
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
