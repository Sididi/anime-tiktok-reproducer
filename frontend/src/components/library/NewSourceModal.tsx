import { useState } from "react";
import { FolderOpen, ChevronDown } from "lucide-react";
import { Input } from "@/components/ui";
import type { LibraryType } from "@/types";
import { LIBRARY_TYPE_OPTIONS } from "@/utils/libraryTypes";
import { FolderBrowserModal } from "@/components/FolderBrowserModal";

interface NewSourceModalProps {
  open: boolean;
  onClose: () => void;
  onSubmit: (
    path: string,
    name: string | undefined,
    type: LibraryType,
    fps: number
  ) => void;
  currentLibraryType: LibraryType;
}

const FPS_OPTIONS = [1, 2, 4];

export function NewSourceModal({
  open,
  onClose,
  onSubmit,
  currentLibraryType,
}: NewSourceModalProps) {
  const [path, setPath] = useState("");
  const [name, setName] = useState("");
  const [selectedType, setSelectedType] = useState<LibraryType>(currentLibraryType);
  const [fps, setFps] = useState(2);
  const [typeExpanded, setTypeExpanded] = useState(false);
  const [folderBrowserOpen, setFolderBrowserOpen] = useState(false);

  if (!open) return null;

  const handleSubmit = () => {
    if (!path) return;
    onSubmit(path, name.trim() || undefined, selectedType, fps);
  };

  const handleClose = () => {
    setPath("");
    setName("");
    setSelectedType(currentLibraryType);
    setFps(2);
    setTypeExpanded(false);
    onClose();
  };

  const currentTypeLabel =
    LIBRARY_TYPE_OPTIONS.find((o) => o.value === selectedType)?.label ??
    selectedType;

  return (
    <>
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
        <div className="bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-xl max-w-lg w-full p-6 flex flex-col gap-4">
          <h2 className="font-semibold text-lg">Nouvelle source</h2>

          {/* Source path */}
          <div className="flex flex-col gap-1.5">
            <label className="text-sm text-[hsl(var(--muted-foreground))]">
              Chemin de la source
            </label>
            <div className="flex gap-2">
              <Input
                value={path}
                onChange={(e) => setPath(e.target.value)}
                placeholder="/media/anime/..."
                className="flex-1"
              />
              <button
                onClick={() => setFolderBrowserOpen(true)}
                className="flex items-center justify-center px-3 bg-[hsl(var(--secondary))] hover:bg-[hsl(var(--secondary))]/80 rounded-md transition-colors shrink-0"
                title="Parcourir les dossiers"
              >
                <FolderOpen className="h-4 w-4" />
              </button>
            </div>
          </div>

          {/* Source name */}
          <div className="flex flex-col gap-1.5">
            <label className="text-sm text-[hsl(var(--muted-foreground))]">
              Nom de la source
            </label>
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Nom (défaut: nom du dossier)"
            />
          </div>

          {/* Type selector (collapsible) */}
          <div className="flex flex-col gap-1.5">
            <button
              onClick={() => setTypeExpanded((v) => !v)}
              className="flex items-center justify-between px-3 py-2 bg-[hsl(var(--secondary))] rounded-md text-sm hover:bg-[hsl(var(--secondary))]/80 transition-colors"
            >
              <span className="text-[hsl(var(--muted-foreground))]">Type</span>
              <div className="flex items-center gap-2">
                <span>{currentTypeLabel}</span>
                <ChevronDown
                  className={`h-4 w-4 transition-transform ${typeExpanded ? "rotate-180" : ""}`}
                />
              </div>
            </button>
            {typeExpanded && (
              <select
                value={selectedType}
                onChange={(e) => setSelectedType(e.target.value as LibraryType)}
                className="bg-[hsl(var(--secondary))] text-[hsl(var(--foreground))] rounded-md px-3 py-2 text-sm border-none outline-none cursor-pointer"
                size={LIBRARY_TYPE_OPTIONS.length}
              >
                {LIBRARY_TYPE_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </select>
            )}
          </div>

          {/* FPS selector */}
          <div className="flex flex-col gap-1.5">
            <label className="text-sm text-[hsl(var(--muted-foreground))]">
              FPS d'indexation
            </label>
            <div className="flex gap-2">
              {FPS_OPTIONS.map((f) => (
                <button
                  key={f}
                  onClick={() => setFps(f)}
                  className={`flex-1 py-1.5 rounded-full text-sm font-medium transition-colors ${
                    fps === f
                      ? "bg-[hsl(var(--primary))] text-white"
                      : "bg-[hsl(var(--secondary))] text-[hsl(var(--foreground))] hover:bg-[hsl(var(--secondary))]/80"
                  }`}
                >
                  {f} fps
                </button>
              ))}
            </div>
          </div>

          {/* Actions */}
          <div className="flex justify-end gap-3 pt-2">
            <button
              onClick={handleClose}
              className="text-sm text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors px-3 py-2"
            >
              Annuler
            </button>
            <button
              onClick={handleSubmit}
              disabled={!path}
              className={`bg-[hsl(var(--primary))] text-white rounded-md px-4 py-2 text-sm font-medium transition-colors ${
                !path
                  ? "opacity-50 cursor-not-allowed"
                  : "hover:bg-[hsl(var(--primary))]/90"
              }`}
            >
              Indexer
            </button>
          </div>
        </div>
      </div>

      <FolderBrowserModal
        open={folderBrowserOpen}
        onClose={() => setFolderBrowserOpen(false)}
        onSelect={(selectedPath) => {
          setPath(selectedPath);
          setFolderBrowserOpen(false);
        }}
        initialPath={path || undefined}
      />
    </>
  );
}
