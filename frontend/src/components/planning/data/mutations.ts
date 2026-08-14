import { useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";
import { api } from "@/api/client";
import type { Platform, StealSpec } from "@/types";
import { planningKeys } from "./queries";

/**
 * Precedence-aware scheduling actions.
 *
 * Every action invalidates the planning queries afterwards; 409
 * tiktok_precedence errors are routed through `confirmPrecedence` (a
 * ConfirmDialog) and retried with confirm_before_tiktok. `notify` surfaces
 * non-blocking notices (platform resync pending).
 */
export interface PlanningActionDeps {
  confirmPrecedence: (err: unknown) => Promise<boolean | null>;
  notify: (message: string) => void;
}

const PENDING_RETRY_NOTICE =
  "Certaines replanifications plateforme seront resynchronisées automatiquement.";

function hasPendingRetry(status: unknown): boolean {
  if (!status) return false;
  if (typeof status === "string") return status === "pending_retry";
  if (typeof status === "object") {
    return Object.values(status as Record<string, unknown>).some(hasPendingRetry);
  }
  return false;
}

export function usePlanningActions({ confirmPrecedence, notify }: PlanningActionDeps) {
  const queryClient = useQueryClient();

  const invalidate = useCallback(
    (eligible = false) => {
      void queryClient.invalidateQueries({ queryKey: planningKeys.eventsRoot() });
      void queryClient.invalidateQueries({ queryKey: planningKeys.freeSlotsRoot() });
      if (eligible) {
        void queryClient.invalidateQueries({ queryKey: planningKeys.eligible() });
      }
    },
    [queryClient],
  );

  /** Run `fn(false)`; on a precedence 409, ask the user and retry with true.
   * Returns null when the user declines (no-op). */
  const withPrecedenceRetry = useCallback(
    async <T>(fn: (confirmBeforeTiktok: boolean) => Promise<T>): Promise<T | null> => {
      try {
        return await fn(false);
      } catch (err) {
        const confirmed = await confirmPrecedence(err);
        if (confirmed === null) throw err;
        if (!confirmed) return null;
        return fn(true);
      }
    },
    [confirmPrecedence],
  );

  const reschedulePlatform = useCallback(
    async (projectId: string, platform: Platform, newSlot: string) => {
      const res = await withPrecedenceRetry((confirm) =>
        api.reschedulePlatform(projectId, platform, newSlot, confirm),
      );
      if (res === null) return false;
      if (hasPendingRetry(res.notification_status)) notify(PENDING_RETRY_NOTICE);
      invalidate();
      return true;
    },
    [withPrecedenceRetry, notify, invalidate],
  );

  const switchApply = useCallback(
    async (
      projectId: string,
      payload: {
        account_id: string;
        platform: Platform;
        slot: string;
        mode: StealSpec["mode"];
        expected_occupant_id: string | null;
      },
    ) => {
      const res = await withPrecedenceRetry((confirm) =>
        api.switchApply(projectId, { ...payload, confirm_before_tiktok: confirm }),
      );
      if (res === null) return false;
      if (hasPendingRetry(res.notification_status)) notify(PENDING_RETRY_NOTICE);
      invalidate();
      return true;
    },
    [withPrecedenceRetry, notify, invalidate],
  );

  const rescheduleAnchor = useCallback(
    async (
      projectId: string,
      payload: {
        tiktok_slot: string;
        overrides?: Partial<Record<Platform, string>>;
        steals?: Partial<Record<Platform, StealSpec>>;
      },
    ) => {
      const res = await withPrecedenceRetry((confirm) =>
        api.rescheduleAnchor(projectId, { ...payload, confirm_before_tiktok: confirm }),
      );
      if (res === null) return false;
      if (hasPendingRetry(res.notification_status)) notify(PENDING_RETRY_NOTICE);
      invalidate();
      return true;
    },
    [withPrecedenceRetry, notify, invalidate],
  );

  const reserveManual = useCallback(
    async (
      projectId: string,
      payload: { account_id: string; at: string; platforms?: Platform[] },
    ) => {
      const res = await api.reserveManual(projectId, payload);
      if (hasPendingRetry(res.notification_status)) notify(PENDING_RETRY_NOTICE);
      invalidate(true);
      return true;
    },
    [notify, invalidate],
  );

  const reserveAnchor = useCallback(
    async (
      projectId: string,
      payload: {
        account_id: string;
        tiktok_slot: string;
        overrides?: Partial<Record<Platform, string>>;
        steals?: Partial<Record<Platform, StealSpec>>;
      },
    ) => {
      const res = await withPrecedenceRetry((confirm) =>
        api.reserveAnchor(projectId, { ...payload, confirm_before_tiktok: confirm }),
      );
      if (res === null) return false;
      invalidate(true);
      return true;
    },
    [withPrecedenceRetry, invalidate],
  );

  const cancelPlatform = useCallback(
    async (projectId: string, platform: Platform) => {
      await api.cancelPlatformSlot(projectId, platform);
      invalidate(true);
    },
    [invalidate],
  );

  const cancelAll = useCallback(
    async (projectId: string) => {
      await api.cancelAllSlots(projectId);
      invalidate(true);
    },
    [invalidate],
  );

  return {
    reschedulePlatform,
    switchApply,
    rescheduleAnchor,
    reserveManual,
    reserveAnchor,
    cancelPlatform,
    cancelAll,
  };
}
