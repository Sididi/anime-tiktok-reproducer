import { useCallback, useEffect, useMemo, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { X } from "lucide-react";
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

/** How far back to fetch events so recently published uploads stay visible. */
const HISTORY_DAYS = 30;

/** Background refresh cadence while the modal is open. */
const POLL_MS = 60_000;

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

function historyStartIso(): string {
  const d = new Date();
  d.setDate(d.getDate() - HISTORY_DAYS);
  d.setHours(0, 0, 0, 0);
  return d.toISOString();
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

  const noPlatformSelected = selectedPlatforms.length === 0;

  const reload = useCallback(
    async (opts?: { silent?: boolean }) => {
      if (!opts?.silent) {
        setLoading(true);
        setError(null);
      }
      try {
        const [accountsRes, eventsRes] = await Promise.all([
          api.listAccounts(),
          // Empty platform selection means "show nothing" — the backend
          // treats a missing `platforms` param as "all", so skip the call.
          selectedPlatforms.length
            ? api.listPlanningEvents({
                account_id: selectedAccountId,
                platforms: selectedPlatforms,
                range_start: historyStartIso(),
              })
            : Promise.resolve({ events: [] as PlanningEvent[] }),
        ]);
        setAccounts(accountsRes.accounts);
        setEvents(eventsRes.events);
        // Drop a persisted account filter that no longer exists — otherwise
        // it silently hides everything with no visible cause.
        if (
          selectedAccountId &&
          !accountsRes.accounts.some((a) => a.id === selectedAccountId)
        ) {
          setSelectedAccountId(null);
        }
      } catch (err) {
        if (!opts?.silent) setError((err as Error).message);
      } finally {
        if (!opts?.silent) setLoading(false);
      }
    },
    [selectedAccountId, selectedPlatforms],
  );

  useEffect(() => {
    if (open) void reload();
  }, [open, reload]);

  // Keep the calendar fresh while it's open: poll in the background and
  // refetch when the window regains focus.
  useEffect(() => {
    if (!open) return;
    const interval = setInterval(() => void reload({ silent: true }), POLL_MS);
    const onFocus = () => void reload({ silent: true });
    window.addEventListener("focus", onFocus);
    return () => {
      clearInterval(interval);
      window.removeEventListener("focus", onFocus);
    };
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
              upcomingCount={
                events.filter((e) => new Date(e.slot).getTime() >= Date.now())
                  .length
              }
            />
            {error && (
              <div className="mx-6 mt-4 p-3 rounded-md bg-[hsl(var(--destructive))]/10 text-sm text-[hsl(var(--destructive))] flex items-start justify-between gap-3">
                <span>{error}</span>
                <button
                  onClick={() => setError(null)}
                  className="p-0.5 rounded hover:bg-[hsl(var(--destructive))]/20 flex-shrink-0"
                  aria-label="Fermer l'erreur"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
            )}
            {noPlatformSelected && (
              <div className="mx-6 mt-4 p-3 rounded-md bg-[hsl(var(--muted))]/50 text-sm text-[hsl(var(--muted-foreground))]">
                Aucune plateforme sélectionnée — activez au moins une
                plateforme pour afficher le planning.
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
                  : "Ce projet n'a pas de réservation TikTok"
              }
              onCancelAll={async () => {
                const m = groupedForPopover.members[0];
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
                if ("slot" in payload && payload.steal) {
                  const res = await api.switchApply(
                    reslottingSingle.project_id,
                    {
                      account_id: reslottingSingle.account_id,
                      platform: reslottingSingle.platform,
                      slot: payload.slot,
                      mode: payload.steal.mode,
                      expected_occupant_id: payload.steal.expected_occupant_id,
                    },
                  );
                  if (
                    Object.values(res.notification_status).includes(
                      "pending_retry",
                    )
                  ) {
                    setError(
                      "Certaines replanifications plateforme seront resynchronisées automatiquement.",
                    );
                  }
                } else if ("slot" in payload) {
                  await api.reschedulePlatform(
                    reslottingSingle.project_id,
                    reslottingSingle.platform,
                    payload.slot,
                  );
                }
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
              initialManual={events.some(
                (e) => e.project_id === reAnchoring.project_id && e.manual,
              )}
              onClose={() => setReAnchoring(null)}
              onConfirm={async (payload) => {
                if ("manual_at" in payload) {
                  const platforms = Array.from(
                    new Set(
                      events
                        .filter((e) => e.project_id === reAnchoring.project_id)
                        .map((e) => e.platform),
                    ),
                  );
                  await api.reserveManual(reAnchoring.project_id, {
                    account_id: reAnchoring.account_id,
                    at: payload.manual_at,
                    platforms,
                  });
                } else if ("tiktok_slot" in payload) {
                  await api.rescheduleAnchor(reAnchoring.project_id, payload);
                }
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
