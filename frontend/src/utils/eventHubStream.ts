/**
 * The connection core of the shared live-update stream.
 *
 * Owns one `fetch` of `/api/events/stream`, parses frames, keeps the
 * topic → key → item cache in sync, and reconnects after a fixed delay on
 * BOTH a clean server close and an error (a server restart resolves the
 * reader rather than rejecting it).
 *
 * Used unchanged by the SharedWorker and by the per-tab fallback, so both
 * transports share one code path. No DOM-only or worker-only globals here.
 */

import { readSSEStream } from "./sse";
import {
  EVENTS_STREAM_PATH,
  RECONNECT_MS,
  type HubCache,
  type HubConnectionStatus,
  type HubItem,
  type ServerFrame,
} from "./eventHubProtocol";

export interface HubCoreOptions {
  onFrame: (frame: ServerFrame) => void;
  onStatus: (status: HubConnectionStatus) => void;
  /** Resolved at call time so a test that replaces `globalThis.fetch` is honoured. */
  fetchImpl?: () => typeof fetch;
  reconnectMs?: number;
}

export class HubCore {
  readonly cache = new Map<string, Map<string, HubItem>>();
  serverId: string | null = null;
  status: HubConnectionStatus = "connecting";
  /** Number of stream connections opened so far (diagnostics / tests). */
  connects = 0;

  private readonly options: HubCoreOptions;
  private running = false;
  private controller: AbortController | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(options: HubCoreOptions) {
    this.options = options;
  }

  start(): void {
    if (this.running) return;
    this.running = true;
    void this.connect();
  }

  /** Abort the connection and forget everything (a later `start` begins fresh). */
  stop(): void {
    this.running = false;
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.controller?.abort();
    this.controller = null;
    this.cache.clear();
    this.serverId = null;
    this.status = "connecting";
  }

  cacheSnapshot(): HubCache {
    const out: HubCache = {};
    for (const [topic, items] of this.cache) {
      out[topic] = Array.from(items.values());
    }
    return out;
  }

  private setStatus(status: HubConnectionStatus): void {
    if (this.status === status) return;
    this.status = status;
    this.options.onStatus(status);
  }

  private applyFrame(frame: ServerFrame): void {
    switch (frame.kind) {
      case "hello":
        if (this.serverId !== frame.server_id) {
          // A restarted backend has a new registry; snapshots follow.
          this.cache.clear();
        }
        this.serverId = frame.server_id;
        break;
      case "snapshot":
        this.cache.set(
          frame.topic,
          new Map(frame.items.map((item) => [item.key, item])),
        );
        break;
      case "event": {
        let items = this.cache.get(frame.topic);
        if (!items) {
          items = new Map();
          this.cache.set(frame.topic, items);
        }
        items.set(frame.key, {
          key: frame.key,
          project_id: frame.project_id,
          data: frame.data,
        });
        break;
      }
    }
  }

  private async connect(): Promise<void> {
    const controller = new AbortController();
    this.controller = controller;
    this.connects += 1;
    const fetchImpl = this.options.fetchImpl
      ? this.options.fetchImpl()
      : globalThis.fetch.bind(globalThis);
    try {
      const response = await fetchImpl(EVENTS_STREAM_PATH, {
        signal: controller.signal,
        cache: "no-store",
      });
      // Frames never carry a top-level `status` (the reader would treat
      // "error" as a stream failure); the intersection only satisfies the
      // reader's generic constraint.
      await readSSEStream<ServerFrame & { status?: undefined }>(
        response,
        (frame) => {
          this.applyFrame(frame);
          if (frame.kind === "hello") this.setStatus("connected");
          this.options.onFrame(frame);
        },
        { signal: controller.signal },
      );
    } catch {
      // Network / abort / non-2xx: handled by the reconnect below.
    }
    if (!this.running || controller.signal.aborted) return;
    this.setStatus("reconnecting");
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      if (this.running) void this.connect();
    }, this.options.reconnectMs ?? RECONNECT_MS);
  }
}
