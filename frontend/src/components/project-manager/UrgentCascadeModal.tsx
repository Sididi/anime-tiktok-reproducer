import { useEffect, useState } from "react";
import { motion } from "framer-motion";
import { AlertTriangle } from "lucide-react";
import { Button } from "@/components/ui";
import { api } from "@/api/client";
import { confirmTikTokPrecedence } from "@/utils/tiktokPrecedence";
import type { CascadePreview, Platform } from "@/types";
import { PLATFORM_SHORT, platformBgHsl } from "@/components/planning/platformColors";

interface UrgentCascadeModalProps {
  open: boolean;
  projectId: string;
  projectTitle: string;
  accountId: string;
  onClose: () => void;
  onConfirmed: () => void;     // parent then continues the upload flow
}

function fmt(iso: string): string {
  return new Intl.DateTimeFormat("fr-FR", {
    weekday: "short", day: "2-digit", month: "short",
    hour: "2-digit", minute: "2-digit", timeZone: "Europe/Paris",
  }).format(new Date(iso));
}

export function UrgentCascadeModal({
  open, projectId, projectTitle, accountId, onClose, onConfirmed,
}: UrgentCascadeModalProps) {
  const [preview, setPreview] = useState<CascadePreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setLoading(true); setError(null);
    api.cascadePreview(projectId, accountId)
      .then(setPreview)
      .catch((err) => setError((err as Error).message))
      .finally(() => setLoading(false));
  }, [open, projectId, accountId]);

  if (!open) return null;
  const blocked = (preview?.blockers.length ?? 0) > 0;
  const totalDisplaced = preview?.per_platform.reduce(
    (acc, p) => acc + p.displaced.length, 0,
  ) ?? 0;

  return (
    <div className="fixed inset-0 z-[60] bg-black/55 flex items-center justify-center" onClick={onClose}>
      <motion.div
        initial={{ opacity: 0, scale: 0.95 }} animate={{ opacity: 1, scale: 1 }}
        exit={{ opacity: 0, scale: 0.95 }}
        className="bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-xl p-5 w-[480px] shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 mb-2">
          <AlertTriangle className="h-5 w-5 text-[hsl(var(--destructive))]" />
          <h3 className="text-sm font-semibold">Urgent upload — pushing others</h3>
        </div>
        <p className="text-xs text-[hsl(var(--muted-foreground))] mb-3">
          "{projectTitle}" will take the nearest slot for each platform; existing scheduled
          posts will be shifted forward.
        </p>

        {loading && <div className="text-xs">Computing cascade…</div>}
        {error && <div className="text-xs text-[hsl(var(--destructive))]">{error}</div>}

        {preview && (
          <div className="rounded border border-[hsl(var(--border))] bg-[hsl(var(--muted))]/30 p-3 font-mono text-[11px] leading-relaxed">
            {preview.per_platform.map((p) => (
              <div key={p.platform} className="mb-2 last:mb-0">
                <div>
                  <span style={{ color: platformBgHsl(p.platform) }}>
                    {PLATFORM_SHORT[p.platform]}
                  </span>{" "}
                  · {fmt(p.target_slot)} ← <b>this video</b>
                </div>
                {p.displaced.map((d) => (
                  <div key={d.project_id} className="text-[hsl(var(--muted-foreground))] pl-4">
                    ↳ {d.anime_title} · {fmt(d.from_slot)} → {fmt(d.to_slot)}
                  </div>
                ))}
                {(p.precedence_warnings ?? []).map((w) => (
                  <div key={`warn-${w.project_id}`} className="text-amber-500 pl-4">
                    ⚠ {w.anime_title} : {w.platforms.join(", ")} publierait avant
                    son TikTok repoussé
                  </div>
                ))}
              </div>
            ))}
            {preview.blockers.map((b, i) => (
              <div key={i} className="text-[hsl(var(--destructive))]">
                ✗ {PLATFORM_SHORT[b.platform as Platform]}: {b.reason}
              </div>
            ))}
          </div>
        )}

        {preview && !blocked && (
          <div className="text-[11px] text-[hsl(var(--muted-foreground))] mt-2">
            {totalDisplaced} project{totalDisplaced === 1 ? "" : "s"} will be shifted.
          </div>
        )}

        <div className="flex justify-end gap-2 mt-4">
          <Button size="sm" variant="ghost" onClick={onClose}>Cancel</Button>
          <Button
            size="sm"
            variant="destructive"
            disabled={!preview || blocked || submitting}
            onClick={async () => {
              setSubmitting(true);
              try {
                try {
                  await api.cascadeApply(projectId, accountId);
                } catch (err) {
                  const confirmed = confirmTikTokPrecedence(err);
                  if (confirmed === null) throw err;
                  if (!confirmed) return;
                  await api.cascadeApply(projectId, accountId, true);
                }
                onClose();
                onConfirmed();
              } catch (err) {
                setError((err as Error).message);
              } finally {
                setSubmitting(false);
              }
            }}
          >
            {submitting ? "Applying…" : "Confirm urgent upload"}
          </Button>
        </div>
      </motion.div>
    </div>
  );
}
