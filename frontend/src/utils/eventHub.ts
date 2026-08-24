/**
 * Page-side client of the shared live-update stream.
 *
 * `getEventHub().subscribe(topic, { projectId? }, handler)` replays the
 * current snapshot for the topic (if one is known yet) and then delivers
 * every update. Under the hood one SharedWorker per browser owns the single
 * backend connection; a browser without SharedWorker support — or a worker
 * that never answers (stale dev worker, broken build) — falls back to a
 * per-tab connection running the very same `HubCore`.
 *
 * Consumers treat every item as the job's full current state and key off
 * terminal states; intermediate states may be coalesced away upstream.
 */

import EventHubWorker from "./eventHub.worker.ts?sharedworker";
import { HubCore } from "./eventHubStream";
import {
  HUB_PROTOCOL_VERSION,
  HUB_WORKER_NAME,
  PONG_TIMEOUT_MS,
  PORT_HEARTBEAT_MS,
  READY_TIMEOUT_MS,
  TRANSPORT_STORAGE_KEY,
  type HubCache,
  type HubConnectionStatus,
  type HubItem,
  type HubTopic,
  type ServerFrame,
  type WorkerToTabMessage,
} from "./eventHubProtocol";

export type HubEvent<T> =
  | { kind: "snapshot"; items: HubItem<T>[] }
  | { kind: "event"; item: HubItem<T> };

export type HubTransport = "connecting" | "shared" | "direct";

export interface SubscribeOptions {
  /** Only deliver items whose `project_id` matches. */
  projectId?: string;
}

interface Subscription {
  topic: HubTopic;
  projectId?: string;
  handler: (event: HubEvent<never>) => void;
}

export interface EventHubDebugSnapshot {
  transport: HubTransport;
  status: HubConnectionStatus;
  serverId: string | null;
  subscriptions: Record<string, number>;
  cacheSizes: Record<string, number>;
  connects: number;
}

export interface EventHubClient {
  subscribe<T>(
    topic: HubTopic,
    options: SubscribeOptions,
    handler: (event: HubEvent<T>) => void,
  ): () => void;
  onStatus(listener: (status: HubConnectionStatus) => void): () => void;
  transport(): HubTransport;
  debugSnapshot(): EventHubDebugSnapshot;
  dispose(): void;
}

function wantsDirectTransport(): boolean {
  if (typeof SharedWorker === "undefined") return true;
  try {
    if (new URLSearchParams(window.location.search).get("hub") === "direct") {
      return true;
    }
    if (window.localStorage.getItem(TRANSPORT_STORAGE_KEY) === "direct") {
      return true;
    }
  } catch {
    // Storage / location unavailable: prefer the shared worker.
  }
  return false;
}

class EventHubClientImpl implements EventHubClient {
  private readonly subscriptions = new Set<Subscription>();
  private readonly statusListeners = new Set<(status: HubConnectionStatus) => void>();
  private readonly cache = new Map<string, Map<string, HubItem>>();
  private readonly readyTopics = new Set<string>();
  private transportKind: HubTransport = "connecting";
  private status: HubConnectionStatus = "connecting";
  private serverId: string | null = null;
  private started = false;
  private disposed = false;

  private worker: SharedWorker | null = null;
  private port: MessagePort | null = null;
  private direct: HubCore | null = null;
  private readyTimer: number | null = null;
  private heartbeatTimer: number | null = null;
  private pongTimer: number | null = null;
  private sharedConnects = 0;

  subscribe<T>(
    topic: HubTopic,
    options: SubscribeOptions,
    handler: (event: HubEvent<T>) => void,
  ): () => void {
    const subscription: Subscription = {
      topic,
      projectId: options.projectId,
      handler: handler as Subscription["handler"],
    };
    this.subscriptions.add(subscription);
    this.ensureStarted();
    if (this.readyTopics.has(topic)) {
      // Replay asynchronously; skipped if the subscriber is already gone
      // (React StrictMode mounts, unmounts and mounts again synchronously).
      queueMicrotask(() => {
        if (!this.subscriptions.has(subscription)) return;
        subscription.handler({
          kind: "snapshot",
          items: this.itemsFor(topic, subscription.projectId) as HubItem<never>[],
        });
      });
    }
    return () => {
      this.subscriptions.delete(subscription);
    };
  }

  onStatus(listener: (status: HubConnectionStatus) => void): () => void {
    this.statusListeners.add(listener);
    return () => {
      this.statusListeners.delete(listener);
    };
  }

