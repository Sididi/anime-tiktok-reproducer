import type { Platform } from "@/types";
import { parisDayKey, weekDaysParis } from "@/utils/parisTime";
import { DayColumn } from "./DayColumn";
import type { GhostSlotItem } from "./GhostSlot";
import type { EventGroup } from "./grouping";

interface WeekBoardProps {
  anchor: Date;
  groups: EventGroup[];
  freeSlots: GhostSlotItem[];
  showGhosts: boolean;
  selectedProjectId: string | null;
  onSelect: (group: EventGroup) => void;
  onGhostClick?: (platform: Platform, slot: string) => void;
}

export function WeekBoard({
  anchor,
  groups,
  freeSlots,
  showGhosts,
  selectedProjectId,
  onSelect,
  onGhostClick,
}: WeekBoardProps) {
  const days = weekDaysParis(anchor);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {!showGhosts && (
        <p className="mb-2 text-center text-[11px] text-[hsl(var(--muted-foreground))]">
          Sélectionnez un compte pour afficher les créneaux libres
        </p>
      )}
      <div className="grid min-h-0 flex-1 grid-cols-7 gap-2 overflow-y-auto pb-4">
        {days.map((day) => {
          const dayKey = parisDayKey(day);
          return (
            <DayColumn
              key={dayKey}
              day={day}
              groups={groups.filter((g) => g.dayKey === dayKey)}
              freeSlots={
                showGhosts ? freeSlots.filter((f) => parisDayKey(f.slot) === dayKey) : []
              }
              selectedProjectId={selectedProjectId}
              onSelect={onSelect}
              onGhostClick={onGhostClick}
            />
          );
        })}
      </div>
    </div>
  );
}
