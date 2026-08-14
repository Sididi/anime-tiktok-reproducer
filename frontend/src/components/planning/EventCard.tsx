import { useState } from "react";
import { Hourglass, Lock } from "lucide-react";
import clsx from "clsx";
import { fmtTime } from "@/utils/parisTime";
import { platformBgHsl, platformTranslucentHsl, PLATFORM_SHORT } from "./platformColors";
import { groupStatus, type EventGroup, type GroupStatus } from "./grouping";

function Avatar({ url, name }: { url: string; name: string }) {
  const [failed, setFailed] = useState(false);
  if (failed) {
    return (
      <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-[hsl(var(--secondary))] text-[9px] font-semibold uppercase text-[hsl(var(--muted-foreground))]">
        {name.charAt(0)}
      </span>
    );
  }
  return (
    <img
      src={url}
      alt={name}
      className="h-5 w-5 shrink-0 rounded-full object-cover"
      onError={() => setFailed(true)}
    />
  );
}

function StatusDot({ status }: { status: GroupStatus }) {
  switch (status) {
    case "running":
      return <span className="planning-status-pulse h-2 w-2 rounded-full bg-emerald-400" />;
    case "complete":
      return <span className="text-[11px] leading-none text-emerald-500">✓</span>;
    case "failed":
      return <span className="h-2 w-2 rounded-full bg-red-500" />;
    case "overdue":
      return <span className="text-[11px] font-bold leading-none text-amber-400">!</span>;
    case "dispatched":
      return <Hourglass className="h-3 w-3 text-sky-400" />;
    case "confirming":
      return <Hourglass className="planning-status-pulse h-3 w-3 text-sky-400" />;
    default:
      return <span className="h-2 w-2 rounded-full bg-[hsl(var(--muted-foreground))]/50" />;
  }
}

const STATUS_LABELS: Record<GroupStatus, string | null> = {
  scheduled: null,
  dispatched: "Programmé",
  confirming: "En attente de confirmation",
  running: "En cours",
  complete: null,
  failed: "Échec",
  overdue: "En retard",
};

interface EventCardProps {
  group: EventGroup;
  selected?: boolean;
  variant?: "board" | "agenda";
  onClick?: (group: EventGroup) => void;
}

export function EventCard({ group, selected, variant = "board", onClick }: EventCardProps) {
  const status = groupStatus(group);
  const first = group.members[0];
  const manual = group.members.some((m) => m.manual);
  const locked = group.members.some((m) => m.timing_locked);
  const statusLabel = STATUS_LABELS[status];

  return (
    <button
      type="button"
      data-testid="planning-event-card"
      data-project-id={group.projectId}
      onClick={() => onClick?.(group)}
      className={clsx(
        "w-full rounded-md border bg-[hsl(var(--card))] text-left transition-colors",
        "hover:border-[hsl(var(--primary))]/60 focus-visible:outline focus-visible:outline-2 focus-visible:outline-[hsl(var(--ring))]",
        manual
          ? "border-dashed border-amber-400/60"
          : "border-[hsl(var(--border))]",
        selected && "border-[hsl(var(--primary))] ring-1 ring-[hsl(var(--ring))]",
        status === "complete" && "opacity-55",
        variant === "board" ? "px-2 py-1.5" : "px-3 py-2",
      )}
    >
      <div className="flex items-center gap-1.5">
        <StatusDot status={status} />
        <span className="text-[13px] font-semibold tabular-nums">{fmtTime(group.slot)}</span>
        {statusLabel && (
          <span
            className={clsx(
              "text-[10px] font-medium",
              status === "failed" && "text-red-400",
              status === "running" && "text-emerald-400",
              status === "overdue" && "text-amber-400",
              (status === "dispatched" || status === "confirming") && "text-sky-400",
            )}
          >
            {statusLabel}
          </span>
        )}
        <span className="flex-1" />
        {locked && <Lock className="h-3 w-3 text-[hsl(var(--muted-foreground))]" />}
        {manual && (
          <span className="rounded bg-amber-400/15 px-1 text-[9px] font-bold text-amber-400">M</span>
        )}
      </div>
      <div className="mt-1 flex items-start gap-1.5">
        <Avatar url={first.account_avatar_url} name={first.account_name} />
        <span className="line-clamp-2 text-xs leading-snug text-[hsl(var(--card-foreground))]">
          {first.anime_title}
        </span>
      </div>
      <div className="mt-1.5 flex flex-wrap items-center gap-1">
        {group.members.map((m) => (
          <span
            key={m.platform}
            className="rounded px-1 py-px text-[9px] font-bold leading-tight"
            style={{
              backgroundColor: platformTranslucentHsl(m.platform),
              color: platformBgHsl(m.platform),
            }}
          >
            {PLATFORM_SHORT[m.platform]}
            {m.status === "failed" && " ✕"}
            {m.status === "complete" && status !== "complete" && " ✓"}
          </span>
        ))}
      </div>
    </button>
  );
}
