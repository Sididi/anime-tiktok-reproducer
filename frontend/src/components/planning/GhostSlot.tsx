import clsx from "clsx";
import type { Platform } from "@/types";
import { fmtTime } from "@/utils/parisTime";
import { platformBgHsl, PLATFORM_SHORT } from "./platformColors";

export interface GhostSlotItem {
  platform: Platform;
  slot: string;
}

interface GhostSlotProps {
  platform: Platform;
  slot: string;
  onClick?: (platform: Platform, slot: string) => void;
}

/**
 * A free configured slot. Only TikTok ghosts are actionable: scheduling is
 * anchored on the TikTok slot, the other platforms follow it.
 */
export function GhostSlot({ platform, slot, onClick }: GhostSlotProps) {
  const actionable = platform === "tiktok";
  return (
    <button
      type="button"
      data-testid="planning-ghost-slot"
      disabled={!actionable}
      onClick={() => actionable && onClick?.(platform, slot)}
      title={
        actionable
          ? "Planifier un projet sur ce créneau"
          : "La planification s'ancre sur le créneau TikTok"
      }
      className={clsx(
        "flex w-full items-center gap-1.5 rounded-md border border-dashed px-2 py-1 text-[11px] tabular-nums transition-colors",
        actionable
          ? "cursor-pointer hover:bg-[hsl(var(--platform-tiktok))]/10"
          : "cursor-default opacity-35",
      )}
      style={{
        borderColor: `hsl(${platform === "tiktok" ? "var(--platform-tiktok)" : "var(--border)"} / 0.5)`,
        color: actionable ? platformBgHsl(platform) : "hsl(var(--muted-foreground))",
      }}
    >
      <span className="text-sm leading-none">+</span>
      <span className="font-medium">{fmtTime(slot)}</span>
      <span className="ml-auto text-[9px] font-bold opacity-80">{PLATFORM_SHORT[platform]}</span>
    </button>
  );
}
