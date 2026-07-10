import { useEffect, useState } from "react";
import { api } from "@/api/client";

export type UploadSourcePreviewStatus = "loading" | "ready" | "error";

/**
 * Polls the backend until the shared final-video preview cache is ready.
 * The backend warms the cache on the first status call, so mounting this
 * hook is enough to trigger the download.
 */
export function useUploadSourcePreview(projectId: string, active: boolean) {
  const [status, setStatus] = useState<UploadSourcePreviewStatus>("loading");

  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    let timer: number | undefined;

    const poll = async () => {
      try {
        const result = await api.getUploadSourceStatus(projectId);
        if (cancelled) return;
        if (result.state === "ready") {
          setStatus("ready");
          return;
        }
        if (result.state === "error") {
          setStatus("error");
          return;
        }
      } catch {
        // transient network error: keep polling
      }
      if (!cancelled) {
        timer = window.setTimeout(poll, 2000);
      }
    };

    setStatus("loading");
    void poll();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [projectId, active]);

  return { status, url: api.getUploadSourcePreviewUrl(projectId) };
}
