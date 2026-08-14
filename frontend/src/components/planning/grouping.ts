import type { PlanningEvent } from "@/types";
import { isPastParis, parisDayKey } from "@/utils/parisTime";

/** One card on the board: a project's platforms scheduled at the same instant. */
export interface EventGroup {
  key: string;
  projectId: string;
  slot: string;
  dayKey: string;
  members: PlanningEvent[];
}

export type GroupStatus =
  | "scheduled"
  | "dispatched"
  | "confirming"
  | "running"
  | "complete"
  | "failed"
  | "overdue";

export function groupEvents(events: PlanningEvent[]): EventGroup[] {
  const byKey = new Map<string, EventGroup>();
  for (const ev of events) {
    const key = `${ev.project_id}@${ev.slot}`;
    let group = byKey.get(key);
    if (!group) {
      group = {
        key,
        projectId: ev.project_id,
        slot: ev.slot,
        dayKey: parisDayKey(ev.slot),
        members: [],
      };
      byKey.set(key, group);
    }
    group.members.push(ev);
  }
  const groups = [...byKey.values()];
  for (const g of groups) {
    g.members.sort((a, b) => a.platform.localeCompare(b.platform));
  }
  groups.sort((a, b) => a.slot.localeCompare(b.slot));
  return groups;
}

/** Aggregate status of a card; `overdue`/`confirming` are derived client-side.
 *
 * "dispatched" = handed to the VPS scheduler, publish pending; once the slot
 * has passed it becomes "confirming" (« En attente de confirmation ») instead
 * of the alarming "overdue", which is reserved for platforms with no
 * publication machinery engaged at all. */
export function groupStatus(group: EventGroup): GroupStatus {
  const statuses = group.members.map((m) => m.status);
  if (statuses.includes("running")) return "running";
  if (statuses.includes("failed")) return "failed";
  if (statuses.every((s) => s === "complete")) return "complete";
  const past = isPastParis(group.slot);
  if (statuses.includes("dispatched")) {
    if (!past) return "dispatched";
    // Past slot: a plain "scheduled" sibling never engaged any publication
    // machinery, so the card stays overdue; otherwise the VPS just hasn't
    // confirmed yet.
    return statuses.includes("scheduled") ? "overdue" : "confirming";
  }
  if (past) return "overdue";
  return "scheduled";
}
