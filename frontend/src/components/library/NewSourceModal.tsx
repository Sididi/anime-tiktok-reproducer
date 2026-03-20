import { useState, useEffect } from "react";
import { FolderOpen, ChevronDown, Layers, X } from "lucide-react";
import { AnimatePresence, motion } from "framer-motion";
import { Input } from "@/components/ui";
import type { LibraryType } from "@/types";
import { LIBRARY_TYPE_OPTIONS } from "@/utils/libraryTypes";
import { FolderBrowserModal } from "@/components/FolderBrowserModal";
import { BatchFolderFixModal } from "./BatchFolderFixModal";
import { BatchConflictModal } from "./BatchConflictModal";
import { api } from "@/api/client";

interface NewSourceModalProps {
  open: boolean;
  onClose: () => void;
  onSubmit: (
    path: string,
    name: string | undefined,
    type: LibraryType,
    fps: number,
  ) => void;
  onBatchSubmit?: (
    items: Array<{ path: string; name: string }>,
    type: LibraryType,
    fps: number,
  ) => void;
  currentLibraryType: LibraryType;
}

interface ValidationResult {
  path: string;
  name: string;
  has_videos: boolean;
  suggested_path: string | null;
  index_status: "new" | "exact_match" | "conflict";
  conflict_details: {
    new_episodes: string[];
    removed_episodes: string[];
    existing_episode_count: number;
    existing_torrent_count: number;
  } | null;
}

const FPS_OPTIONS = [1, 2, 4];

