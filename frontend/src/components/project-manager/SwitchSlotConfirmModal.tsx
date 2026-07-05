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

export function SwitchSlotConfirmModal({
  open, projectId, accountId, platform, slotIso, onClose, onChoose,
}: SwitchSlotConfirmModalProps) {
  const [preview, setPreview] = useState<SwitchPreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState<SwitchMode | null>(null);
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

  const cascadeBlocked = (preview?.cascade.blockers.length ?? 0) > 0;
  const nextFreeBlocked = (preview?.next_free.blockers.length ?? 0) > 0;
  const cascadeCount = preview?.cascade.displaced.length ?? 0;
  const ytQuotaWarning =
    platform === "youtube" && (preview?.uploaded_count ?? 0) > 10;

  const choose = async (mode: SwitchMode) => {
    if (!preview) return;
    setSubmitting(mode); setError(null);
    try {
      await onChoose(mode, preview);
      onClose();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSubmitting(null);
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
            Échanger le slot {PLATFORM_SHORT[platform]} · {fmt(slotIso)}
          </h3>
        </div>
        <p className="text-xs text-[hsl(var(--muted-foreground))] mb-3">
          Ce slot est occupé par «{preview?.occupant_title ?? "…"}». Choisissez
          comment le libérer.
        </p>

        {loading && <div className="text-xs">Calcul des déplacements…</div>}
        {error && <div className="text-xs text-[hsl(var(--destructive))] mb-2">{error}</div>}

        {preview && (
          <div className="space-y-3">
            <div className="rounded border border-[hsl(var(--border))] bg-[hsl(var(--muted))]/30 p-3">
              <div className="text-[11px] font-semibold mb-1">
                Cascade en chaîne — {cascadeCount} vidéo{cascadeCount > 1 ? "s" : ""} déplacée{cascadeCount > 1 ? "s" : ""}
              </div>
              <div className="font-mono text-[11px] leading-relaxed text-[hsl(var(--muted-foreground))] max-h-32 overflow-y-auto">
                {preview.cascade.displaced.map((d) => (
                  <div key={d.project_id}>
                    ↳ {d.anime_title} · {fmt(d.from_slot)} → {fmt(d.to_slot)}
                  </div>
                ))}
                {preview.cascade.blockers.map((b, i) => (
                  <div key={i} className="text-[hsl(var(--destructive))]">✗ {b.reason}</div>
                ))}
              </div>
              {ytQuotaWarning && (
                <div className="text-[11px] text-amber-500 mt-1">
                  ⚠ {preview.uploaded_count} vidéos YouTube déjà uploadées seront
                  re-planifiées (~{preview.uploaded_count * 50} unités de quota API).
                </div>
              )}
            </div>

            <div className="rounded border border-[hsl(var(--border))] bg-[hsl(var(--muted))]/30 p-3">
              <div className="text-[11px] font-semibold mb-1">Prochain slot libre — 1 vidéo déplacée</div>
              <div className="font-mono text-[11px] text-[hsl(var(--muted-foreground))]">
                {preview.next_free.displaced.map((d) => (
                  <div key={d.project_id}>
                    ↳ {d.anime_title} · {fmt(d.from_slot)} → {fmt(d.to_slot)}
                  </div>
                ))}
                {preview.next_free.blockers.map((b, i) => (
                  <div key={i} className="text-[hsl(var(--destructive))]">✗ {b.reason}</div>
                ))}
              </div>
            </div>
          </div>
        )}

        <div className="flex justify-end gap-2 mt-4">
          <Button size="sm" variant="ghost" onClick={onClose}>Annuler</Button>
          <Button
            size="sm" variant="outline"
            disabled={!preview || nextFreeBlocked || submitting !== null}
            onClick={() => choose("next_free")}
          >
            {submitting === "next_free" ? "…" : "Slot libre suivant (1 vidéo)"}
          </Button>
          <Button
            size="sm"
            disabled={!preview || cascadeBlocked || submitting !== null}
            onClick={() => choose("cascade")}
          >
            {submitting === "cascade" ? "…" : `Cascader (${cascadeCount} vidéo${cascadeCount > 1 ? "s" : ""})`}
          </Button>
        </div>
      </motion.div>
    </div>
  );
}
