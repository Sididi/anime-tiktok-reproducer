import { useState } from "react";
import { AlertTriangle } from "lucide-react";
import { getLibraryTypeLabel } from "@/utils/libraryTypes";
import type { LibraryType } from "@/types";

interface PurgeModalProps {
  open: boolean;
  onClose: () => void;
  onConfirm: (allTypes: boolean) => void;
  currentLibraryType: LibraryType;
  estimatedBytes: number;
  sourceCount: number;
}

function formatGigabytes(bytes: number): string {
  const gb = bytes / (1024 * 1024 * 1024);
  return `${gb.toFixed(1)} GB`;
}

export function PurgeModal({
  open,
  onClose,
  onConfirm,
  currentLibraryType,
  estimatedBytes,
  sourceCount,
}: PurgeModalProps) {
  const [allTypes, setAllTypes] = useState(false);

  if (!open) return null;

  const handleConfirm = () => {
    onConfirm(allTypes);
  };

  const handleClose = () => {
    setAllTypes(false);
    onClose();
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
      <div className="bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-xl max-w-md w-full p-6 flex flex-col gap-4">
        {/* Warning icon */}
        <div className="flex justify-center">
          <AlertTriangle className="h-8 w-8 text-[hsl(var(--destructive))]" />
        </div>

        {/* Title */}
        <h2 className="font-semibold text-lg text-center">
          Purger la librairie {getLibraryTypeLabel(currentLibraryType)}
        </h2>

        {/* Explanation */}
        <p className="text-sm text-[hsl(var(--muted-foreground))] text-center leading-relaxed">
          Les fichiers vidéo source et les index locaux seront supprimés. Les
          données restent disponibles sur le Storage Box pour un
          retéléchargement ultérieur.
        </p>

        {/* Space estimate */}
        <div className="bg-[hsl(var(--secondary))] rounded-lg px-4 py-3 text-sm text-center">
          <span className="text-[hsl(var(--foreground))]">
            Espace estimé libéré:{" "}
          </span>
          <span className="font-semibold text-[hsl(var(--destructive))]">
            {formatGigabytes(estimatedBytes)}
          </span>
          <span className="text-[hsl(var(--muted-foreground))]">
            {" "}
            ({sourceCount} source{sourceCount !== 1 ? "s" : ""})
          </span>
        </div>

        {/* Checkbox */}
        <label className="flex items-center gap-2 cursor-pointer text-sm">
          <input
            type="checkbox"
            checked={allTypes}
            onChange={(e) => setAllTypes(e.target.checked)}
            className="rounded"
          />
          <span>Purger également les autres types de librairie</span>
        </label>

        {/* Actions */}
        <div className="flex justify-end gap-3 pt-2">
          <button
            onClick={handleClose}
            className="text-sm text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors px-3 py-2"
          >
            Annuler
          </button>
          <button
            onClick={handleConfirm}
            className="bg-[hsl(var(--destructive))] text-white rounded-md px-4 py-2 text-sm font-medium hover:bg-[hsl(var(--destructive))]/90 transition-colors"
          >
            Confirmer la purge
          </button>
        </div>
      </div>
    </div>
  );
}
