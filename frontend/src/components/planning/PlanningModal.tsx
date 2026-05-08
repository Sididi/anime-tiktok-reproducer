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
import { SlotPickerPopover } from "@/components/project-manager/SlotPickerPopover";

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

interface GroupedEventClick {
  project_id: string;
  slot: string;
  members: PlanningEvent[];
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
    grouped: GroupedEventClick;
    anchor: { x: number; y: number };
  } | null>(null);
  const [reslottingSingle, setReslottingSingle] = useState<PlanningEvent | null>(
    null,
  );
  const [reAnchoring, setReAnchoring] = useState<PlanningEvent | null>(null);

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

  const groupedForPopover = popover?.grouped;
  const projectHasTikTok = !!groupedForPopover?.members.some(
    (m) => m.platform === "tiktok",
  );

  const memberFor = (platform: Platform): PlanningEvent | undefined =>
    groupedForPopover?.members.find((m) => m.platform === platform);

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
            className="w-[96vw] max-w-[1700px] h-[92vh] bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-xl flex flex-col overflow-hidden"
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
            <div className="flex-1 min-h-0 overflow-hidden">
              <PlanningCalendar
                events={events}
                onEventClick={(grouped, anchor) =>
                  setPopover({ grouped, anchor })
                }
              />
            </div>
          </motion.div>

          {popover && groupedForPopover && (
            <EventPopover
              members={groupedForPopover.members}
              anchor={popover.anchor}
              onClose={() => setPopover(null)}
              onReschedulePlatform={(platform) => {
                const m = memberFor(platform);
                if (m) setReslottingSingle(m);
              }}
              onCancelPlatform={async (platform) => {
                const m = memberFor(platform);
                if (!m) return;
                if (
                  !confirm(
                    `Cancel ${platform.toUpperCase()} slot for ${m.anime_title}?`,
                  )
                )
                  return;
                try {
                  await api.cancelPlatformSlot(m.project_id, m.platform);
                  setPopover(null);
                  await reload();
                } catch (err) {
                  setError((err as Error).message);
                }
              }}
              onRescheduleProject={() => {
                const tt = memberFor("tiktok");
                if (tt) setReAnchoring(tt);
              }}
              rescheduleProjectDisabled={!projectHasTikTok}
              rescheduleProjectDisabledReason={
                projectHasTikTok
                  ? undefined
                  : "This project has no TikTok reservation"
              }
              onCancelAll={async () => {
                const m = groupedForPopover.members[0];
                if (
                  !confirm(`Cancel ALL platforms for ${m.anime_title}?`)
                )
                  return;
                try {
                  await api.cancelAllSlots(m.project_id);
                  setPopover(null);
                  await reload();
                } catch (err) {
                  setError((err as Error).message);
                }
              }}
            />
          )}

          {reslottingSingle && (
            <SlotPickerPopover
              open
              mode="single-platform"
              projectId={reslottingSingle.project_id}
              accountId={reslottingSingle.account_id}
              platform={reslottingSingle.platform}
              platformsForAnchor={[reslottingSingle.platform]}
              initialIso={reslottingSingle.slot}
              onClose={() => setReslottingSingle(null)}
              onConfirm={async (payload) => {
                const single = payload as { slot: string };
                await api.reschedulePlatform(
                  reslottingSingle.project_id,
                  reslottingSingle.platform,
                  single.slot,
                );
                setReslottingSingle(null);
                setPopover(null);
                await reload();
              }}
            />
          )}

          {reAnchoring && (
            <SlotPickerPopover
              open
              mode="anchor"
              projectId={reAnchoring.project_id}
              accountId={reAnchoring.account_id}
              initialIso={
                events.find(
                  (e) =>
                    e.project_id === reAnchoring.project_id &&
                    e.platform === "tiktok",
                )?.slot
              }
              platformsForAnchor={Array.from(
                new Set(
                  events
                    .filter((e) => e.project_id === reAnchoring.project_id)
                    .map((e) => e.platform),
                ),
              )}
              onClose={() => setReAnchoring(null)}
              onConfirm={async (payload) => {
                const anchor = payload as {
                  tiktok_slot: string;
                  overrides?: Partial<Record<Platform, string>>;
                };
                await api.rescheduleAnchor(reAnchoring.project_id, anchor);
                setReAnchoring(null);
                setPopover(null);
                await reload();
              }}
            />
          )}
        </motion.div>
      )}
    </AnimatePresence>
  );
}
