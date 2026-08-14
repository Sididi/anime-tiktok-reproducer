import { useCallback, useRef, useState, type ReactNode } from "react";
import { AlertTriangle } from "lucide-react";

interface ConfirmOptions {
  title: string;
  body: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  destructive?: boolean;
}

interface ActiveConfirm extends ConfirmOptions {
  resolve: (confirmed: boolean) => void;
}

/**
 * Promise-based confirmation dialog (replaces window.confirm in planning).
 * Usage: const { confirm, dialog } = useConfirmDialog();
 *        if (await confirm({ title, body })) { ... }
 * Render {dialog} once in the page.
 */
export function useConfirmDialog() {
  const [active, setActive] = useState<ActiveConfirm | null>(null);
  const activeRef = useRef<ActiveConfirm | null>(null);

  const confirm = useCallback((options: ConfirmOptions): Promise<boolean> => {
    // A newer request supersedes any pending one (declined).
    activeRef.current?.resolve(false);
    return new Promise<boolean>((resolve) => {
      const entry = { ...options, resolve };
      activeRef.current = entry;
      setActive(entry);
    });
  }, []);

  const settle = (confirmed: boolean) => {
    activeRef.current?.resolve(confirmed);
    activeRef.current = null;
    setActive(null);
  };

  const dialog = active ? (
    <div
      className="fixed inset-0 z-[70] flex items-center justify-center bg-black/60 p-6"
      onClick={() => settle(false)}
      role="dialog"
      aria-modal="true"
      aria-label={active.title}
    >
      <div
        className="w-full max-w-md rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-5"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start gap-3">
          <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-amber-400" />
          <div className="min-w-0">
            <h2 className="text-sm font-semibold">{active.title}</h2>
            <div className="mt-2 text-xs leading-relaxed text-[hsl(var(--muted-foreground))]">
              {active.body}
            </div>
          </div>
        </div>
        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            autoFocus
            onClick={() => settle(false)}
            className="rounded bg-[hsl(var(--secondary))] px-3 py-1.5 text-xs font-medium hover:bg-[hsl(var(--secondary))]/80"
          >
            {active.cancelLabel ?? "Annuler"}
          </button>
          <button
            type="button"
            onClick={() => settle(true)}
            className={
              active.destructive
                ? "rounded bg-[hsl(var(--destructive))] px-3 py-1.5 text-xs font-semibold text-[hsl(var(--destructive-foreground))] hover:bg-[hsl(var(--destructive))]/90"
                : "rounded bg-[hsl(var(--primary))] px-3 py-1.5 text-xs font-semibold text-[hsl(var(--primary-foreground))] hover:bg-[hsl(var(--primary))]/90"
            }
          >
            {active.confirmLabel ?? "Confirmer"}
          </button>
        </div>
      </div>
    </div>
  ) : null;

  return { confirm, dialog };
}
