import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ArrowLeft, CalendarDays, RefreshCw, X } from "lucide-react";
import type { Platform, PlanningEvent } from "@/types";
import {
  addDaysParis,
  addMonthsParis,
  fmtMonthYear,
  fmtWeekRange,
  monthGridParis,
  nowParis,
  startOfDayParis,
  startOfWeekParis,
} from "@/utils/parisTime";
import { PlanningToolbar, type PlanningView } from "@/components/planning/PlanningToolbar";
import { WeekBoard } from "@/components/planning/WeekBoard";
import { MonthGrid } from "@/components/planning/MonthGrid";
import { AgendaList } from "@/components/planning/AgendaList";
import { DetailsPanel } from "@/components/planning/DetailsPanel";
import { QuickAssignPanel } from "@/components/planning/QuickAssignPanel";
import { SlotPickerPopover } from "@/components/project-manager/SlotPickerPopover";
import { groupEvents, type EventGroup } from "@/components/planning/grouping";
import { usePlanningFilters } from "@/components/planning/usePlanningFilters";
import { usePrecedenceConfirm } from "@/components/planning/usePrecedenceConfirm";
import { useConfirmDialog } from "@/components/planning/ConfirmDialog";
import { usePlanningActions } from "@/components/planning/data/mutations";
import {
  useAccounts,
  useFreeSlotRange,
  usePlanningEvents,
} from "@/components/planning/data/queries";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 30_000, refetchOnWindowFocus: true, retry: 1 },
  },
});

const AGENDA_DAYS = 30;

interface Toast {
  id: number;
  message: string;
}

export function PlanningPage() {
  return (
    <QueryClientProvider client={queryClient}>
      <PlanningPageInner />
    </QueryClientProvider>
  );
}