  transport(): HubTransport {
    return this.transportKind;
  }

  debugSnapshot(): EventHubDebugSnapshot {
    const subscriptions: Record<string, number> = {};
    for (const subscription of this.subscriptions) {
      subscriptions[subscription.topic] = (subscriptions[subscription.topic] ?? 0) + 1;
    }
    const cacheSizes: Record<string, number> = {};
    for (const [topic, items] of this.cache) cacheSizes[topic] = items.size;
    return {
      transport: this.transportKind,
      status: this.status,
      serverId: this.serverId,
      subscriptions,
      cacheSizes,
      connects: this.direct ? this.direct.connects : this.sharedConnects,
    };
  }

  dispose(): void {
    this.disposed = true;
    this.stopHeartbeat();
    this.detachWorker(true);
    this.direct?.stop();
    this.direct = null;
    this.subscriptions.clear();
    this.statusListeners.clear();
  }

  // ------------------------------------------------------------------
  // transport selection

  private ensureStarted(): void {
    if (this.started || this.disposed) return;
    this.started = true;
    if (wantsDirectTransport()) {
      this.startDirect();
    } else {
      this.startShared();
    }
  }

  private startShared(): void {
    try {
      const worker = new EventHubWorker({ name: HUB_WORKER_NAME });
      this.worker = worker;
      const port = worker.port;
      this.port = port;
      worker.onerror = (event) => {
        this.fallbackToDirect(`worker error: ${event.message || "unknown"}`);
      };
      port.onmessage = (event: MessageEvent<WorkerToTabMessage>) => {
        this.handleWorkerMessage(event.data);
      };
      port.start();
      this.sharedConnects += 1;
      port.postMessage({ type: "hello", protocolVersion: HUB_PROTOCOL_VERSION });
      this.readyTimer = window.setTimeout(() => {
        this.readyTimer = null;
        this.fallbackToDirect("worker did not answer in time");
      }, READY_TIMEOUT_MS);
    } catch (error) {
      this.fallbackToDirect(`worker unavailable: ${String(error)}`);
    }
  }

  private startDirect(): void {
    this.transportKind = "direct";
    this.direct = new HubCore({
      onFrame: (frame) => this.applyFrame(frame),
      onStatus: (status) => this.setStatus(status),
    });
    this.direct.start();
  }

  private fallbackToDirect(reason: string): void {
    if (this.disposed || this.transportKind === "direct") return;
    console.warn(
      `[eventHub] using a per-tab connection: ${reason}. ` +
        "If this is a stale dev worker, close every tab of the app once.",
    );
    this.stopHeartbeat();
    this.detachWorker(false);
    this.startDirect();
  }

  private detachWorker(sayBye: boolean): void {
    if (this.readyTimer !== null) {
      window.clearTimeout(this.readyTimer);
      this.readyTimer = null;
    }
    const port = this.port;
    if (port) {
      if (sayBye) {
        try {
          port.postMessage({ type: "bye" });
        } catch {
          // Port already gone.
        }
      }
      port.onmessage = null;
      try {
        port.close();
      } catch {
        // Already closed.
      }
    }
    this.port = null;
    if (this.worker) this.worker.onerror = null;
    this.worker = null;
  }

  // ------------------------------------------------------------------
  // shared-worker session

  private handleWorkerMessage(message: WorkerToTabMessage): void {
    switch (message.type) {
      case "ready":
        if (this.readyTimer !== null) {
          window.clearTimeout(this.readyTimer);
          this.readyTimer = null;
        }
        if (message.protocolVersion !== HUB_PROTOCOL_VERSION) {
          this.fallbackToDirect(
            `worker protocol ${message.protocolVersion} ≠ page protocol ${HUB_PROTOCOL_VERSION}`,
          );
          return;
        }
        this.transportKind = "shared";
        this.serverId = message.serverId;
        this.setStatus(message.status);
        this.replaceCache(message.cache);
        this.startHeartbeat();
        break;
      case "pong":
        if (this.pongTimer !== null) {
          window.clearTimeout(this.pongTimer);
          this.pongTimer = null;
        }
        break;
      case "frame":
        this.applyFrame(message.frame);
        break;
      case "status":
        this.setStatus(message.status);
        break;
    }
  }

  private startHeartbeat(): void {
    this.stopHeartbeat();
    this.heartbeatTimer = window.setInterval(() => this.ping(false), PORT_HEARTBEAT_MS);
    window.addEventListener("pagehide", this.handlePageHide);
    document.addEventListener("visibilitychange", this.handleVisibilityChange);
  }

