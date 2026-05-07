import { motion } from "framer-motion";
import { ExternalLink, X, Trash2, RotateCcw, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui";
import type { PlanningEvent } from "@/types";
import { PLATFORM_LABELS, platformBgHsl } from "./platformColors";

interface EventPopoverProps {
  event: PlanningEvent;
  anchor: { x: number; y: number };
  onClose: () => void;
  onRescheduleSlot: () => void;
  onRescheduleProject: () => void;
  rescheduleProjectDisabled?: boolean;
  rescheduleProjectDisabledReason?: string;
  onCancelSlot: () => void;
  onCancelAll: () => void;
}

function formatSlot(iso: string): string {
  return new Intl.DateTimeFormat("fr-FR", {
    weekday: "short", day: "2-digit", month: "short",
    hour: "2-digit", minute: "2-digit",
    timeZone: "Europe/Paris",
  }).format(new Date(iso));
}

export function EventPopover({
  event, anchor, onClose,
  onRescheduleSlot, onRescheduleProject, rescheduleProjectDisabled,
  rescheduleProjectDisabledReason,
  onCancelSlot, onCancelAll,
}: EventPopoverProps) {
  const left = Math.min(window.innerWidth - 320, Math.max(8, anchor.x + 8));
  const top = Math.min(window.innerHeight - 280, Math.max(8, anchor.y + 8));

  return (
    <div
      className="fixed inset-0 z-[55]"
      onClick={onClose}
      role="dialog"
      aria-label="Event details"
    >
      <motion.div
        initial={{ opacity: 0, scale: 0.96 }}
        animate={{ opacity: 1, scale: 1 }}
        exit={{ opacity: 0, scale: 0.96 }}
        transition={{ duration: 0.12 }}
        className="absolute w-80 rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] shadow-xl p-4"
        style={{ left, top }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between mb-2">
          <div className="min-w-0">
            <div className="font-semibold truncate">{event.anime_title}</div>
            <div className="text-[11px] font-mono text-[hsl(var(--muted-foreground))] truncate">
              {event.project_id}
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-1 rounded hover:bg-[hsl(var(--muted))]"
            aria-label="Close"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="flex items-center gap-2 mb-3">
          <img
            src={event.account_avatar_url}
            alt=""
            className="h-6 w-6 rounded-full bg-[hsl(var(--muted))]"
          />
          <span className="text-sm">{event.account_name}</span>
          <span
            className="ml-auto text-[10px] font-bold px-2 py-0.5 rounded text-white"
            style={{ backgroundColor: platformBgHsl(event.platform) }}
          >
            {PLATFORM_LABELS[event.platform]}
          </span>
        </div>

        <div className="text-sm text-[hsl(var(--muted-foreground))] mb-3">
          {formatSlot(event.slot)}
        </div>

        {event.drive_folder_url && (
          <a
            href={event.drive_folder_url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 text-xs text-[hsl(var(--primary))] hover:underline mb-3"
          >
            Drive folder <ExternalLink className="h-3 w-3" />
          </a>
        )}

        <div className="grid grid-cols-2 gap-2">
          <Button size="sm" variant="outline" onClick={onRescheduleSlot}>
            <RefreshCw className="h-3.5 w-3.5 mr-1.5" /> Reschedule slot
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={onRescheduleProject}
            disabled={rescheduleProjectDisabled}
            title={rescheduleProjectDisabledReason}
          >
            <RotateCcw className="h-3.5 w-3.5 mr-1.5" /> Reschedule project
          </Button>
          <Button size="sm" variant="ghost" onClick={onCancelSlot} className="text-[hsl(var(--destructive))]">
            <Trash2 className="h-3.5 w-3.5 mr-1.5" /> Cancel slot
          </Button>
          <Button size="sm" variant="ghost" onClick={onCancelAll} className="text-[hsl(var(--destructive))]">
            <Trash2 className="h-3.5 w-3.5 mr-1.5" /> Cancel all
          </Button>
        </div>
      </motion.div>
    </div>
  );
}
