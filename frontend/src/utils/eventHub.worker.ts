/// <reference lib="webworker" />
/**
 * SharedWorker owning the browser's single `/api/events/stream` connection.
 *
 * Every tab connects a MessagePort, says `hello`, gets `ready` with the
 * current cache, then receives every server frame. Ports are dropped on
 * `bye`, on the port's `close` event (newer browsers), or after a long
 * silence; when the last port is gone the connection is aborted (the browser
 * terminates the worker itself once its last tab closes).
 */

import { HubCore } from "./eventHubStream";
import {
  HUB_PROTOCOL_VERSION,
  PORT_STALE_MS,
  PORT_SWEEP_MS,
  type TabToWorkerMessage,
  type WorkerToTabMessage,
} from "./eventHubProtocol";

const scope = self as unknown as SharedWorkerGlobalScope;

interface PortRecord {
  port: MessagePort;
  lastSeen: number;
}

const ports = new Map<MessagePort, PortRecord>();

function send(record: PortRecord, message: WorkerToTabMessage): void {
  try {
    record.port.postMessage(message);
  } catch {
    dropPort(record.port);
  }
}

function broadcast(message: WorkerToTabMessage): void {
  for (const record of Array.from(ports.values())) {
    send(record, message);
  }
}

const core = new HubCore({
  onFrame: (frame) => broadcast({ type: "frame", frame }),
  onStatus: (status) => broadcast({ type: "status", status }),
});

function dropPort(port: MessagePort): void {
  if (!ports.delete(port)) return;
  try {
    port.close();
  } catch {
    // Already closed.
  }
  if (ports.size === 0) core.stop();
}

function handleMessage(port: MessagePort, message: TabToWorkerMessage): void {
  const record = ports.get(port);
  if (!record) return;
  record.lastSeen = Date.now();
  switch (message.type) {
    case "hello":
      send(record, {
        type: "ready",
        protocolVersion: HUB_PROTOCOL_VERSION,
        status: core.status,
        serverId: core.serverId,
        cache: core.cacheSnapshot(),
      });
      break;
    case "ping":
      send(record, { type: "pong" });
      break;
    case "bye":
      dropPort(port);
      break;
  }
}

scope.onconnect = (event: MessageEvent) => {
  try {
    const port = event.ports[0];
    if (!port) return;
    ports.set(port, { port, lastSeen: Date.now() });
    port.onmessage = (messageEvent: MessageEvent<TabToWorkerMessage>) => {
      try {
        handleMessage(port, messageEvent.data);
      } catch (error) {
        console.error("[eventHub.worker] message handling failed", error);
      }
    };
    // Fired when the other side is closed or its document is discarded
    // (Chrome ≥ 133); older browsers rely on `bye` + the staleness sweep.
    port.addEventListener("close", () => dropPort(port));
    port.start();
    core.start();
  } catch (error) {
    console.error("[eventHub.worker] connect failed", error);
  }
};

setInterval(() => {
  const now = Date.now();
  for (const record of Array.from(ports.values())) {
    if (now - record.lastSeen > PORT_STALE_MS) dropPort(record.port);
  }
}, PORT_SWEEP_MS);
