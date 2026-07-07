import { useState } from "react";
import { motion } from "framer-motion";
import { ExternalLink, X, Trash2, RotateCcw, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui";
import type { Platform, PlanningEvent } from "@/types";
import { ALL_PLATFORMS } from "@/types";
import { PLATFORM_LABELS, platformBgHsl } from "./platformColors";

interface EventPopoverProps {
  /**
   * All members of the grouped event (same project + same slot).
   * Always at least one element.
   */
  members: PlanningEvent[];
  anchor: { x: number; y: number };
  onClose: () => void;
  onReschedulePlatform: (platform: Platform) => void;
  onCancelPlatform: (platform: Platform) => void;
  onRescheduleProject: () => void;
  rescheduleProjectDisabled?: boolean;
  rescheduleProjectDisabledReason?: string;
  onCancelAll: () => void;
}

function formatSlot(iso: string): string {
  return new Intl.DateTimeFormat("fr-FR", {
    weekday: "short",
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "Europe/Paris",
  }).format(new Date(iso));
}

const STATUS_UI: Record<
  PlanningEvent["status"],
  { label: string; className: string } | null
> = {
  scheduled: null,
  running: {
    label: "En cours",
    className: "text-green-500 border-green-500/40",
  },
  complete: {
    label: "Publié",
    className: "text-green-500/80 border-green-500/30",
  },
};

type ConfirmTarget = { kind: "platform"; platform: Platform } | { kind: "all" };

export function EventPopover({
  members,
  anchor,
  onClose,
  onReschedulePlatform,
  onCancelPlatform,
  onRescheduleProject,
  rescheduleProjectDisabled,
  rescheduleProjectDisabledReason,
  onCancelAll,
}: EventPopoverProps) {
  const first = members[0];
  const projectTimingLocked = first.timing_locked;
  const ordered = [...members].sort(
    (a, b) =>
      ALL_PLATFORMS.indexOf(a.platform) - ALL_PLATFORMS.indexOf(b.platform),
  );
  // Two-step destructive actions: first click arms ("Confirmer ?"),
  // second click executes. Any other interaction disarms.
  const [confirming, setConfirming] = useState<ConfirmTarget | null>(null);

  const POPOVER_W = 360;
  // Approx height: 96 (header+slot+drive) + 38 per platform row + 84 (global actions).
  const POPOVER_H = 96 + members.length * 38 + 84;

  // Place to the right of the click; flip to the left if it would overflow.
  let left = anchor.x + 12;
  if (left + POPOVER_W > window.innerWidth - 8) {
    left = anchor.x - POPOVER_W - 12;
  }
  left = Math.max(8, Math.min(window.innerWidth - POPOVER_W - 8, left));

  // Prefer below the click; flip above if it would clip the viewport bottom.
  let top = anchor.y + 12;
  if (top + POPOVER_H > window.innerHeight - 8) {
    top = anchor.y - POPOVER_H - 12;
  }
  top = Math.max(8, Math.min(window.innerHeight - POPOVER_H - 8, top));

  const isConfirmingPlatform = (p: Platform) =>
    confirming?.kind === "platform" && confirming.platform === p;
  const isConfirmingAll = confirming?.kind === "all";

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
        className="absolute rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] shadow-xl p-4"
        style={{ left, top, width: POPOVER_W }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between mb-2 gap-2">
          <div className="min-w-0">
            <div className="font-semibold truncate">{first.anime_title}</div>
            <div className="text-[11px] font-mono text-[hsl(var(--muted-foreground))] truncate">
              {first.project_id}
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-1 rounded hover:bg-[hsl(var(--muted))] flex-shrink-0"
            aria-label="Fermer"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="flex items-center gap-2 mb-2">
          <img
            src={first.account_avatar_url}
            alt=""
            className="h-6 w-6 rounded-full bg-[hsl(var(--muted))] flex-shrink-0"
          />
          <span className="text-sm truncate">{first.account_name}</span>
        </div>

        <div className="text-sm text-[hsl(var(--muted-foreground))] mb-2">
          {formatSlot(first.slot)}
        </div>

        {first.manual && (
          <div className="text-[11px] text-amber-500 mb-2">
            Programmation manuelle — hors système de slots
          </div>
        )}

        {first.drive_folder_url && (
          <a
            href={first.drive_folder_url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 text-xs text-[hsl(var(--primary))] hover:underline mb-3"
          >
            Dossier Drive <ExternalLink className="h-3 w-3" />
          </a>
        )}

        {/* Per-platform actions */}
        <div className="space-y-1.5 mb-3 border-t border-[hsl(var(--border))] pt-2">
          {ordered.map((m) => {
            const statusUi = STATUS_UI[m.status];
            const statusLocked = m.status !== "scheduled";
            const timingLocked = m.timing_locked;
            // Timing lock forbids MOVING a slot, not cancelling it.
            const rescheduleLocked = statusLocked || timingLocked;
            const cancelLocked = statusLocked;
            const rescheduleLockedReason = statusLocked
              ? (m.status === "running"
                  ? "Upload en cours — action impossible"
                  : "Déjà publié — action impossible")
              : "La publication TikTok a commencé — horaire verrouillé";
            const cancelLockedReason =
              m.status === "running"
                ? "Upload en cours — action impossible"
                : "Déjà publié — action impossible";
            return (
              <div key={m.platform} className="flex items-center gap-2">
                <span
                  className="text-[10px] font-bold px-1.5 py-0.5 rounded text-white flex-shrink-0"
                  style={{
                    backgroundColor: platformBgHsl(m.platform),
                    minWidth: 30,
                    textAlign: "center",
                  }}
                >
                  {PLATFORM_LABELS[m.platform]}
                </span>
                {statusUi && (
                  <span
                    className={`text-[10px] px-1.5 py-0.5 rounded border ${statusUi.className} ${
                      m.status === "running" ? "planning-status-pulse" : ""
                    }`}
                  >
                    {statusUi.label}
                  </span>
                )}
                <div className="flex-1" />
                <Button
                  size="sm"
                  variant="ghost"
                  className="h-7 px-2 text-xs"
                  disabled={rescheduleLocked}
                  onClick={() => {
                    setConfirming(null);
                    onReschedulePlatform(m.platform);
                  }}
                  title={
                    rescheduleLocked
                      ? rescheduleLockedReason
                      : `Déplacer le créneau ${PLATFORM_LABELS[m.platform]}`
                  }
                >
                  <RefreshCw className="h-3 w-3 mr-1" /> Déplacer
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  className={`h-7 px-2 text-xs text-[hsl(var(--destructive))] hover:text-[hsl(var(--destructive))] ${
                    isConfirmingPlatform(m.platform)
                      ? "bg-[hsl(var(--destructive))]/15"
                      : ""
                  }`}
                  disabled={cancelLocked}
                  onClick={() => {
                    if (isConfirmingPlatform(m.platform)) {
                      setConfirming(null);
                      onCancelPlatform(m.platform);
                    } else {
                      setConfirming({ kind: "platform", platform: m.platform });
                    }
                  }}
                  title={
                    cancelLocked
                      ? cancelLockedReason
                      : `Annuler le créneau ${PLATFORM_LABELS[m.platform]}`
                  }
                >
                  <Trash2 className="h-3 w-3 mr-1" />
                  {isConfirmingPlatform(m.platform) ? "Confirmer ?" : "Annuler"}
                </Button>
              </div>
            );
          })}
        </div>

        {/* Global actions — affect the whole project (all platforms, even on
            other calendar cards). */}
        <div className="grid grid-cols-2 gap-2 border-t border-[hsl(var(--border))] pt-2">
          <Button
            size="sm"
            variant="outline"
            className="h-8 px-2 text-xs whitespace-nowrap"
            onClick={() => {
              setConfirming(null);
              onRescheduleProject();
            }}
            disabled={rescheduleProjectDisabled || projectTimingLocked}
            title={
              projectTimingLocked
                ? "La publication TikTok a commencé — horaire verrouillé"
                : rescheduleProjectDisabledReason ??
                  "Replanifier le projet entier (toutes plateformes)"
            }
          >
            <RotateCcw className="h-3.5 w-3.5 mr-1" /> Replanifier projet
          </Button>
          <Button
            size="sm"
            variant="ghost"
            className={`h-8 px-2 text-xs text-[hsl(var(--destructive))] whitespace-nowrap ${
              isConfirmingAll ? "bg-[hsl(var(--destructive))]/15" : ""
            }`}
            onClick={() => {
              if (isConfirmingAll) {
                setConfirming(null);
                onCancelAll();
              } else {
                setConfirming({ kind: "all" });
              }
            }}
            title="Annuler toutes les plateformes du projet"
          >
            <Trash2 className="h-3.5 w-3.5 mr-1" />
            {isConfirmingAll ? "Confirmer ?" : "Tout annuler"}
          </Button>
        </div>
      </motion.div>
    </div>
  );
}
