import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api } from "@/api/client";
import type { FreeSlot, Platform } from "@/types";

export const planningKeys = {
  all: ["planning"] as const,
  accounts: () => [...planningKeys.all, "accounts"] as const,
  events: (f: { accountId: string | null; platforms: Platform[]; start: string; end: string }) =>
    [...planningKeys.all, "events", f] as const,
  eventsRoot: () => [...planningKeys.all, "events"] as const,
  freeSlots: (f: { accountId: string; platforms: Platform[]; start: string; end: string }) =>
    [...planningKeys.all, "freeSlots", f] as const,
  freeSlotsRoot: () => [...planningKeys.all, "freeSlots"] as const,
  eligible: () => [...planningKeys.all, "eligible"] as const,
} as const;

export const EVENTS_POLL_MS = 60_000;

export function useAccounts() {
  return useQuery({
    queryKey: planningKeys.accounts(),
    queryFn: () => api.listAccounts(),
    staleTime: 5 * 60_000,
    select: (data) => data.accounts,
  });
}

export function usePlanningEvents(params: {
  accountId: string | null;
  platforms: Platform[];
  start: string;
  end: string;
}) {
  const sorted = [...params.platforms].sort();
  return useQuery({
    queryKey: planningKeys.events({ ...params, platforms: sorted }),
    queryFn: () =>
      api.listPlanningEvents({
        account_id: params.accountId,
        platforms: sorted,
        range_start: params.start,
        range_end: params.end,
      }),
    // Empty platform selection means "show nothing": the backend treats a
    // missing platforms param as "all", so don't call it at all.
    enabled: sorted.length > 0,
    refetchInterval: EVENTS_POLL_MS,
    placeholderData: keepPreviousData,
    select: (data) => data.events,
  });
}

export interface FreeSlotEntry {
  platform: Platform;
  slot: string;
  available: boolean;
}

export function useFreeSlotRange(params: {
  accountId: string | null;
  platforms: Platform[];
  start: string;
  end: string;
  enabled: boolean;
}) {
  const sorted = [...params.platforms].sort();
  return useQuery({
    queryKey: planningKeys.freeSlots({
      accountId: params.accountId ?? "",
      platforms: sorted,
      start: params.start,
      end: params.end,
    }),
    queryFn: () =>
      api.listFreeSlotRange({
        account_id: params.accountId!,
        range_start: params.start,
        range_end: params.end,
        platforms: sorted,
      }),
    enabled: params.enabled && !!params.accountId && sorted.length > 0,
    refetchInterval: EVENTS_POLL_MS,
    placeholderData: keepPreviousData,
    select: (data): FreeSlotEntry[] =>
      (Object.entries(data.slots) as [Platform, FreeSlot[]][]).flatMap(
        ([platform, slots]) =>
          slots
            .filter((s) => s.available)
            .map((s) => ({ platform, slot: s.slot, available: s.available })),
      ),
  });
}

/** Projects ready for quick-assign: fetched only while the panel is open. */
export function useEligibleProjects(enabled: boolean) {
  return useQuery({
    queryKey: planningKeys.eligible(),
    queryFn: () => api.listProjectManagerProjects(),
    enabled,
    staleTime: 0,
    select: (data) =>
      data.projects.filter(
        (p) =>
          p.can_upload_status === "green" &&
          p.uploaded_status === "red" &&
          !p.scheduled_at,
      ),
  });
}
