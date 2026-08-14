import clsx from "clsx";
import type { TZDate } from "@date-fns/tz";
import type { Platform } from "@/types";
import { fmtDayShort, isPastParis, isSameParisDay, startOfDayParis, addDaysParis } from "@/utils/parisTime";
import { EventCard } from "./EventCard";
import { GhostSlot, type GhostSlotItem } from "./GhostSlot";
import type { EventGroup } from "./grouping";

interface DayColumnProps {
  day: TZDate;
  groups: EventGroup[];
  freeSlots: GhostSlotItem[];
  selectedProjectId: string | null;
  onSelect: (group: EventGroup) => void;
  onGhostClick?: (platform: Platform, slot: string) => void;
}

type Item =
  | { kind: "event"; slot: string; group: EventGroup }
  | { kind: "ghost"; slot: string; platform: Platform };

export function DayColumn({
  day,
  groups,
  freeSlots,
  selectedProjectId,
  onSelect,
  onGhostClick,
}: DayColumnProps) {
  const isToday = isSameParisDay(day, new Date());
  const isPastDay = !isToday && isPastParis(addDaysParis(startOfDayParis(day), 1));

  const items: Item[] = [
    ...groups.map((g): Item => ({ kind: "event", slot: g.slot, group: g })),
    ...freeSlots
      .filter((f) => !isPastParis(f.slot))
      .map((f): Item => ({ kind: "ghost", slot: f.slot, platform: f.platform })),
  ].sort((a, b) => a.slot.localeCompare(b.slot));

  return (
    <div className="flex min-w-0 flex-col" data-testid="planning-day-column">
      <div
        className={clsx(
          "mb-2 flex items-baseline justify-center gap-1 rounded-md py-1 text-xs capitalize",
          isToday
            ? "bg-[hsl(var(--primary))]/15 font-semibold text-[hsl(var(--primary))]"
            : "text-[hsl(var(--muted-foreground))]",
        )}
      >
        {fmtDayShort(day)}
      </div>
      <div className={clsx("flex flex-1 flex-col gap-1.5", isPastDay && "opacity-55")}>
        {items.map((item) =>
          item.kind === "event" ? (
            <EventCard
              key={item.group.key}
              group={item.group}
              selected={item.group.projectId === selectedProjectId}
              onClick={onSelect}
            />
          ) : (
            <GhostSlot
              key={`ghost-${item.platform}-${item.slot}`}
              platform={item.platform}
              slot={item.slot}
              onClick={onGhostClick}
            />
          ),
        )}
        {items.length === 0 && (
          <div className="mt-4 text-center text-[11px] text-[hsl(var(--muted-foreground))]/50">
            —
          </div>
        )}
      </div>
    </div>
  );
}
