import { useState, useEffect, useCallback } from "react";
import {
  Folder,
  FolderOpen,
  ArrowUp,
  Film,
  Loader2,
  X,
  Check,
} from "lucide-react";
import { Button } from "@/components/ui";
import { api } from "@/api/client";
import type { BrowseEntry } from "@/types";

interface FolderBrowserModalProps {
  open: boolean;
  onClose: () => void;
  onSelect: (path: string) => void;
  initialPath?: string;
  multiSelect?: boolean;
  onSelectMultiple?: (paths: string[]) => void;
}

export function FolderBrowserModal({
  open,
  onClose,
  onSelect,
  initialPath,
  multiSelect = false,
  onSelectMultiple,
}: FolderBrowserModalProps) {
  const [currentPath, setCurrentPath] = useState<string>("");
  const [parentPath, setParentPath] = useState<string | null>(null);
  const [entries, setEntries] = useState<BrowseEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedPaths, setSelectedPaths] = useState<Set<string>>(new Set());

  const browse = useCallback(async (path?: string) => {
    setLoading(true);
    setError(null);
    try {
      const result = await api.browseDirectories(path);
      setCurrentPath(result.current_path);
      setParentPath(result.parent_path);
      setEntries(result.entries);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (open) {
      setSelectedPaths(new Set());
      if (initialPath) {
        // Try initialPath first; if it fails, try parent directories
        const tryPath = async () => {
          setLoading(true);
          setError(null);
          const segments = initialPath.split("/").filter(Boolean);
          // Try the full path, then progressively shorter paths
          for (let i = segments.length; i >= 0; i--) {
            const candidate =
              i === 0 ? undefined : "/" + segments.slice(0, i).join("/");
            try {
              const result = await api.browseDirectories(candidate);
              setCurrentPath(result.current_path);
              setParentPath(result.parent_path);
              setEntries(result.entries);
              setLoading(false);
              return;
            } catch {
              // continue to shorter path
            }
          }
          setLoading(false);
          setError("Impossible d'ouvrir le chemin initial");
        };
        tryPath();
      } else {
        browse(undefined);
      }
    }
  }, [open, initialPath, browse]);

  if (!open) return null;

  const dirs = entries.filter((e) => e.is_dir);
  const files = entries.filter((e) => !e.is_dir);

  const toggleSelection = (path: string) => {
    setSelectedPaths((prev) => {
      const next = new Set(prev);
      if (next.has(path)) {
        next.delete(path);
      } else {
        next.add(path);
      }
      return next;
    });
  };

  const allVisibleSelected =
    dirs.length > 0 && dirs.every((d) => selectedPaths.has(d.path));

  const toggleSelectAll = () => {
    setSelectedPaths((prev) => {
      const next = new Set(prev);
      if (allVisibleSelected) {
        for (const d of dirs) next.delete(d.path);
      } else {
        for (const d of dirs) next.add(d.path);
      }
      return next;
    });
  };

  const handleConfirm = () => {
    if (multiSelect && onSelectMultiple) {
      onSelectMultiple(Array.from(selectedPaths));
    } else {
      onSelect(currentPath);
    }
    onClose();
  };

  const selectionCount = selectedPaths.size;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
      <div className="bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-xl shadow-xl w-full max-w-2xl max-h-[80vh] flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-[hsl(var(--border))]">
          <div className="flex items-center gap-3">
            <h2 className="font-semibold">
              {multiSelect
                ? "Selectionner des dossiers"
                : "Selectionner un dossier"}
            </h2>
            {multiSelect && selectionCount > 0 && (
              <span className="text-xs bg-[hsl(var(--primary))] text-white rounded-full px-2 py-0.5 font-medium">
                {selectionCount}
              </span>
            )}
          </div>
          <button
            onClick={onClose}
            className="p-1 rounded hover:bg-[hsl(var(--muted))] transition-colors"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* Current path */}
        <div className="px-4 py-2 bg-[hsl(var(--muted))] text-xs font-mono text-[hsl(var(--muted-foreground))] truncate">
          {currentPath}
        </div>

        {error && (
          <div className="px-4 py-2 text-sm text-[hsl(var(--destructive))]">
            {error}
          </div>
        )}

        {/* Navigation */}
        <div className="flex-1 overflow-y-auto min-h-0">
          {loading ? (
            <div className="flex items-center justify-center py-12">
              <Loader2 className="h-6 w-6 animate-spin text-[hsl(var(--muted-foreground))]" />
            </div>
          ) : (
            <div className="py-2 px-2 flex flex-col gap-0.5">
              {/* Select All (multi-select only) */}
              {multiSelect && dirs.length > 0 && (
                <button
                  onClick={toggleSelectAll}
                  className="flex items-center gap-2 w-full px-4 py-2 text-sm hover:bg-[hsl(var(--secondary))]/50 rounded-lg transition-colors text-[hsl(var(--muted-foreground))] border-b border-[hsl(var(--border))] mb-1"
                >
                  <div
                    className={`h-4 w-4 rounded border flex items-center justify-center shrink-0 transition-colors ${
                      allVisibleSelected
                        ? "bg-[hsl(var(--primary))] border-[hsl(var(--primary))]"
                        : "border-[hsl(var(--border))]"
                    }`}
                  >
                    {allVisibleSelected && (
                      <Check className="h-3 w-3 text-white" />
                    )}
                  </div>
                  <span className="text-xs font-medium">Tout selectionner</span>
                </button>
              )}

              {parentPath && (
                <button
                  onClick={() => browse(parentPath)}
                  className="flex items-center gap-2 w-full px-4 py-2 text-sm hover:bg-[hsl(var(--secondary))]/50 rounded-lg transition-colors text-[hsl(var(--muted-foreground))]"
                >
                  {multiSelect && <div className="h-4 w-4 shrink-0" />}
                  <ArrowUp className="h-4 w-4" />
                  <span>..</span>
                </button>
              )}
              {dirs.length === 0 && files.length === 0 && (
                <div className="px-4 py-8 text-center text-sm text-[hsl(var(--muted-foreground))]">
                  Dossier vide
                </div>
              )}
              {dirs.map((entry) => (
                <div
                  key={entry.path}
                  className="flex items-center gap-2 w-full px-4 py-2 text-sm hover:bg-[hsl(var(--secondary))]/50 rounded-lg transition-colors"
                >
                  {multiSelect && (
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        toggleSelection(entry.path);
                      }}
                      className="shrink-0"
                    >
                      <div
                        className={`h-4 w-4 rounded border flex items-center justify-center transition-colors ${
                          selectedPaths.has(entry.path)
                            ? "bg-[hsl(var(--primary))] border-[hsl(var(--primary))]"
                            : "border-[hsl(var(--border))] hover:border-[hsl(var(--primary))]/50"
                        }`}
                      >
                        {selectedPaths.has(entry.path) && (
                          <Check className="h-3 w-3 text-white" />
                        )}
                      </div>
                    </button>
                  )}
                  <button
                    onClick={() => browse(entry.path)}
                    className="flex items-center gap-2 flex-1 min-w-0"
                  >
                    {entry.has_videos ? (
                      <FolderOpen className="h-4 w-4 text-amber-500 shrink-0" />
                    ) : (
                      <Folder className="h-4 w-4 text-[hsl(var(--muted-foreground))] shrink-0" />
                    )}
                    <span className="truncate">{entry.name}</span>
                    {entry.has_videos && (
                      <Film className="h-3 w-3 text-amber-500 shrink-0 ml-auto" />
                    )}
                  </button>
                </div>
              ))}
              {files.length > 0 && (
                <>
                  {dirs.length > 0 && (
                    <div className="border-t border-[hsl(var(--border))] my-1" />
                  )}
                  {files.map((entry) => (
                    <div
                      key={entry.path}
                      className="flex items-center gap-2 w-full px-4 py-1.5 text-xs text-[hsl(var(--muted-foreground))] rounded-lg"
                    >
                      {multiSelect && <div className="h-4 w-4 shrink-0" />}
                      <Film className="h-3.5 w-3.5 shrink-0" />
                      <span className="truncate">{entry.name}</span>
                    </div>
                  ))}
                </>
              )}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end gap-2 p-4 border-t border-[hsl(var(--border))]">
          <Button variant="outline" onClick={onClose}>
            Annuler
          </Button>
          <Button
            onClick={handleConfirm}
            disabled={multiSelect ? selectionCount === 0 : !currentPath}
          >
            {multiSelect
              ? `Selectionner ${selectionCount} dossier${selectionCount > 1 ? "s" : ""}`
              : "Selectionner ce dossier"}
          </Button>
        </div>
      </div>
    </div>
  );
}
