import { ALL_PLATFORMS, type Account, type Platform } from "@/types";

// Mirror of backend SchedulingService._MIN_LEAD_MINUTES
// (backend/app/services/scheduling_service.py): nothing can be scheduled
// closer than this to now. Keep both sides in sync.
export const MIN_LEAD_MINUTES = 15;
export const MIN_LEAD_MS = MIN_LEAD_MINUTES * 60 * 1000;

/** New integrations are opt-in. Older account responses may only carry the
 * shared slots; those remain a fallback for the four original platforms. */
export function scheduledPlatformsForAccount(account: Account | undefined): Platform[] {
  if (!account) return [];
  return ALL_PLATFORMS.filter((platform) => {
    const inherited = ["youtube", "facebook", "instagram", "tiktok"].includes(platform)
      ? account.slots
      : [];
    return (account.slots_by_platform?.[platform] ?? inherited).length > 0;
  });
}
