import { useEffect, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { api } from "@/api/client";
import type { Platform, FreeSlot } from "@/types";
import { PLATFORM_LABELS } from "@/components/planning/platformColors";

interface PerPlatformOverrideProps {
  accountId: string;
  anchorIso: string;
  resolved: Partial<Record<Platform, { slot: string; available: boolean }>>;
  overrides: Partial<Record<Platform, string>>;
  onChangeOverride: (platform: Platform, slotIso: string | null) => void;
  platforms: Platform[]; // platforms the project will reserve
}

const NON_TT_PLATFORMS: Platform[] = ["youtube", "facebook", "instagram"];

export function PerPlatformOverride({
  accountId, anchorIso, resolved, overrides, onChangeOverride, platforms,
}: PerPlatformOverrideProps) {
  const [open, setOpen] = useState(false);
  const [slotsByPlatform, setSlotsByPlatform] = useState<Record<Platform, FreeSlot[]>>(
    {} as Record<Platform, FreeSlot[]>,
  );

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    (async () => {
      const next: Record<Platform, FreeSlot[]> = {} as Record<Platform, FreeSlot[]>;
      for (const platform of NON_TT_PLATFORMS) {
        if (!platforms.includes(platform)) continue;
        const r = await api.listFreeSlots({
          account_id: accountId,
          platform,
          after: anchorIso,
          limit: 20,
        });
        next[platform] = r.slots;
      }
      if (!cancelled) setSlotsByPlatform(next);
    })();
    return () => { cancelled = true; };
  }, [open, accountId, anchorIso, platforms]);

  return (
    <div className="border-t border-[hsl(var(--border))] pt-2">
      <button
        type="button"
        onClick={() => setOpen((p) => !p)}
        className="text-xs flex items-center gap-1 text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]"
      >
        {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
        Override per-platform
      </button>
      {open && (
        <div className="mt-2 space-y-1.5">
          {NON_TT_PLATFORMS.filter((p) => platforms.includes(p)).map((p) => {
            const slots = slotsByPlatform[p] ?? [];
            const current = overrides[p] ?? resolved[p]?.slot ?? "";
            return (
              <div key={p} className="flex items-center gap-2 text-xs">
                <span className="w-20">{PLATFORM_LABELS[p]}</span>
                <select
                  className="flex-1 rounded border border-[hsl(var(--border))] bg-[hsl(var(--card))] px-2 py-1 text-xs"
                  value={current}
                  onChange={(e) => onChangeOverride(p, e.target.value || null)}
                >
                  {slots.filter((s) => s.available || s.slot === current).map((s) => (
                    <option key={s.slot} value={s.slot}>
                      {new Intl.DateTimeFormat("fr-FR", {
                        weekday: "short", day: "2-digit", month: "short",
                        hour: "2-digit", minute: "2-digit", timeZone: "Europe/Paris",
                      }).format(new Date(s.slot))}
                    </option>
                  ))}
                </select>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
