import { useEffect, useState } from "react";
import { motion } from "framer-motion";
import { ArrowLeftRight } from "lucide-react";
import { Button } from "@/components/ui";
import { api } from "@/api/client";
import type { Platform, SwitchMode, SwitchPreview } from "@/types";
import { PLATFORM_SHORT } from "@/components/planning/platformColors";

interface SwitchSlotConfirmModalProps {
  open: boolean;
  projectId: string;
  accountId: string;
  platform: Platform;
  slotIso: string;
  onClose: () => void;
  onChoose: (mode: SwitchMode, preview: SwitchPreview) => void | Promise<void>;
}

function fmt(iso: string): string {
  return new Intl.DateTimeFormat("fr-FR", {
    weekday: "short", day: "2-digit", month: "short",
    hour: "2-digit", minute: "2-digit", timeZone: "Europe/Paris",
  }).format(new Date(iso));
}

/**
 * Takeover confirmation: shows the occupant's NEW timings under the single
 * "relocate" strategy — the occupant is pushed to its nearest free slots,
 * TikTok-first (1 API call per platform). The old chain-cascade / next-free
 * choice was removed from the UI (2026-08); backend modes stay available.
 */
export function SwitchSlotConfirmModal({
  open, projectId, accountId, platform, slotIso, onClose, onChoose,
}: SwitchSlotConfirmModalProps) {
  const [preview, setPreview] = useState<SwitchPreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setPreview(null); setError(null); setLoading(true);
    api.switchPreview(projectId, { account_id: accountId, platform, slot: slotIso })
      .then(setPreview)
      .catch((err) => setError((err as Error).message))
      .finally(() => setLoading(false));
  }, [open, projectId, accountId, platform, slotIso]);

  if (!open) return null;

  const plan = preview?.relocate ?? null;
  const blocked = (plan?.blockers.length ?? 0) > 0;
  const relocatedUploadedCount =
    plan?.displaced.filter((d) => d.requires_platform_notification).length ?? 0;
  const ytQuotaWarning = platform === "youtube" && relocatedUploadedCount > 10;

  const choose = async () => {
    if (!preview) return;
    setSubmitting(true); setError(null);
    try {
      await onChoose("relocate", preview);
      onClose();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-[70] bg-black/55 flex items-center justify-center" onClick={onClose}>
      <motion.div
        initial={{ opacity: 0, scale: 0.95 }} animate={{ opacity: 1, scale: 1 }}
        exit={{ opacity: 0, scale: 0.95 }}
        className="bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-xl p-5 w-[480px] shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 mb-2">
          <ArrowLeftRight className="h-5 w-5 text-amber-500" />
          <h3 className="text-sm font-semibold">
            Prendre le slot {PLATFORM_SHORT[platform]} · {fmt(slotIso)}
          </h3>
        </div>
        <p className="text-xs text-[hsl(var(--muted-foreground))] mb-3">
          Ce slot est occupé par «{preview?.occupant_title ?? "…"}». Confirmer
          le déplacera vers ses prochains slots libres (TikTok d'abord, les
          autres plateformes suivent).
        </p>

        {loading && <div className="text-xs">Calcul des déplacements…</div>}
        {error && <div className="text-xs text-[hsl(var(--destructive))] mb-2">{error}</div>}

        {preview && plan && (
          <div className="rounded border border-[hsl(var(--border))] bg-[hsl(var(--muted))]/30 p-3">
            <div className="text-[11px] font-semibold mb-1">
              Nouveaux horaires de «{preview.occupant_title ?? "?"}»
            </div>
            <div className="font-mono text-[11px] leading-relaxed text-[hsl(var(--muted-foreground))] max-h-32 overflow-y-auto">
              {plan.displaced.map((d) => {
                const p = (d.platform ?? platform) as Platform;
                return (
                  <div key={`${d.project_id}:${p}`}>
                    ↳ {PLATFORM_SHORT[p]} · {fmt(d.from_slot)} → {fmt(d.to_slot)}
                  </div>
                );
              })}
              {plan.blockers.map((b, i) => (
                <div key={i} className="text-[hsl(var(--destructive))]">
                  ✗ {PLATFORM_SHORT[b.platform] ?? b.platform}: {b.reason}
                </div>
              ))}
            </div>
            {ytQuotaWarning && (
              <div className="text-[11px] text-amber-500 mt-1">
                ⚠ {relocatedUploadedCount} vidéos YouTube déjà uploadées seront
                re-planifiées (~{relocatedUploadedCount * 50} unités de quota API).
              </div>
            )}
          </div>
        )}

        <div className="flex justify-end gap-2 mt-4">
          <Button size="sm" variant="ghost" onClick={onClose}>Annuler</Button>
          <Button
            size="sm"
            disabled={!preview || blocked || submitting}
            onClick={choose}
          >
            {submitting ? "…" : "Libérer le slot"}
          </Button>
        </div>
      </motion.div>
    </div>
  );
}
