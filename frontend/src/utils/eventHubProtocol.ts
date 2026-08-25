/**
 * Wire + worker protocol of the shared live-update stream.
 *
 * The backend exposes ONE SSE endpoint (`/api/events/stream`) carrying every
 * job registry. In the browser a SharedWorker owns the single connection and
 * relays frames to every tab, so the app never approaches Chrome's
 * 6-sockets-per-host limit no matter how many tabs are open.
 *
 * This module is imported by the page AND the worker: types and constants
 * only, no DOM / worker globals.
 */

/** Bump when the tab↔worker messages change; the worker name carries it so a
 * stale dev worker (SharedWorkers keep old code until every tab closes) is
 * simply left behind instead of talking a different protocol. */
export const HUB_PROTOCOL_VERSION = 1;
export const HUB_WORKER_NAME = `atr-event-hub-v${HUB_PROTOCOL_VERSION}`;

export const EVENTS_STREAM_PATH = "/api/events/stream";
export const RECONNECT_MS = 3000;
/** Tabs ping the worker at this cadence while visible. */
export const PORT_HEARTBEAT_MS = 5000;
/** A port silent for this long is dropped. Generous on purpose: Chrome
 * throttles hidden tabs' timers to once a minute (and may freeze them), and
 * a tab that comes back re-attaches through the ping/pong check. */
export const PORT_STALE_MS = 150_000;
export const PORT_SWEEP_MS = 30_000;
/** How long a tab waits for the worker's `ready` before using its own connection. */
export const READY_TIMEOUT_MS = 2000;
/** How long a re-visible tab waits for `pong` before re-attaching. */
export const PONG_TIMEOUT_MS = 2000;
/** `localStorage[TRANSPORT_STORAGE_KEY] = "direct"` (or `?hub=direct`) forces
 * a per-tab connection — used by e2e tests, whose fetch mocks cannot reach a
 * SharedWorker. */
export const TRANSPORT_STORAGE_KEY = "atr:eventHub.transport";

export type HubTopic = "startup_jobs" | "upload_jobs" | "index_jobs" | "zoom_jobs";

export interface HubItem<T = unknown> {
  key: string;
  project_id: string | null;
  data: T;
}

/** Frames as sent by the backend (never a top-level `status`). */
export type ServerFrame =
  | { kind: "hello"; server_id: string; topics: string[] }
  | { kind: "snapshot"; topic: string; items: HubItem[] }
  | {
      kind: "event";
      topic: string;
      key: string;
      project_id: string | null;
      data: unknown;
    };

export type HubConnectionStatus = "connecting" | "connected" | "reconnecting";

/** Serialisable copy of the worker's cache: topic → items. */
export type HubCache = Record<string, HubItem[]>;

export type TabToWorkerMessage =
  | { type: "hello"; protocolVersion: number }
  | { type: "ping" }
  | { type: "bye" };

export type WorkerToTabMessage =
  | {
      type: "ready";
      protocolVersion: number;
      status: HubConnectionStatus;
      serverId: string | null;
      cache: HubCache;
    }
  | { type: "pong" }
  | { type: "frame"; frame: ServerFrame }
  | { type: "status"; status: HubConnectionStatus };
