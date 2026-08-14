import { useEffect, useState } from "react";
import { api } from "@/api/client";
import { useDownloadProgressStore } from "@/stores/downloadProgressStore";

type UploadSourcePreviewStatus = "loading" | "ready" | "error";

/**
 * Polls the backend until the shared final-video preview cache is ready.
 * The backend warms the cache on the first status call, so mounting this
 * hook is enough to trigger the download.
 */
export function useUploadSourcePreview(
  projectId: string,
  active: boolean,
  projectTitle?: string | null,
) {
  const [status, setStatus] = useState<UploadSourcePreviewStatus>("loading");
  const [version, setVersion] = useState<string>();
  const report = useDownloadProgressStore((s) => s.report);
  const clear = useDownloadProgressStore((s) => s.clear);

  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    let timer: number | undefined;

    const poll = async () => {
      try {
        const result = await api.getUploadSourceStatus(projectId);
        if (cancelled) return;
        if (result.state === "ready") {
          // The version makes the first playable request distinct from any
          // stale/failed response the browser may have seen while the cache
          // file was being prepared.
          setVersion(result.version || String(Date.now()));
          setStatus("ready");
          clear(projectId);
          return;
        }
        if (result.state === "error") {
          setStatus("error");
          clear(projectId);
          return;
        }
        if (result.state === "in_progress") {
          report(projectId, {
            state: "in_progress",
            bytesDone: result.bytes_done,
            bytesTotal: result.bytes_total,
            title: projectTitle,
          });
        }
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
      clear(projectId);
    };
  }, [projectId, active, projectTitle, report, clear]);

  return {
    status,
    url: api.getUploadSourcePreviewUrl(projectId, version),
  };
}
