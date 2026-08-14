import clsx from "clsx";
import { format } from "date-fns";
import {
  fmtTime,
  isSameParisDay,
  monthGridParis,
  parisDayKey,
  toParis,
} from "@/utils/parisTime";
import { platformBgHsl } from "./platformColors";
import { groupStatus, type EventGroup } from "./grouping";

const MAX_CHIPS = 3;

interface MonthGridProps {
  anchor: Date;
  groups: EventGroup[];
  selectedProjectId: string | null;
  onSelect: (group: EventGroup) => void;
  onDayClick: (day: Date) => void;
}

export function MonthGrid({ anchor, groups, selectedProjectId, onSelect, onDayClick }: MonthGridProps) {
  const cells = monthGridParis(anchor);
  const anchorMonth = format(toParis(anchor), "yyyy-MM");
  const byDay = new Map<string, EventGroup[]>();
  for (const g of groups) {
    const list = byDay.get(g.dayKey) ?? [];
    list.push(g);
    byDay.set(g.dayKey, list);
  }

  return (
    <div className="grid min-h-0 flex-1 auto-rows-fr grid-cols-7 gap-px overflow-y-auto rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--border))]">
      {cells.map((day) => {
        const dayKey = parisDayKey(day);
        const inMonth = format(day, "yyyy-MM") === anchorMonth;
        const isToday = isSameParisDay(day, new Date());
        const dayGroups = byDay.get(dayKey) ?? [];
        const overflow = dayGroups.length - MAX_CHIPS;

        return (
          <div
            key={dayKey}
            className={clsx(
              "flex min-h-[92px] min-w-0 flex-col gap-1 bg-[hsl(var(--background))] p-1.5",
              !inMonth && "opacity-40",
            )}
          >
            <button
              type="button"
              onClick={() => onDayClick(day)}
              title="Voir cette semaine"
              className={clsx(
                "self-end rounded-full px-1.5 text-[11px] tabular-nums leading-5 transition-colors hover:bg-[hsl(var(--secondary))]",
                isToday
                  ? "bg-[hsl(var(--primary))] font-semibold text-[hsl(var(--primary-foreground))] hover:bg-[hsl(var(--primary))]"
                  : "text-[hsl(var(--muted-foreground))]",
              )}
            >
              {format(day, "d")}
            </button>
            {dayGroups.slice(0, MAX_CHIPS).map((g) => {
              const status = groupStatus(g);
              return (
                <button
                  key={g.key}
                  type="button"
                  onClick={() => onSelect(g)}
                  className={clsx(
                    "flex min-w-0 items-center gap-1 rounded bg-[hsl(var(--card))] px-1 py-0.5 text-left text-[10px] leading-tight transition-colors hover:bg-[hsl(var(--secondary))]",
                    g.projectId === selectedProjectId && "ring-1 ring-[hsl(var(--ring))]",
                    status === "complete" && "opacity-55",
                  )}
                >
                  <span className="tabular-nums text-[hsl(var(--muted-foreground))]">
                    {fmtTime(g.slot)}
                  </span>
                  <span className="flex shrink-0 gap-0.5">
                    {g.members.map((m) => (
                      <span
                        key={m.platform}
                        className="h-1.5 w-1.5 rounded-full"
                        style={{ backgroundColor: platformBgHsl(m.platform) }}
                      />
                    ))}
                  </span>
                  <span className="truncate">{g.members[0].anime_title}</span>
                  {status === "failed" && <span className="shrink-0 text-red-400">✕</span>}
                  {status === "running" && (
                    <span className="planning-status-pulse h-1.5 w-1.5 shrink-0 rounded-full bg-emerald-400" />
                  )}
                </button>
              );
            })}
            {overflow > 0 && (
              <span className="px-1 text-[10px] text-[hsl(var(--muted-foreground))]">
                +{overflow}
              </span>
            )}
          </div>
        );
      })}
    </div>
  );
}