export function NewSourceModal({
  open,
  onClose,
  onSubmit,
  onBatchSubmit,
  currentLibraryType,
}: NewSourceModalProps) {
  const [path, setPath] = useState("");
  const [name, setName] = useState("");
  const [selectedType, setSelectedType] = useState<LibraryType>(currentLibraryType);
  const [fps, setFps] = useState(2);
  const [typeExpanded, setTypeExpanded] = useState(false);
  const [folderBrowserOpen, setFolderBrowserOpen] = useState(false);

  // Batch mode state
  const [batchMode, setBatchMode] = useState(false);
  const [batchPaths, setBatchPaths] = useState<string[]>([]);
  const [batchBrowserOpen, setBatchBrowserOpen] = useState(false);
  const [batchProcessing, setBatchProcessing] = useState(false);

  // Batch validation flow state
  const [validationResults, setValidationResults] = useState<ValidationResult[]>([]);
  const [fixQueue, setFixQueue] = useState<ValidationResult[]>([]);
  const [conflictQueue, setConflictQueue] = useState<ValidationResult[]>([]);
  const [currentFixIndex, setCurrentFixIndex] = useState(-1);
  const [currentConflictIndex, setCurrentConflictIndex] = useState(-1);
  const [resolvedItems, setResolvedItems] = useState<Array<{ path: string; name: string }>>([]);

  // Manual browse during fix flow
  const [fixBrowseOpen, setFixBrowseOpen] = useState(false);

  // Sync selectedType when modal opens with a new currentLibraryType
  useEffect(() => {
    if (open) {
      setSelectedType(currentLibraryType);
    }
  }, [open, currentLibraryType]);

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
    setBatchMode(false);
    setBatchPaths([]);
    setValidationResults([]);
    setFixQueue([]);
    setConflictQueue([]);
    setCurrentFixIndex(-1);
    setCurrentConflictIndex(-1);
    setResolvedItems([]);
    setBatchProcessing(false);
    onClose();
  };

  const handleBatchModeToggle = () => {
    setBatchMode((v) => !v);
    setBatchPaths([]);
    setPath("");
    setName("");
    setValidationResults([]);
    setFixQueue([]);
    setConflictQueue([]);
    setCurrentFixIndex(-1);
    setCurrentConflictIndex(-1);
    setResolvedItems([]);
    setBatchProcessing(false);
  };

  // -------------------------------------------------------------------------
  // Batch validation + fix/conflict flow
  // -------------------------------------------------------------------------
  const finalizeBatch = (items: Array<{ path: string; name: string }>) => {
    if (items.length > 0 && onBatchSubmit) {
      onBatchSubmit(items, selectedType, fps);
    }
    handleClose();
  };

  const startBatchValidation = async () => {
    if (batchPaths.length === 0) return;
    setBatchProcessing(true);

    try {
      const { results } = await api.validateBatchFolders(batchPaths, selectedType);
      setValidationResults(results);

      // Separate into categories
      const immediatelyValid: Array<{ path: string; name: string }> = [];
      const needsFix: ValidationResult[] = [];
      const hasConflict: ValidationResult[] = [];

      for (const r of results) {
        if (r.index_status === "exact_match") {
          // Silently skip — already indexed with same content
          continue;
        }
        if (!r.has_videos) {
          needsFix.push(r);
        } else if (r.index_status === "conflict") {
          hasConflict.push(r);
        } else {
          // "new" and has_videos — good to go
          immediatelyValid.push({ path: r.path, name: r.name });
        }
      }

      setResolvedItems(immediatelyValid);
      setFixQueue(needsFix);
      setConflictQueue(hasConflict);

      if (needsFix.length > 0) {
        setCurrentFixIndex(0);
      } else if (hasConflict.length > 0) {
        setCurrentConflictIndex(0);
      } else {
        // All resolved, submit immediately
        finalizeBatch(immediatelyValid);
      }
    } catch (err) {
      console.error("Batch validation failed:", err);
    } finally {
      setBatchProcessing(false);
    }
  };

  // Fix flow: handle resolution for current fix item
  const handleFixResolved = (resolvedPath: string | null) => {
    const currentItem = fixQueue[currentFixIndex];
    const nextItems = [...resolvedItems];

    if (resolvedPath) {
      nextItems.push({ path: resolvedPath, name: currentItem.name });
    }

    const nextIndex = currentFixIndex + 1;
    if (nextIndex < fixQueue.length) {
      setResolvedItems(nextItems);
      setCurrentFixIndex(nextIndex);
    } else {
      // Fix phase done, move to conflict phase
      setResolvedItems(nextItems);
      setCurrentFixIndex(-1);
      if (conflictQueue.length > 0) {
        setCurrentConflictIndex(0);
      } else {
        finalizeBatch(nextItems);
      }
    }
  };

  // Conflict flow: handle resolution for current conflict item
  const handleConflictResolved = (accepted: boolean) => {
    const currentItem = conflictQueue[currentConflictIndex];
    const nextItems = [...resolvedItems];

    if (accepted) {
      nextItems.push({ path: currentItem.path, name: currentItem.name });
    }

    const nextIndex = currentConflictIndex + 1;
    if (nextIndex < conflictQueue.length) {
      setResolvedItems(nextItems);
      setCurrentConflictIndex(nextIndex);
    } else {
      finalizeBatch(nextItems);
    }
  };

  const currentTypeLabel =
    LIBRARY_TYPE_OPTIONS.find((o) => o.value === selectedType)?.label ??
    selectedType;

  const currentFixItem =
    currentFixIndex >= 0 ? fixQueue[currentFixIndex] : null;
  const currentConflictItem =
    currentConflictIndex >= 0 ? conflictQueue[currentConflictIndex] : null;

  return (
    <>
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
        <div className="bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-xl max-w-lg w-full p-6 flex flex-col gap-4">
          {/* Header with batch toggle */}
          <div className="flex items-center justify-between">
            <h2 className="font-semibold text-lg">Nouvelle source</h2>
            <button
              onClick={handleBatchModeToggle}
              className={`flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-full transition-colors ${
                batchMode
                  ? "bg-[hsl(var(--primary))] text-white"
                  : "bg-[hsl(var(--secondary))] text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]"
              }`}
            >
              <Layers className="h-3 w-3" />
              Mode batch
            </button>
          </div>

          {/* Source path (single mode) */}
          <AnimatePresence mode="wait">
            {!batchMode ? (
              <motion.div
                key="single-path"
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: "auto" }}
                exit={{ opacity: 0, height: 0 }}
                transition={{ duration: 0.15 }}
                className="flex flex-col gap-1.5"
              >
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
              </motion.div>
            ) : (
              <motion.div
                key="batch-path"
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: "auto" }}
                exit={{ opacity: 0, height: 0 }}
                transition={{ duration: 0.15 }}
                className="flex flex-col gap-2"
              >
                <button
                  onClick={() => setBatchBrowserOpen(true)}
                  className="flex items-center justify-center gap-2 w-full py-3 bg-[hsl(var(--secondary))] hover:bg-[hsl(var(--secondary))]/80 rounded-lg transition-colors text-sm"
                >
                  <FolderOpen className="h-4 w-4" />
                  Selectionner des dossiers
                </button>
                {batchPaths.length > 0 && (
                  <div className="bg-[hsl(var(--muted))] rounded-lg p-3 max-h-32 overflow-y-auto space-y-1">
                    <p className="text-xs font-medium text-[hsl(var(--muted-foreground))] mb-1">
                      {batchPaths.length} dossier{batchPaths.length > 1 ? "s" : ""} selectionne{batchPaths.length > 1 ? "s" : ""}
                    </p>
                    {batchPaths.map((p) => {
                      const folderName = p.split("/").pop() || p;
                      return (
                        <div
                          key={p}
                          className="flex items-center justify-between text-xs"
                        >
                          <span className="truncate font-mono text-[hsl(var(--foreground))]">
                            {folderName}
                          </span>
                          <button
                            onClick={() =>
                              setBatchPaths((prev) =>
                                prev.filter((x) => x !== p),
                              )
                            }
                            className="p-0.5 rounded hover:bg-[hsl(var(--secondary))] shrink-0 ml-2"
                          >
                            <X className="h-3 w-3 text-[hsl(var(--muted-foreground))]" />
                          </button>
                        </div>
                      );
                    })}
                  </div>
                )}
              </motion.div>
            )}
          </AnimatePresence>

          {/* Source name (single mode only) */}
          <AnimatePresence>
            {!batchMode && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: "auto" }}
                exit={{ opacity: 0, height: 0 }}
                transition={{ duration: 0.15 }}
                className="flex flex-col gap-1.5"
              >
                <label className="text-sm text-[hsl(var(--muted-foreground))]">
                  Nom de la source
                </label>
                <Input
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="Nom (defaut: nom du dossier)"
                />
              </motion.div>
            )}
          </AnimatePresence>

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
              onClick={batchMode ? startBatchValidation : handleSubmit}
              disabled={
                batchMode
                  ? batchPaths.length === 0 || batchProcessing
                  : !path
              }
              className={`bg-[hsl(var(--primary))] text-white rounded-md px-4 py-2 text-sm font-medium transition-colors ${
                (batchMode ? batchPaths.length === 0 || batchProcessing : !path)
                  ? "opacity-50 cursor-not-allowed"
                  : "hover:bg-[hsl(var(--primary))]/90"
              }`}
            >
              {batchProcessing ? "Validation..." : "Indexer"}
            </button>
          </div>
        </div>
      </div>

      {/* Single-mode folder browser */}
      <FolderBrowserModal
        open={folderBrowserOpen}
        onClose={() => setFolderBrowserOpen(false)}
        onSelect={(selectedPath) => {
          setPath(selectedPath);
          setFolderBrowserOpen(false);
        }}
        initialPath={path || undefined}
      />

      {/* Batch-mode folder browser (multi-select) */}
      <FolderBrowserModal
        open={batchBrowserOpen}
        onClose={() => setBatchBrowserOpen(false)}
        onSelect={() => {}}
        multiSelect
        onSelectMultiple={(paths) => {
          setBatchPaths((prev) => {
            const existing = new Set(prev);
            const merged = [...prev];
            for (const p of paths) {
              if (!existing.has(p)) merged.push(p);
            }
            return merged;
          });
          setBatchBrowserOpen(false);
        }}
      />

      {/* Fix modal for folders without videos */}
      {currentFixItem && (
        <BatchFolderFixModal
          open
          folderName={currentFixItem.name}
          originalPath={currentFixItem.path}
          suggestedPath={currentFixItem.suggested_path}
          onAcceptSuggestion={(sugPath) => handleFixResolved(sugPath)}
          onBrowseManually={() => setFixBrowseOpen(true)}
          onSkip={() => handleFixResolved(null)}
        />
      )}

      {/* Manual browse during fix flow */}
      <FolderBrowserModal
        open={fixBrowseOpen}
        onClose={() => setFixBrowseOpen(false)}
        onSelect={(selectedPath) => {
          setFixBrowseOpen(false);
          handleFixResolved(selectedPath);
        }}
        initialPath={currentFixItem?.path}
      />

      {/* Conflict modal for already-indexed sources with differences */}
      {currentConflictItem && currentConflictItem.conflict_details && (
        <BatchConflictModal
          open
          sourceName={currentConflictItem.name}
          conflictDetails={currentConflictItem.conflict_details}
          onAccept={() => handleConflictResolved(true)}
          onSkip={() => handleConflictResolved(false)}
        />
      )}
    </>
  );
}
