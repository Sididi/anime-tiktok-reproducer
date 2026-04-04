export const MEDIA_PRIORITY = {
  MANUAL_MODAL: 1000,
  ACTIVE_FAST_WATCH: 920,
  FAST_WATCH_PREFETCH_BASE: 860,
  ACTIVE: 720,
  NEAR_VIEWPORT_BASE: 560,
  OFFSCREEN: 120,
  DEDICATED_AUDIO: 950,
} as const;

export function getFastWatchPrefetchPriority(offset: number): number {
  return MEDIA_PRIORITY.FAST_WATCH_PREFETCH_BASE - offset * 10;
}

export function getViewportPriority(distance: number): number {
  return MEDIA_PRIORITY.NEAR_VIEWPORT_BASE - distance * 10;
}
