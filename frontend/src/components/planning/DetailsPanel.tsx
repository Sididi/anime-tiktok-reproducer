import { CalendarClock, ExternalLink, Lock, X } from "lucide-react";
import clsx from "clsx";
import type { PlanningEvent, Platform } from "@/types";
import { fmtTime, relativeDayLabel } from "@/utils/parisTime";
import { platformBgHsl, platformTranslucentHsl, PLATFORM_LABELS } from "./platformColors";

const STATUS_LABELS: Record<PlanningEvent["status"], string> = {
  scheduled: "Planifié",
  dispatched: "Programmé (serveur)",
  running: "En cours",
  complete: "Publié",
  failed: "Échec",
};

interface DetailsPanelProps {
  events: PlanningEvent[];
  onClose: () => void;
  onReschedulePlatform: (platform: Platform) => void;
  onCancelPlatform: (platform: Platform) => void;
  onRescheduleAnchor: () => void;
  onCancelAll: () => void;
}

/** Right-side panel showing every reservation of the selected project. */
export function DetailsPanel({
  events,
  onClose,
  onReschedulePlatform,
  onCancelPlatform,
  onRescheduleAnchor,
  onCancelAll,
}: DetailsPanelProps) {
  if (events.length === 0) return null;
  const first = events[0];
  const sorted = [...events].sort((a, b) => a.slot.localeCompare(b.slot));
  const hasTikTok = events.some((e) => e.platform === "tiktok");
  const anyLocked = events.some((e) => e.timing_locked);
  const allDone = events.every(
    (e) => e.status !== "scheduled" && e.status !== "dispatched",
  );

  return (
    <aside
      data-testid="planning-details-panel"
      className="flex w-[340px] shrink-0 flex-col border-l border-[hsl(var(--border))] bg-[hsl(var(--card))]"
    >
      <div className="flex items-start gap-2 border-b border-[hsl(var(--border))] p-4">
        <div className="min-w-0 flex-1">
          <h2 className="text-sm font-semibold leading-snug">{first.anime_title}</h2>
          <p className="mt-1 text-xs text-[hsl(var(--muted-foreground))]">{first.account_name}</p>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Fermer le panneau"
          className="rounded p-1 hover:bg-[hsl(var(--secondary))]"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="flex-1 overflow-y-auto p-4">
        <div className="flex flex-col gap-2">
          {sorted.map((ev) => {
            const actionable = ev.status === "scheduled" || ev.status === "dispatched";
            const canMove = actionable && !ev.timing_locked;
            return (
              <div
                key={`${ev.platform}-${ev.slot}`}
                className="rounded-md border border-[hsl(var(--border))] p-2.5"
              >
                <div className="flex items-center gap-2">
                  <span
                    className="rounded px-1.5 py-0.5 text-[10px] font-bold"
                    style={{
                      backgroundColor: platformTranslucentHsl(ev.platform),
                      color: platformBgHsl(ev.platform),
                    }}
                  >
                    {PLATFORM_LABELS[ev.platform]}
                  </span>
                  {ev.status === "complete" && ev.posted_url ? (
                    <a
                      href={ev.posted_url}
                      target="_blank"
                      rel="noreferrer"
                      className="text-[11px] font-medium text-[hsl(var(--primary))] hover:underline"
                    >
                      Publié ↗
                    </a>
                  ) : (
                    <span
                      className={clsx(
                        "text-[11px] font-medium",
                        ev.status === "failed" && "text-red-400",
                        ev.status === "running" && "text-emerald-400",
                        ev.status === "dispatched" && "text-sky-400",
                        ev.status === "complete" && "text-[hsl(var(--muted-foreground))]",
                      )}
                    >
                      {STATUS_LABELS[ev.status]}
                    </span>
                  )}
                  <span className="flex-1" />
                  {ev.timing_locked && (
                    <span title="Verrouillé (créneau TikTok imminent)">
                      <Lock className="h-3 w-3 text-[hsl(var(--muted-foreground))]" />
                    </span>
                  )}
                  {ev.manual && (
                    <span className="rounded bg-amber-400/15 px-1 text-[9px] font-bold text-amber-400">
                      Manuel
                    </span>
                  )}
                </div>
                <div className="mt-1.5 flex items-center gap-1.5 text-xs text-[hsl(var(--muted-foreground))]">
                  <CalendarClock className="h-3.5 w-3.5" />
                  {relativeDayLabel(ev.slot)} à {fmtTime(ev.slot)}
                </div>
                {actionable && (
                  <div className="mt-2 flex gap-1.5">
                    <button
                      type="button"
                      disabled={!canMove}
                      onClick={() => onReschedulePlatform(ev.platform)}
                      title={canMove ? undefined : "Créneau TikTok imminent — modification verrouillée"}
                      className="rounded bg-[hsl(var(--secondary))] px-2 py-1 text-[11px] hover:bg-[hsl(var(--secondary))]/80 disabled:cursor-not-allowed disabled:opacity-40"
                    >
                      Déplacer
                    </button>
                    <button
                      type="button"
                      onClick={() => onCancelPlatform(ev.platform)}
                      className="rounded px-2 py-1 text-[11px] text-[hsl(var(--destructive))] hover:bg-[hsl(var(--destructive))]/15"
                    >
                      Annuler
                    </button>
                  </div>
                )}
              </div>
            );
          })}
        </div>

        {first.drive_folder_url && (
          <a
            href={first.drive_folder_url}
            target="_blank"
            rel="noreferrer"
            className="mt-3 inline-flex items-center gap-1.5 text-xs text-[hsl(var(--primary))] hover:underline"
          >
            <ExternalLink className="h-3.5 w-3.5" />
            Dossier Drive
          </a>
        )}
      </div>

      {!allDone && (
        <div className="flex flex-col gap-2 border-t border-[hsl(var(--border))] p-4">
          <button
            type="button"
            disabled={!hasTikTok || anyLocked}
            onClick={onRescheduleAnchor}
            title={
              !hasTikTok
                ? "Nécessite une réservation TikTok (ancre)"
                : anyLocked
                  ? "Créneau TikTok imminent — modification verrouillée"
                  : undefined
            }
            className="rounded bg-[hsl(var(--secondary))] px-3 py-1.5 text-xs font-medium hover:bg-[hsl(var(--secondary))]/80 disabled:cursor-not-allowed disabled:opacity-40"
          >
            Replanifier le projet
          </button>
          <button
            type="button"
            onClick={onCancelAll}
            className="rounded px-3 py-1.5 text-xs font-medium text-[hsl(var(--destructive))] hover:bg-[hsl(var(--destructive))]/15"
          >
            Tout annuler
          </button>
        </div>
      )}
    </aside>
  );
}
