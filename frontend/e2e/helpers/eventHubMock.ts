/**
 * addInitScript-safe mock of the shared live-update stream.
 *
 *   await page.addInitScript(installEventHubMock, { topics: { index_jobs: [...] } });
 *
 * Install it BEFORE a spec's own `window.fetch` mock so the spec's mock wraps
 * it and falls through to it for `/api/events/stream`.
 *
 * It forces the page onto the per-tab ("direct") transport — Playwright can
 * neither route nor monkeypatch requests issued from a SharedWorker — and
 * serves the hub protocol: `hello`, one `snapshot` per topic, then whatever
 * the test pushes through `window.__atrHub.pushItem(topic, item)`.
 *
 * The function body is serialised into the page: it must not reference
 * anything outside itself except its `config` argument.
 */

export interface HubMockItem {
  key: string;
  project_id: string | null;
  data: Record<string, unknown>;
}

export interface HubMockConfig {
  topics?: Record<string, HubMockItem[]>;
  serverId?: string;
}

export interface HubMockHandle {
  connects: number;
  topics: Record<string, HubMockItem[]>;
  push(frame: unknown): void;
  pushItem(topic: string, item: HubMockItem): void;
  close(): void;
}

export function installEventHubMock(config: HubMockConfig) {
  const DEFAULT_TOPICS = ["startup_jobs", "upload_jobs", "index_jobs", "zoom_jobs"];
  const topics: Record<string, HubMockItem[]> = {};
  for (const topic of DEFAULT_TOPICS) {
    topics[topic] = (config.topics?.[topic] ?? []).map((item) => ({ ...item }));
  }
  for (const [topic, items] of Object.entries(config.topics ?? {})) {
    if (!(topic in topics)) topics[topic] = items.map((item) => ({ ...item }));
  }
  const serverId = config.serverId ?? "mock-server";
  const encoder = new TextEncoder();
  let controller: ReadableStreamDefaultController<Uint8Array> | null = null;
  const pending: string[] = [];
  const frame = (payload: unknown) => `data: ${JSON.stringify(payload)}\n\n`;

  const hub: HubMockHandle = {
    connects: 0,
    topics,
    push(payload) {
      const chunk = frame(payload);
      if (controller) {
        controller.enqueue(encoder.encode(chunk));
      } else {
        pending.push(chunk);
      }
    },
    pushItem(topic, item) {
      const items = (topics[topic] ??= []);
      const index = items.findIndex((existing) => existing.key === item.key);
      if (index >= 0) {
        items[index] = { ...item };
      } else {
        items.push({ ...item });
      }
      hub.push({
        kind: "event",
        topic,
        key: item.key,
        project_id: item.project_id,
        data: item.data,
      });
    },
    close() {
      try {
        controller?.close();
      } catch {
        // Already closed.
      }
      controller = null;
    },
  };
  (window as unknown as { __atrHub: HubMockHandle }).__atrHub = hub;

  try {
    window.localStorage.setItem("atr:eventHub.transport", "direct");
  } catch {
    // Storage unavailable: the page-side client also honours `?hub=direct`.
  }

  const originalFetch = window.fetch.bind(window);
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const requestUrl =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.toString()
          : input.url;
    const url = new URL(requestUrl, window.location.origin);

    if (url.pathname === "/api/events/stream") {
      hub.connects += 1;
      let own: ReadableStreamDefaultController<Uint8Array> | null = null;
      const stream = new ReadableStream<Uint8Array>({
        start(streamController) {
          own = streamController;
          controller = streamController;
          streamController.enqueue(
            encoder.encode(
              frame({ kind: "hello", server_id: serverId, topics: Object.keys(topics) }),
            ),
          );
          for (const [topic, items] of Object.entries(topics)) {
            streamController.enqueue(
              encoder.encode(frame({ kind: "snapshot", topic, items })),
            );
          }
          pending.splice(0).forEach((chunk) => {
            streamController.enqueue(encoder.encode(chunk));
          });
        },
        cancel() {
          if (controller === own) controller = null;
        },
      });
      return new Response(stream, {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      });
    }

    return originalFetch(input, init);
  };
}