  private stopHeartbeat(): void {
    if (this.heartbeatTimer !== null) {
      window.clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
    if (this.pongTimer !== null) {
      window.clearTimeout(this.pongTimer);
      this.pongTimer = null;
    }
    window.removeEventListener("pagehide", this.handlePageHide);
    document.removeEventListener("visibilitychange", this.handleVisibilityChange);
  }

  /** Ping the worker; with `expectPong` a missing answer means the worker
   * dropped this (long hidden / frozen) tab's port, so re-attach. */
  private ping(expectPong: boolean): void {
    const port = this.port;
    if (!port) return;
    try {
      port.postMessage({ type: "ping" });
    } catch {
      this.reattachWorker();
      return;
    }
    if (expectPong && this.pongTimer === null) {
      this.pongTimer = window.setTimeout(() => {
        this.pongTimer = null;
        this.reattachWorker();
      }, PONG_TIMEOUT_MS);
    }
  }

  private reattachWorker(): void {
    if (this.disposed || this.transportKind !== "shared") return;
    this.stopHeartbeat();
    this.detachWorker(false);
    this.transportKind = "connecting";
    this.startShared();
  }

  private readonly handlePageHide = (): void => {
    // Only a real unload; bfcache restores fire pageshow and keep the port.
    const port = this.port;
    if (!port) return;
    try {
      port.postMessage({ type: "bye" });
    } catch {
      // Port already gone.
    }
  };

  private readonly handleVisibilityChange = (): void => {
    if (document.visibilityState === "visible") this.ping(true);
  };

  // ------------------------------------------------------------------
  // cache + dispatch (both transports)

  private setStatus(status: HubConnectionStatus): void {
    if (this.status === status) return;
    this.status = status;
    for (const listener of this.statusListeners) listener(status);
  }

  private itemsFor(topic: string, projectId?: string): HubItem[] {
    const items = this.cache.get(topic);
    if (!items) return [];
    const all = Array.from(items.values());
    return projectId === undefined
      ? all
      : all.filter((item) => item.project_id === projectId);
  }

  private dispatchSnapshot(topic: string): void {
    for (const subscription of Array.from(this.subscriptions)) {
      if (subscription.topic !== topic) continue;
      subscription.handler({
        kind: "snapshot",
        items: this.itemsFor(topic, subscription.projectId) as HubItem<never>[],
      });
    }
  }

  private replaceCache(cache: HubCache): void {
    for (const [topic, items] of Object.entries(cache)) {
      this.cache.set(topic, new Map(items.map((item) => [item.key, item])));
      this.readyTopics.add(topic);
      this.dispatchSnapshot(topic);
    }
  }

  private applyFrame(frame: ServerFrame): void {
    switch (frame.kind) {
      case "hello":
        if (this.serverId !== frame.server_id) {
          this.cache.clear();
          this.readyTopics.clear();
        }
        this.serverId = frame.server_id;
        break;
      case "snapshot":
        this.cache.set(
          frame.topic,
          new Map(frame.items.map((item) => [item.key, item])),
        );
        this.readyTopics.add(frame.topic);
        this.dispatchSnapshot(frame.topic);
        break;
      case "event": {
        const item: HubItem = {
          key: frame.key,
          project_id: frame.project_id,
          data: frame.data,
        };
        let items = this.cache.get(frame.topic);
        if (!items) {
          items = new Map();
          this.cache.set(frame.topic, items);
        }
        items.set(item.key, item);
        for (const subscription of Array.from(this.subscriptions)) {
          if (subscription.topic !== frame.topic) continue;
          if (
            subscription.projectId !== undefined &&
            item.project_id !== subscription.projectId
          ) {
            continue;
          }
          subscription.handler({ kind: "event", item: item as HubItem<never> });
        }
        break;
      }
    }
  }
}

const GLOBAL_KEY = "__atrEventHub";

type HubGlobal = typeof globalThis & { [GLOBAL_KEY]?: EventHubClient };

/** One client per page; kept on `globalThis` so Vite HMR module swaps never
 * open a second connection (a full reload picks up new hub code). */
export function getEventHub(): EventHubClient {
  const host = globalThis as HubGlobal;
  if (!host[GLOBAL_KEY]) {
    host[GLOBAL_KEY] = new EventHubClientImpl();
  }
  return host[GLOBAL_KEY];
}
