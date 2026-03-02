/**
 * Video Connection Semaphore
 *
 * Limits the number of concurrent <video> elements actively fetching data.
 * Browsers typically limit ~6 HTTP/1.1 connections per origin. Since the Vite
 * dev-server proxy makes all /api requests same-origin, video fetches compete
 * with regular API calls for the same 6-connection pool.
 *
 * We cap active video loads to MAX_CONCURRENT (4) so that 2 connections remain
 * available for API calls (match data, SSE streams, etc.).
 *
 * Priority support: Fast Watch preloads use `acquirePriority()` which jumps
 * ahead of regular IntersectionObserver-triggered loads in the queue.
 */

const MAX_CONCURRENT = 4;

interface QueueEntry {
  resolve: () => void;
  priority: boolean;
}

let activeCount = 0;
const queue: QueueEntry[] = [];

function tryDequeue(): void {
  while (activeCount < MAX_CONCURRENT && queue.length > 0) {
    const entry = queue.shift()!;
    activeCount++;
    entry.resolve();
  }
}

/**
 * Acquire a slot in the connection pool (normal priority).
 * Resolves immediately if slots are available, otherwise waits in FIFO order.
 * Returns a release function that MUST be called when the video is unloaded.
 */
export function acquire(): Promise<() => void> {
  return new Promise<() => void>((resolve) => {
    const releaseOnce = createRelease();
    if (activeCount < MAX_CONCURRENT) {
      activeCount++;
      resolve(releaseOnce);
    } else {
      queue.push({
        resolve: () => resolve(releaseOnce),
        priority: false,
      });
    }
  });
}

/**
 * Acquire a slot with priority (used by Fast Watch forceLoad).
 * Priority entries jump ahead of normal entries in the queue.
 */
export function acquirePriority(): Promise<() => void> {
  return new Promise<() => void>((resolve) => {
    const releaseOnce = createRelease();
    if (activeCount < MAX_CONCURRENT) {
      activeCount++;
      resolve(releaseOnce);
    } else {
      // Insert before the first non-priority entry
      const insertIdx = queue.findIndex((e) => !e.priority);
      const entry: QueueEntry = {
        resolve: () => resolve(releaseOnce),
        priority: true,
      };
      if (insertIdx === -1) {
        queue.push(entry);
      } else {
        queue.splice(insertIdx, 0, entry);
      }
    }
  });
}

function createRelease(): () => void {
  let released = false;
  return () => {
    if (released) return;
    released = true;
    activeCount = Math.max(0, activeCount - 1);
    tryDequeue();
  };
}

/** Current number of active slots (for debugging). */
export function getActiveCount(): number {
  return activeCount;
}

/** Current queue length (for debugging). */
export function getQueueLength(): number {
  return queue.length;
}