function PlanningPageInner() {
  const [searchParams, setSearchParams] = useSearchParams();
  const view = (searchParams.get("view") as PlanningView) || "week";
  const selectedProjectId = searchParams.get("project");

  const [anchor, setAnchor] = useState<Date>(() => new Date());
  const { accountId, setAccountId, platforms, setPlatforms } = usePlanningFilters();
  const [quickAssignSlot, setQuickAssignSlot] = useState<string | null>(null);
  const [reslottingSingle, setReslottingSingle] = useState<PlanningEvent | null>(null);
  const [reAnchoring, setReAnchoring] = useState<PlanningEvent | null>(null);
  const [toasts, setToasts] = useState<Toast[]>([]);

  const notify = useCallback((message: string) => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, message }]);
    window.setTimeout(() => {
      setToasts((t) => t.filter((toast) => toast.id !== id));
    }, 6000);
  }, []);

  const { confirmPrecedence, dialog: precedenceDialog } = usePrecedenceConfirm();
  const { confirm, dialog: confirmDialog } = useConfirmDialog();
  const actions = usePlanningActions({ confirmPrecedence, notify });

  const setView = (v: PlanningView) =>
    setSearchParams((prev) => {
      prev.set("view", v);
      return prev;
    });
  const setSelectedProject = useCallback(
    (projectId: string | null) =>
      setSearchParams((prev) => {
        if (projectId) prev.set("project", projectId);
        else prev.delete("project");
        return prev;
      }),
    [setSearchParams],
  );

  // Paris-derived fetch range for the active view.
  const range = useMemo(() => {
    if (view === "month") {
      const cells = monthGridParis(anchor);
      return {
        start: cells[0].toISOString(),
        end: addDaysParis(cells[cells.length - 1], 1).toISOString(),
      };
    }
    if (view === "agenda") {
      const start = startOfDayParis(nowParis());
      return {
        start: start.toISOString(),
        end: addDaysParis(start, AGENDA_DAYS).toISOString(),
      };
    }
    const start = startOfWeekParis(anchor);
    return { start: start.toISOString(), end: addDaysParis(start, 7).toISOString() };
  }, [view, anchor]);

  const accounts = useAccounts();
  const events = usePlanningEvents({ accountId, platforms, ...range });
  const freeSlots = useFreeSlotRange({
    accountId,
    platforms,
    ...range,
    enabled: view === "week",
  });

  // Drop a persisted account filter that no longer exists — otherwise it
  // silently hides everything with no visible cause.
  useEffect(() => {
    if (accountId && accounts.data && !accounts.data.some((a) => a.id === accountId)) {
      setAccountId(null);
    }
  }, [accountId, accounts.data, setAccountId]);

  const eventList = useMemo(() => events.data ?? [], [events.data]);
  const groups = useMemo(() => groupEvents(eventList), [eventList]);
  const selectedEvents = useMemo(
    () => eventList.filter((ev) => ev.project_id === selectedProjectId),
    [eventList, selectedProjectId],
  );
  const selectedAccount = accounts.data?.find((a) => a.id === accountId) ?? null;

  const periodLabel =
    view === "month"
      ? fmtMonthYear(anchor)
      : view === "week"
        ? fmtWeekRange(startOfWeekParis(anchor))
        : `${AGENDA_DAYS} prochains jours`;

  const navigate = (dir: 1 | -1) => {
    setAnchor((a) => (view === "month" ? addMonthsParis(a, dir) : addDaysParis(a, dir * 7)));
  };

  const onSelectGroup = (group: EventGroup) => {
    setQuickAssignSlot(null);
    setSelectedProject(group.projectId === selectedProjectId ? null : group.projectId);
  };

  const onGhostClick = (_platform: Platform, slot: string) => {
    setSelectedProject(null);
    setQuickAssignSlot(slot);
  };

  const projectPlatforms = (projectId: string): Platform[] =>
    Array.from(
      new Set(
        eventList.filter((e) => e.project_id === projectId).map((e) => e.platform),
      ),
    );

  return (
    <div className="flex h-screen flex-col">
      <header className="flex items-center gap-3 border-b border-[hsl(var(--border))] px-4 py-2.5">
        <Link
          to="/"
          className="flex items-center gap-1.5 rounded px-2 py-1 text-sm text-[hsl(var(--muted-foreground))] transition-colors hover:bg-[hsl(var(--secondary))] hover:text-[hsl(var(--foreground))]"
        >
          <ArrowLeft className="h-4 w-4" />
          Librairie
        </Link>
        <div className="w-px h-5 bg-[hsl(var(--border))]" />
        <CalendarDays className="h-4 w-4 text-[hsl(var(--primary))]" />
        <h1 className="text-sm font-bold">Planning</h1>
        {events.isFetching && (
          <RefreshCw className="h-3.5 w-3.5 animate-spin text-[hsl(var(--muted-foreground))]" />
        )}
      </header>

      <div className="flex min-h-0 flex-1">
        <main className="flex min-w-0 flex-1 flex-col gap-3 p-4">
          <PlanningToolbar
            view={view}
            onViewChange={setView}
            periodLabel={periodLabel}
            onPrev={() => navigate(-1)}
            onNext={() => navigate(1)}
            onToday={() => setAnchor(new Date())}
            accounts={accounts.data ?? []}
            accountId={accountId}
            onAccountChange={setAccountId}
            platforms={platforms}
            onPlatformsChange={setPlatforms}
          />

          {events.isError && (
            <div className="rounded-md bg-[hsl(var(--destructive))]/10 p-3 text-sm text-[hsl(var(--destructive))]">
              {(events.error as Error).message}
            </div>
          )}
          {platforms.length === 0 && (
            <div className="rounded-md bg-[hsl(var(--muted))]/50 p-3 text-sm text-[hsl(var(--muted-foreground))]">
              Aucune plateforme sélectionnée — activez au moins une plateforme pour
              afficher le planning.
            </div>
          )}

          {view === "week" && (
            <WeekBoard
              anchor={anchor}
              groups={groups}
              freeSlots={freeSlots.data ?? []}
              showGhosts={accountId !== null}
              selectedProjectId={selectedProjectId}
              onSelect={onSelectGroup}
              onGhostClick={onGhostClick}
            />
          )}
          {view === "month" && (
            <MonthGrid
              anchor={anchor}
              groups={groups}
              selectedProjectId={selectedProjectId}
              onSelect={onSelectGroup}
              onDayClick={(day) => {
                setAnchor(day);
                setView("week");
              }}
            />
          )}
          {view === "agenda" && (
            <AgendaList
              groups={groups.filter((g) => new Date(g.slot) >= new Date())}
              selectedProjectId={selectedProjectId}
              onSelect={onSelectGroup}
            />
          )}
        </main>

        {selectedProjectId && selectedEvents.length > 0 && (
          <DetailsPanel
            events={selectedEvents}
            onClose={() => setSelectedProject(null)}
            onReschedulePlatform={(platform) => {
              const ev = selectedEvents.find((e) => e.platform === platform);
              if (ev) setReslottingSingle(ev);
            }}
            onCancelPlatform={(platform) => {
              const ev = selectedEvents.find((e) => e.platform === platform);
              if (!ev) return;
              void (async () => {
                const ok = await confirm({
                  title: "Annuler ce créneau ?",
                  body: `La réservation ${platform} de « ${ev.anime_title} » sera libérée.`,
                  confirmLabel: "Annuler le créneau",
                  cancelLabel: "Garder",
                  destructive: true,
                });
                if (!ok) return;
                try {
                  await actions.cancelPlatform(ev.project_id, ev.platform);
                } catch (err) {
                  notify(`Échec de l'annulation : ${(err as Error).message}`);
                }
              })();
            }}
            onRescheduleAnchor={() => {
              const tt = selectedEvents.find((e) => e.platform === "tiktok");
              if (tt) setReAnchoring(tt);
            }}
            onCancelAll={() => {
              const first = selectedEvents[0];
              void (async () => {
                const ok = await confirm({
                  title: "Tout annuler ?",
                  body: `Toutes les réservations de « ${first.anime_title} » seront libérées.`,
                  confirmLabel: "Tout annuler",
                  cancelLabel: "Garder",
                  destructive: true,
                });
                if (!ok) return;
                try {
                  await actions.cancelAll(first.project_id);
                  setSelectedProject(null);
                } catch (err) {
                  notify(`Échec de l'annulation : ${(err as Error).message}`);
                }
              })();
            }}
          />
        )}
        {quickAssignSlot && selectedAccount && (
          <QuickAssignPanel
            slot={quickAssignSlot}
            account={selectedAccount}
            onClose={() => setQuickAssignSlot(null)}
            onReserveAnchor={actions.reserveAnchor}
            onReserveManual={actions.reserveManual}
            notify={notify}
          />
        )}
      </div>

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
            let done = false;
            if ("slot" in payload && payload.steal) {
              done = await actions.switchApply(reslottingSingle.project_id, {
                account_id: reslottingSingle.account_id,
                platform: reslottingSingle.platform,
                slot: payload.slot,
                mode: payload.steal.mode,
                expected_occupant_id: payload.steal.expected_occupant_id,
              });
            } else if ("slot" in payload) {
              done = await actions.reschedulePlatform(
                reslottingSingle.project_id,
                reslottingSingle.platform,
                payload.slot,
              );
            }
            if (done) setReslottingSingle(null);
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
            selectedEvents.find((e) => e.platform === "tiktok")?.slot ?? reAnchoring.slot
          }
          platformsForAnchor={projectPlatforms(reAnchoring.project_id)}
          initialManual={selectedEvents.some((e) => e.manual)}
          onClose={() => setReAnchoring(null)}
          onConfirm={async (payload) => {
            let done = false;
            if ("manual_at" in payload) {
              done = await actions.reserveManual(reAnchoring.project_id, {
                account_id: reAnchoring.account_id,
                at: payload.manual_at,
                platforms: projectPlatforms(reAnchoring.project_id),
              });
            } else if ("tiktok_slot" in payload) {
              done = await actions.rescheduleAnchor(reAnchoring.project_id, payload);
            }
            if (done) setReAnchoring(null);
          }}
        />
      )}

      {precedenceDialog}
      {confirmDialog}

      {toasts.length > 0 && (
        <div className="fixed bottom-4 right-4 z-[80] flex w-80 flex-col gap-2">
          {toasts.map((toast) => (
            <div
              key={toast.id}
              className="flex items-start gap-2 rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-3 text-xs shadow-lg"
            >
              <span className="flex-1">{toast.message}</span>
              <button
                type="button"
                onClick={() => setToasts((t) => t.filter((x) => x.id !== toast.id))}
                aria-label="Fermer la notification"
                className="rounded p-0.5 hover:bg-[hsl(var(--secondary))]"
              >
                <X className="h-3 w-3" />
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
