import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, CopyPlus, Loader2 } from "lucide-react";

import { Button } from "@/components/ui";
import type { DuplicationVariant } from "@/types";

/** Predefined duplication variants (Template drives every other parameter). */
const DUPLICATION_PRESETS: DuplicationVariant[] = [
  { language: "fr", template: "zoomed" },
  { language: "fr", template: "squared" },
  { language: "en", template: "classic" },
];

interface DuplicateProjectModalProps {
  isOpen: boolean;
  currentLanguage: string;
  currentTemplate: string | null;
  templates: Array<{ key: string; label: string }>;
  onClose: () => void;
  onConfirm: (
    variants: DuplicationVariant[],
    startAutomate: boolean,
  ) => Promise<void>;
}

function presetKey(variant: DuplicationVariant): string {
  return `${variant.language}/${variant.template}`;
}

export function DuplicateProjectModal({
  isOpen,
  currentLanguage,
  currentTemplate,
  templates,
  onClose,
  onConfirm,
}: DuplicateProjectModalProps) {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [startAutomate, setStartAutomate] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const sameAsMother = useMemo(
    () =>
      new Set(
        DUPLICATION_PRESETS.filter(
          (p) =>
            p.language === currentLanguage && p.template === currentTemplate,
        ).map(presetKey),
      ),
    [currentLanguage, currentTemplate],
  );

  // Reset on open: everything checked except variants identical to the mother.
  useEffect(() => {
    if (!isOpen) return;
    setSelected(
      new Set(
        DUPLICATION_PRESETS.filter((p) => !sameAsMother.has(presetKey(p))).map(
          presetKey,
        ),
      ),
    );
    setStartAutomate(true);
    setSubmitting(false);
    setError(null);
  }, [isOpen, sameAsMother]);

  if (!isOpen) return null;

  const templateLabel = (key: string) =>
    templates.find((t) => t.key === key)?.label ?? key;

  const toggle = (key: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const handleConfirm = async () => {
    const variants = DUPLICATION_PRESETS.filter((p) =>
      selected.has(presetKey(p)),
    );
    if (variants.length === 0) return;
    setSubmitting(true);
    setError(null);
    try {
      await onConfirm(variants, startAutomate);
      onClose();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-[60] bg-black/50 flex items-center justify-center"
      onClick={onClose}
    >
      <div
        className="bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-xl p-5 w-[440px] max-w-[92vw] shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="font-semibold flex items-center gap-2 mb-1">
          <CopyPlus className="h-4 w-4" />
          Dupliquer le projet
        </h3>
        <p className="text-sm text-[hsl(var(--muted-foreground))] mb-4">
          Crée des copies du projet (état pré-script) avec les paramètres
          prédéfinis ci-dessous. Chaque duplication s'ouvre dans un nouvel
          onglet.
        </p>

        <label className="flex items-center gap-2 text-sm mb-4">
          <input
            type="checkbox"
            checked={startAutomate}
            onChange={(e) => setStartAutomate(e.target.checked)}
            disabled={submitting}
          />
          Start automate automatically
        </label>

        <table className="w-full text-sm mb-4">
          <thead>
            <tr className="text-left text-xs text-[hsl(var(--muted-foreground))] border-b border-[hsl(var(--border))]">
              <th className="py-2 w-8"></th>
              <th className="py-2">Langue</th>
              <th className="py-2">Template</th>
            </tr>
          </thead>
          <tbody>
            {DUPLICATION_PRESETS.map((preset) => {
              const key = presetKey(preset);
              const identical = sameAsMother.has(key);
              return (
                <tr
                  key={key}
                  className="border-b border-[hsl(var(--border))]/50 cursor-pointer hover:bg-[hsl(var(--muted))]/40"
                  onClick={() => !submitting && toggle(key)}
                >
                  <td className="py-2.5">
                    <input
                      type="checkbox"
                      checked={selected.has(key)}
                      onChange={() => toggle(key)}
                      onClick={(e) => e.stopPropagation()}
                      disabled={submitting}
                    />
                  </td>
                  <td className="py-2.5 font-medium uppercase">
                    {preset.language}
                  </td>
                  <td className="py-2.5">
                    {templateLabel(preset.template)}
                    {identical && (
                      <span
                        className="ml-2 inline-flex items-center gap-1 text-[11px] text-amber-500"
                        title="Même langue et template que le projet actuel : la duplication serait identique."
                      >
                        <AlertTriangle className="h-3.5 w-3.5" />
                        identique au projet actuel
                      </span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>

        {error && (
          <div className="mb-3 text-sm text-[hsl(var(--destructive))]">
            {error}
          </div>
        )}

        <div className="flex justify-end gap-2">
          <Button variant="ghost" size="sm" onClick={onClose} disabled={submitting}>
            Cancel
          </Button>
          <Button
            size="sm"
            onClick={handleConfirm}
            disabled={submitting || selected.size === 0}
          >
            {submitting ? (
              <>
                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                Duplicating...
              </>
            ) : (
              `Confirm (${selected.size})`
            )}
          </Button>
        </div>
      </div>
    </div>
  );
}
