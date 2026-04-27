import { useEffect, useState } from "react";
import { Loader2, Pencil } from "lucide-react";
import { Button, Input } from "@/components/ui";
import type { SourceDetails } from "@/types";

interface RenameSourceModalProps {
  open: boolean;
  source: SourceDetails | null;
  loading: boolean;
  error: string | null;
  onClose: () => void;
  onSubmit: (newName: string) => void;
}

export function RenameSourceModal({
  open,
  source,
  loading,
  error,
  onClose,
  onSubmit,
}: RenameSourceModalProps) {
  const [name, setName] = useState("");

  useEffect(() => {
    if (open && source) {
      setName(source.name);
    }
  }, [open, source]);

  if (!open || !source) {
    return null;
  }

  const trimmedName = name.trim();
  const unchanged = trimmedName === source.name.trim();
  const submitDisabled = loading || !trimmedName || unchanged;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={() => {
        if (!loading) {
          onClose();
        }
      }}
    >
      <div
        className="w-full max-w-lg rounded-xl border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-6 shadow-2xl"
        onClick={(event) => event.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="rename-source-title"
      >
        <div className="mb-4 flex items-center gap-3">
          <div className="rounded-full bg-indigo-500/10 p-2 text-indigo-400">
            <Pencil className="h-5 w-5" />
          </div>
          <div>
            <h2 id="rename-source-title" className="text-lg font-semibold">
              Renommer « {source.name} »
            </h2>
            <p className="text-sm text-[hsl(var(--muted-foreground))]">
              Le series_id reste identique. Le nouveau nom sera propagé à la
              release Storage Box, au cache local et aux références persistées.
            </p>
          </div>
        </div>

        <div className="space-y-2">
          <label
            htmlFor="rename-source-input"
            className="text-sm font-medium text-[hsl(var(--foreground))]"
          >
            Nouveau nom
          </label>
          <Input
            id="rename-source-input"
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="Nom de la série"
            disabled={loading}
            autoFocus
          />
          {unchanged && (
            <p className="text-xs text-[hsl(var(--muted-foreground))]">
              Entrez un nom différent pour lancer le renommage.
            </p>
          )}
        </div>

        {error && (
          <div className="mt-4 rounded-lg bg-red-500/10 px-4 py-3 text-sm text-red-200">
            {error}
          </div>
        )}

        <div className="mt-6 flex justify-end gap-3">
          <Button variant="ghost" onClick={onClose} disabled={loading}>
            Annuler
          </Button>
          <Button
            onClick={() => onSubmit(trimmedName)}
            disabled={submitDisabled}
          >
            {loading ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                Renommage...
              </>
            ) : (
              <>
                <Pencil className="mr-2 h-4 w-4" />
                Renommer
              </>
            )}
          </Button>
        </div>
      </div>
    </div>
  );
}
