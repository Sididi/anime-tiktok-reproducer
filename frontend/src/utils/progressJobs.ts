import { getEventHub } from "./eventHub";

export interface ProgressJob {
  id: string;
  project_id: string;
  kind: string;
  parameters: Record<string, unknown>;
  status: "queued" | "running" | "complete" | "error";
  event: { status?: string; error?: string | null; [key: string]: unknown };
  updated_at: string;
  result_available?: boolean;
  result_included?: boolean;
}

/** Adapt hub updates to the existing progress readers without a network stream.
 * Cancelling this response only detaches its observer: the server owns the job.
 */
export function observeProgressJob(job: ProgressJob, signal?: AbortSignal): Response {
  let detach = () => {};
  let cleanup = () => {};
  let stopped = false;
  let lastUpdate = "";
  let fetchingResult = false;
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      const stop = () => {
        if (stopped) return;
        stopped = true;
        detach();
        signal?.removeEventListener("abort", abort);
      };
      cleanup = stop;
      const abort = () => {
        if (stopped) return;
        stop();
        controller.error(new DOMException("Aborted", "AbortError"));
      };
      const accept = (next: ProgressJob) => {
        if (stopped || next.id !== job.id || next.updated_at <= lastUpdate) return;
        if (next.status === "complete" && next.result_available && !next.result_included) {
          if (fetchingResult) return;
          fetchingResult = true;
          void fetch(`/api/operations/${job.id}`, { signal })
            .then(async (response) => {
              if (!response.ok) throw new Error("Impossible de récupérer le résultat. Rechargez le projet.");
              accept(await response.json());
            })
            .catch((error) => { if (!stopped) { stop(); controller.error(error); } });
          return;
        }
        lastUpdate = next.updated_at;
        controller.enqueue(encoder.encode(`data: ${JSON.stringify(next.event)}\n\n`));
        if (next.status === "complete" || next.status === "error") {
          stop();
          controller.close();
        }
      };
      if (signal?.aborted) { abort(); return; }
      signal?.addEventListener("abort", abort, { once: true });
      detach = getEventHub().subscribe<ProgressJob>("progress_jobs", { projectId: job.project_id }, (event) => {
        if (event.kind === "snapshot") event.items.forEach((item) => accept(item.data));
        else accept(event.item.data);
      });
      accept(job);
      // Also fetch once: the hub may have replayed an older snapshot before
      // this POST completed, or the task may finish before we subscribe.
      if (!stopped && !fetchingResult) {
        void fetch(`/api/operations/${job.id}`, { signal })
          .then(async (response) => { if (response.ok) accept(await response.json()); })
          .catch(() => {}); // The shared hub reconnects and replays the state.
      }
    },
    cancel() { cleanup(); },
  });
  return new Response(stream, { headers: { "Content-Type": "text/event-stream" } });
}

export async function startProgressJob(url: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  headers.set("Prefer", "respond-async");
  const response = await fetch(url, { ...init, headers });
  // Preserve regular HTTP errors and support legacy servers / SSE fixtures.
  if (!response.ok || !response.headers.get("content-type")?.includes("application/json")) return response;
  return observeProgressJob(await response.json(), init.signal ?? undefined);
}
