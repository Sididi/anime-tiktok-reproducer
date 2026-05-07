import { useCallback, useEffect, useMemo, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { api } from "@/api/client";
import {
  ALL_PLATFORMS,
  type Account,
  type Platform,
  type PlanningEvent,
} from "@/types";
import { PlanningHeader } from "./PlanningHeader";
import { PlanningCalendar } from "./PlanningCalendar";
import { EventPopover } from "./EventPopover";

const LS_ACCOUNT = "atr.planning.account_id";
const LS_PLATFORMS = "atr.planning.platforms";

function readPersistedPlatforms(): Platform[] {
  try {
    const raw = localStorage.getItem(LS_PLATFORMS);
    if (!raw) return [...ALL_PLATFORMS];
    const arr = JSON.parse(raw) as Platform[];
    return arr.length ? arr : [...ALL_PLATFORMS];
  } catch {
    return [...ALL_PLATFORMS];
  }
}

function readPersistedAccount(): string | null {
  try {
    const raw = localStorage.getItem(LS_ACCOUNT);
    return raw && raw !== "null" ? raw : null;
  } catch {
    return null;
  }
}

interface PlanningModalProps {
  open: boolean;
  onClose: () => void;
}

export function PlanningModal({ open, onClose }: PlanningModalProps) {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [events, setEvents] = useState<PlanningEvent[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedAccountId, setSelectedAccountId] = useState<string | null>(
    readPersistedAccount(),
  );
  const [selectedPlatforms, setSelectedPlatforms] = useState<Platform[]>(
    readPersistedPlatforms(),
  );
  const [popover, setPopover] = useState<{
    event: PlanningEvent;
    anchor: { x: number; y: number };
  } | null>(null);

  const selectedAccount = useMemo(
    () => accounts.find((a) => a.id === selectedAccountId) ?? null,
    [accounts, selectedAccountId],
  );

  const reload = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [accountsRes, eventsRes] = await Promise.all([
        api.listAccounts(),
        api.listPlanningEvents({
          account_id: selectedAccountId,
          platforms: selectedPlatforms,
        }),
      ]);
      setAccounts(accountsRes.accounts);
      setEvents(eventsRes.events);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, [selectedAccountId, selectedPlatforms]);

  useEffect(() => {
    if (open) void reload();
  }, [open, reload]);

  useEffect(() => {
    localStorage.setItem(LS_ACCOUNT, selectedAccountId ?? "null");
  }, [selectedAccountId]);

  useEffect(() => {
    localStorage.setItem(LS_PLATFORMS, JSON.stringify(selectedPlatforms));
  }, [selectedPlatforms]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        if (popover) setPopover(null);
        else onClose();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, popover, onClose]);

  if (!open) return null;

  const eventForPopover = popover?.event;
  const projectHasTikTok = !!events.find(
    (e) =>
      e.project_id === eventForPopover?.project_id && e.platform === "tiktok",
  );

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className="fixed inset-0 z-50 bg-black/70 flex items-center justify-center p-6"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          onClick={() => {
            if (popover) return;
            onClose();
          }}
        >
          <motion.div
            className="w-full max-w-7xl h-[88vh] bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-xl flex flex-col overflow-hidden"
            initial={{ scale: 0.95, opacity: 0 }}
            animate={{ scale: 1, opacity: 1 }}
            exit={{ scale: 0.95, opacity: 0 }}
            transition={{ duration: 0.2 }}
            onClick={(e) => e.stopPropagation()}
          >
            <PlanningHeader
              accounts={accounts}
              selectedAccount={selectedAccount}
              onSelectAccount={setSelectedAccountId}
              selectedPlatforms={selectedPlatforms}
              onChangePlatforms={setSelectedPlatforms}
              loading={loading}
              onRefresh={() => void reload()}
              onClose={onClose}
            />
            {error && (
              <div className="mx-6 mt-4 p-3 rounded-md bg-[hsl(var(--destructive))]/10 text-sm text-[hsl(var(--destructive))]">
                {error}
              </div>
            )}
            <div className="flex-1 overflow-hidden p-4">
              <PlanningCalendar
                events={events}
                onEventClick={(event, anchor) => setPopover({ event, anchor })}
              />
            </div>
          </motion.div>

          {popover && eventForPopover && (
            <EventPopover
              event={eventForPopover}
              anchor={popover.anchor}
              onClose={() => setPopover(null)}
              onRescheduleSlot={async () => {
                const newSlotIso = window.prompt(
                  `New slot for ${eventForPopover.platform} (ISO 8601, e.g. 2026-05-08T14:00:00Z):`,
                );
                if (!newSlotIso) return;
                try {
                  await api.reschedulePlatform(
                    eventForPopover.project_id,
                    eventForPopover.platform,
                    newSlotIso,
                  );
                  setPopover(null);
                  await reload();
                } catch (err) {
                  setError((err as Error).message);
                }
              }}
              onRescheduleProject={() => {
                // Phase 3 wires the TT-anchored picker; show a placeholder for now.
                window.alert("Reschedule whole project: available in Phase 3.");
              }}
              rescheduleProjectDisabled={!projectHasTikTok}
              rescheduleProjectDisabledReason={
                projectHasTikTok
                  ? undefined
                  : "This project has no TikTok reservation"
              }
              onCancelSlot={async () => {
                if (
                  !confirm(
                    `Cancel ${eventForPopover.platform} slot for ${eventForPopover.anime_title}?`,
                  )
                )
                  return;
                try {
                  await api.cancelPlatformSlot(
                    eventForPopover.project_id,
                    eventForPopover.platform,
                  );
                  setPopover(null);
                  await reload();
                } catch (err) {
                  setError((err as Error).message);
                }
              }}
              onCancelAll={async () => {
                if (
                  !confirm(
                    `Cancel ALL slots for ${eventForPopover.anime_title}?`,
                  )
                )
                  return;
                try {
                  await api.cancelAllSlots(eventForPopover.project_id);
                  setPopover(null);
                  await reload();
                } catch (err) {
                  setError((err as Error).message);
                }
              }}
            />
          )}
        </motion.div>
      )}
    </AnimatePresence>
  );
}
