import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  ChevronDown,
  CheckCircle2,
  XCircle,
  AlertTriangle,
  Loader2,
  X,
  Copy,
  Check,
} from "lucide-react";
import type { LibraryType } from "@/types";
import type {
  TorrentEntry,
  SourceTorrentMetadata,
  VerificationResult,
  ReplacementProgressEvent,
} from "@/types/library";
import { api } from "@/api/client";
import { readSSEStream } from "@/utils/sse";

type ModalState = "editing" | "verifying" | "results" | "reindexing";

interface TorrentManagementModalProps {
  open: boolean;
  onClose: () => void;
  sourceName: string;
  libraryType: LibraryType;
  focusTorrentId?: string;
  onComplete: () => void;
}

export function TorrentManagementModal({
  open,
  onClose,
  sourceName,
  libraryType,
  focusTorrentId,
  onComplete,
}: TorrentManagementModalProps) {
  const [state, setState] = useState<ModalState>("editing");
  const [metadata, setMetadata] = useState<SourceTorrentMetadata | null>(null);
  const [loading, setLoading] = useState(false);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [editedMagnets, setEditedMagnets] = useState<Record<string, string>>(
    {},
  );
  const [editingId, setEditingId] = useState<string | null>(null);
  const [verificationProgress, setVerificationProgress] =
    useState<ReplacementProgressEvent | null>(null);
  const [results, setResults] = useState<VerificationResult[]>([]);
  const [reindexProgress, setReindexProgress] =
    useState<ReplacementProgressEvent | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  // Load torrent metadata on open
  useEffect(() => {
    if (!open || !sourceName) return;
    setState("editing");
    setEditedMagnets({});
    setEditingId(null);
    setResults([]);
    setError(null);
    setVerificationProgress(null);
    setReindexProgress(null);

    setLoading(true);
    api
      .getSourceTorrents(libraryType, sourceName)
      .then((data) => {
        setMetadata(data);
        setExpandedId(focusTorrentId ?? data.torrents[0]?.id ?? null);
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [open, sourceName, libraryType, focusTorrentId]);

  const hasChanges = useMemo(
    () => Object.keys(editedMagnets).length > 0,
    [editedMagnets],
  );

  const handleEditMagnet = useCallback(
    (torrentId: string, value: string) => {
      const torrent = metadata?.torrents.find((t) => t.id === torrentId);
      if (!torrent) return;
      if (value === torrent.magnet_uri || value === "") {
        setEditedMagnets((prev) => {
          const next = { ...prev };
          delete next[torrentId];
          return next;
        });
      } else {
        setEditedMagnets((prev) => ({ ...prev, [torrentId]: value }));
      }
    },
    [metadata],
  );

  const handleCancelEdit = useCallback(
    (torrentId: string) => {
      setEditedMagnets((prev) => {
        const next = { ...prev };
        delete next[torrentId];
        return next;
      });
      setEditingId(null);
    },
    [],
  );

  const handleCopyMagnet = useCallback(
    (torrentId: string, magnet: string) => {
      navigator.clipboard.writeText(magnet);
      setCopiedId(torrentId);
      setTimeout(() => setCopiedId(null), 2000);
    },
    [],
  );

  // --- Verify & Apply ---
  const handleVerify = useCallback(async () => {
    if (!hasChanges) return;
    setState("verifying");
    setError(null);
    setVerificationProgress(null);

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      const replacements = Object.entries(editedMagnets).map(
        ([torrent_id, new_magnet_uri]) => ({ torrent_id, new_magnet_uri }),
      );

      const response = await api.replaceTorrents(
        sourceName,
        libraryType,
        replacements,
      );

      await readSSEStream<ReplacementProgressEvent & { status?: string; error?: string | null; message?: string | null }>(
        response,
        (event) => {
          setVerificationProgress(event);
          if (event.phase === "results" && event.verification_results) {
            setResults(event.verification_results);
            setState("results");
          }
          if (event.phase === "error") {
            setError(event.error || "Erreur inconnue");
            setState("results");
          }
        },
        { signal: controller.signal, stopWhen: (e) => e.phase === "results" || e.phase === "error" || e.phase === "complete" },
      );
    } catch (e: unknown) {
      if (e instanceof Error && e.name !== "AbortError") {
        setError(e.message);
        setState("results");
      }
    }
  }, [hasChanges, editedMagnets, sourceName, libraryType]);

  // --- Confirm Reindex ---
  const handleReindex = useCallback(async () => {
    const warnIds = results
      .filter((r) => r.status === "warn")
      .map((r) => r.torrent_id);
    if (warnIds.length === 0) {
      onComplete();
      onClose();
      return;
    }

    setState("reindexing");
    setError(null);
    setReindexProgress(null);

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      const response = await api.confirmReindex(
        sourceName,
        libraryType,
        warnIds,
      );

      await readSSEStream<ReplacementProgressEvent & { status?: string; error?: string | null; message?: string | null }>(
        response,
        (event) => {
          setReindexProgress(event);
          if (event.phase === "complete") {
            onComplete();
            onClose();
          }
          if (event.phase === "error") {
            setError(event.error || "Erreur lors de la réindexation");
          }
        },
        { signal: controller.signal, stopWhen: (e) => e.phase === "complete" || e.phase === "error" },
      );
    } catch (e: unknown) {
      if (e instanceof Error && e.name !== "AbortError") {
        setError(e.message);
      }
    }
  }, [results, sourceName, libraryType, onComplete, onClose]);

  // Apply PASS-only results (no reindex needed)
  const handleApplyPassOnly = useCallback(() => {
    onComplete();
    onClose();
  }, [onComplete, onClose]);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      abortRef.current?.abort();
    };
  }, []);

  if (!open) return null;

  const hasWarn = results.some((r) => r.status === "warn");
  const hasFail = results.some((r) => r.status === "fail");
  const allPass = results.length > 0 && results.every((r) => r.status === "pass");

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
      <div
        className="w-full max-w-xl border rounded-xl flex flex-col max-h-[85vh]"
        style={{
          backgroundColor: "hsl(var(--card))",
          borderColor: "hsl(var(--border))",
        }}
      >
        {/* Header */}
        <div
          className="flex items-center justify-between px-5 py-4 border-b shrink-0"
          style={{ borderColor: "hsl(var(--border))" }}
        >
          <div>
            <h2
              className="text-base font-semibold"
              style={{ color: "hsl(var(--foreground))" }}
            >
              {state === "editing" && "Gestion des épisodes"}
              {state === "verifying" && "Vérification en cours..."}
              {state === "results" && "Résultats de vérification"}
              {state === "reindexing" && "Réindexation en cours..."}
            </h2>
            <p
              className="text-xs mt-0.5"
              style={{ color: "hsl(var(--muted-foreground))" }}
            >
              {sourceName}
              {metadata &&
                ` · ${metadata.torrents.length} torrent(s)`}
            </p>
          </div>
          {(state === "editing" || state === "results") && (
            <button
              onClick={onClose}
              className="p-1 rounded-md hover:bg-white/5 transition-colors"
              style={{ color: "hsl(var(--muted-foreground))" }}
            >
              <X className="h-5 w-5" />
            </button>
          )}
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-5 py-3 space-y-2">
          {loading && (
            <div className="flex items-center justify-center py-8">
              <Loader2
                className="h-6 w-6 animate-spin"
                style={{ color: "hsl(var(--primary))" }}
              />
            </div>
          )}

          {error && state !== "results" && (
            <div
              className="text-sm p-3 rounded-lg"
              style={{
                backgroundColor: "hsl(var(--destructive) / 0.1)",
                color: "hsl(var(--destructive))",
              }}
            >
              {error}
            </div>
          )}

          {/* Editing state: accordion of torrents */}
          {state === "editing" && metadata && (
            <TorrentAccordion
              torrents={metadata.torrents}
              expandedId={expandedId}
              onToggle={(id) =>
                setExpandedId(expandedId === id ? null : id)
              }
              editedMagnets={editedMagnets}
              editingId={editingId}
              onStartEdit={setEditingId}
              onEditMagnet={handleEditMagnet}
              onCancelEdit={handleCancelEdit}
              copiedId={copiedId}
              onCopyMagnet={handleCopyMagnet}
            />
          )}

          {/* Verifying state */}
          {state === "verifying" && verificationProgress && (
            <VerificationProgressView progress={verificationProgress} />
          )}

          {/* Results state */}
          {state === "results" && (
            <ResultsView results={results} error={error} />
          )}

          {/* Reindexing state */}
          {state === "reindexing" && reindexProgress && (
            <ReindexProgressView progress={reindexProgress} />
          )}
        </div>

        {/* Footer */}
        <div
          className="flex justify-end gap-2 px-5 py-3 border-t shrink-0"
          style={{ borderColor: "hsl(var(--border))" }}
        >
          {state === "editing" && (
            <>
              <button
                onClick={onClose}
                className="px-4 py-2 text-sm rounded-lg transition-colors hover:bg-white/5"
                style={{ color: "hsl(var(--muted-foreground))" }}
              >
                Fermer
              </button>
              <button
                onClick={handleVerify}
                disabled={!hasChanges}
                className="px-4 py-2 text-sm rounded-lg font-medium text-white transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                style={{
                  backgroundColor: hasChanges
                    ? "hsl(var(--primary))"
                    : "hsl(var(--primary) / 0.5)",
                }}
              >
                Vérifier et appliquer
              </button>
            </>
          )}

          {state === "results" && (
            <>
              <button
                onClick={onClose}
                className="px-4 py-2 text-sm rounded-lg transition-colors hover:bg-white/5"
                style={{ color: "hsl(var(--muted-foreground))" }}
              >
                Annuler
              </button>
              {allPass && (
                <button
                  onClick={handleApplyPassOnly}
                  className="px-4 py-2 text-sm rounded-lg font-medium text-white"
                  style={{ backgroundColor: "hsl(142 71% 45%)" }}
                >
                  Appliquer
                </button>
              )}
              {hasWarn && !hasFail && (
                <button
                  onClick={handleReindex}
                  className="px-4 py-2 text-sm rounded-lg font-semibold"
                  style={{ backgroundColor: "#f59e0b", color: "#1e1e2e" }}
                >
                  Réindexer et appliquer
                </button>
              )}
              {hasWarn && hasFail && (
                <button
                  onClick={handleReindex}
                  className="px-4 py-2 text-sm rounded-lg font-semibold"
                  style={{ backgroundColor: "#f59e0b", color: "#1e1e2e" }}
                >
                  Appliquer les valides et réindexer
                </button>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

// --- Sub-components ---

function TorrentAccordion({
  torrents,
  expandedId,
  onToggle,
  editedMagnets,
  editingId,
  onStartEdit,
  onEditMagnet,
  onCancelEdit,
  copiedId,
  onCopyMagnet,
}: {
  torrents: TorrentEntry[];
  expandedId: string | null;
  onToggle: (id: string) => void;
  editedMagnets: Record<string, string>;
  editingId: string | null;
  onStartEdit: (id: string) => void;
  onEditMagnet: (id: string, value: string) => void;
  onCancelEdit: (id: string) => void;
  copiedId: string | null;
  onCopyMagnet: (id: string, magnet: string) => void;
}) {
  if (torrents.length === 0) {
    return (
      <p
        className="text-sm py-6 text-center"
        style={{ color: "hsl(var(--muted-foreground))" }}
      >
        Aucun torrent lié à cette source.
      </p>
    );
  }

  return (
    <div className="space-y-2">
      {torrents.map((torrent) => {
        const isExpanded = expandedId === torrent.id;
        const isModified = torrent.id in editedMagnets;
        const isEditing = editingId === torrent.id;

        return (
          <div
            key={torrent.id}
            className="rounded-lg overflow-hidden border transition-colors"
            style={{
              borderColor: isModified
                ? "hsl(45 93% 58% / 0.4)"
                : "hsl(var(--border))",
            }}
          >
            {/* Torrent header */}
            <button
              onClick={() => onToggle(torrent.id)}
              className="w-full flex items-center justify-between px-4 py-3 text-left transition-colors hover:bg-white/3"
              style={{
                backgroundColor: isExpanded
                  ? isModified
                    ? "hsl(45 93% 58% / 0.06)"
                    : "hsl(var(--primary) / 0.06)"
                  : "transparent",
              }}
            >
              <div className="flex items-center gap-2 min-w-0">
                <ChevronDown
                  className={`h-3.5 w-3.5 shrink-0 transition-transform ${isExpanded ? "" : "-rotate-90"}`}
                  style={{
                    color: isModified
                      ? "#fbbf24"
                      : isExpanded
                        ? "hsl(var(--primary))"
                        : "hsl(var(--muted-foreground))",
                  }}
                />
                <div className="min-w-0">
                  <div
                    className="text-sm font-medium truncate"
                    style={{ color: "hsl(var(--foreground))" }}
                  >
                    {torrent.torrent_name}
                  </div>
                  <div
                    className="text-xs"
                    style={{ color: "hsl(var(--muted-foreground))" }}
                  >
                    {torrent.files.length} épisode(s)
                  </div>
                </div>
              </div>
              {isModified && (
                <span
                  className="text-xs px-2 py-0.5 rounded shrink-0 ml-2"
                  style={{
                    backgroundColor: "hsl(45 93% 58% / 0.15)",
                    color: "#fbbf24",
                  }}
                >
                  Modifié
                </span>
              )}
            </button>

            {/* Expanded content */}
            <AnimatePresence initial={false}>
              {isExpanded && (
                <motion.div
                  initial={{ height: 0, opacity: 0 }}
                  animate={{ height: "auto", opacity: 1 }}
                  exit={{ height: 0, opacity: 0 }}
                  transition={{ duration: 0.2 }}
                  className="overflow-hidden"
                >
                  <div className="px-4 pb-3 space-y-3">
                    {/* Magnet URI section */}
                    <div>
                      <div
                        className="text-xs mb-1"
                        style={{ color: "hsl(var(--muted-foreground))" }}
                      >
                        Magnet URI
                      </div>
                      {isEditing || isModified ? (
                        <div className="flex gap-2">
                          <input
                            type="text"
                            value={
                              editedMagnets[torrent.id] ?? torrent.magnet_uri
                            }
                            onChange={(e) =>
                              onEditMagnet(torrent.id, e.target.value)
                            }
                            className="flex-1 text-xs px-2.5 py-1.5 rounded-md font-mono border outline-none focus:ring-1"
                            style={{
                              backgroundColor: "hsl(var(--background))",
                              borderColor: "hsl(45 93% 58% / 0.3)",
                              color: "#fbbf24",
                            }}
                            placeholder="magnet:?xt=urn:btih:..."
                            autoFocus={isEditing && !isModified}
                          />
                          <button
                            onClick={() => onCancelEdit(torrent.id)}
                            className="shrink-0 text-xs px-2.5 py-1.5 rounded-md border transition-colors hover:bg-white/5"
                            style={{
                              color: "hsl(var(--muted-foreground))",
                              borderColor: "hsl(var(--border))",
                            }}
                          >
                            Annuler
                          </button>
                        </div>
                      ) : (
                        <div className="flex gap-2">
                          <div
                            className="flex-1 text-xs px-2.5 py-1.5 rounded-md font-mono truncate"
                            style={{
                              backgroundColor: "hsl(var(--background))",
                              color: "hsl(var(--muted-foreground))",
                            }}
                          >
                            {torrent.magnet_uri}
                          </div>
                          <button
                            onClick={() =>
                              onCopyMagnet(torrent.id, torrent.magnet_uri)
                            }
                            className="shrink-0 p-1.5 rounded-md border transition-colors hover:bg-white/5"
                            style={{ borderColor: "hsl(var(--border))" }}
                          >
                            {copiedId === torrent.id ? (
                              <Check
                                className="h-3.5 w-3.5"
                                style={{ color: "hsl(142 71% 45%)" }}
                              />
                            ) : (
                              <Copy
                                className="h-3.5 w-3.5"
                                style={{
                                  color: "hsl(var(--muted-foreground))",
                                }}
                              />
                            )}
                          </button>
                          <button
                            onClick={() => onStartEdit(torrent.id)}
                            className="shrink-0 text-xs px-3 py-1.5 rounded-md text-white transition-colors"
                            style={{ backgroundColor: "hsl(var(--primary))" }}
                          >
                            Modifier
                          </button>
                        </div>
                      )}
                    </div>

                    {/* Episodes list */}
                    <div>
                      <div
                        className="text-xs mb-1"
                        style={{ color: "hsl(var(--muted-foreground))" }}
                      >
                        Épisodes liés
                      </div>
                      <div className="flex flex-wrap gap-1">
                        {torrent.files.map((f, i) => {
                          const name =
                            f.torrent_filename.split("/").pop() || f.torrent_filename;
                          // Truncate long names
                          const display =
                            name.length > 40
                              ? name.slice(0, 37) + "..."
                              : name;
                          return (
                            <span
                              key={i}
                              className="text-xs px-2 py-0.5 rounded"
                              style={{
                                backgroundColor: "hsl(var(--secondary))",
                                color: "hsl(var(--muted-foreground))",
                              }}
                              title={name}
                            >
                              {display}
                            </span>
                          );
                        })}
                      </div>
                    </div>
                  </div>
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        );
      })}
    </div>
  );
}

function VerificationProgressView({
  progress,
}: {
  progress: ReplacementProgressEvent;
}) {
  return (
    <div className="py-4 space-y-4">
      <div>
        <div className="flex justify-between mb-1">
          <span
            className="text-sm"
            style={{ color: "hsl(var(--foreground))" }}
          >
            {progress.message}
          </span>
          <span
            className="text-sm"
            style={{ color: "hsl(var(--primary))" }}
          >
            {Math.round(progress.progress * 100)}%
          </span>
        </div>
        <div
          className="h-1.5 rounded-full overflow-hidden"
          style={{ backgroundColor: "hsl(var(--secondary))" }}
        >
          <motion.div
            className="h-full rounded-full"
            style={{ backgroundColor: "hsl(var(--primary))" }}
            initial={{ width: 0 }}
            animate={{ width: `${progress.progress * 100}%` }}
            transition={{ duration: 0.3 }}
          />
        </div>
      </div>
      {progress.phase === "stalled" && (
        <div
          className="text-sm p-3 rounded-lg"
          style={{
            backgroundColor: "hsl(45 93% 58% / 0.1)",
            color: "#fbbf24",
          }}
        >
          ⚠ Téléchargement bloqué — VPN désactivé ?
        </div>
      )}
    </div>
  );
}

function ResultsView({
  results,
  error,
}: {
  results: VerificationResult[];
  error: string | null;
}) {
  return (
    <div className="space-y-2">
      {error && results.length === 0 && (
        <div
          className="text-sm p-3 rounded-lg"
          style={{
            backgroundColor: "hsl(var(--destructive) / 0.1)",
            color: "hsl(var(--destructive))",
          }}
        >
          {error}
        </div>
      )}
      {results.map((r) => (
        <div
          key={r.torrent_id}
          className="p-3 rounded-lg border"
          style={{
            borderColor:
              r.status === "pass"
                ? "hsl(142 71% 45% / 0.3)"
                : r.status === "warn"
                  ? "hsl(45 93% 58% / 0.3)"
                  : "hsl(var(--destructive) / 0.3)",
            backgroundColor:
              r.status === "pass"
                ? "hsl(142 71% 45% / 0.04)"
                : r.status === "warn"
                  ? "hsl(45 93% 58% / 0.04)"
                  : "hsl(var(--destructive) / 0.04)",
          }}
        >
          <div className="flex items-center gap-2 mb-1.5">
            {r.status === "pass" && (
              <CheckCircle2 className="h-4 w-4 shrink-0" style={{ color: "hsl(142 71% 45%)" }} />
            )}
            {r.status === "warn" && (
              <AlertTriangle className="h-4 w-4 shrink-0" style={{ color: "#fbbf24" }} />
            )}
            {r.status === "fail" && (
              <XCircle className="h-4 w-4 shrink-0" style={{ color: "hsl(var(--destructive))" }} />
            )}
            <span
              className="text-sm font-medium flex-1 min-w-0 truncate"
              style={{ color: "hsl(var(--foreground))" }}
            >
              {r.torrent_id}
            </span>
            <span
              className="text-xs px-2 py-0.5 rounded shrink-0"
              style={{
                backgroundColor:
                  r.status === "pass"
                    ? "hsl(142 71% 45% / 0.15)"
                    : r.status === "warn"
                      ? "hsl(45 93% 58% / 0.15)"
                      : "hsl(var(--destructive) / 0.15)",
                color:
                  r.status === "pass"
                    ? "hsl(142 71% 45%)"
                    : r.status === "warn"
                      ? "#fbbf24"
                      : "hsl(var(--destructive))",
              }}
            >
              {r.status.toUpperCase()}
            </span>
          </div>
          <div
            className="flex gap-4 text-xs"
            style={{ color: "hsl(var(--muted-foreground))" }}
          >
            <span>
              Match:{" "}
              <span
                style={{
                  color:
                    r.match_rate >= 0.85
                      ? "hsl(142 71% 45%)"
                      : r.match_rate >= 0.6
                        ? "#fbbf24"
                        : "hsl(var(--destructive))",
                }}
              >
                {Math.round(r.match_rate * 100)}%
              </span>
            </span>
            <span>
              Similarité:{" "}
              <span
                style={{
                  color:
                    r.avg_similarity >= 0.75
                      ? "hsl(142 71% 45%)"
                      : r.avg_similarity >= 0.55
                        ? "#fbbf24"
                        : "hsl(var(--destructive))",
                }}
              >
                {r.avg_similarity.toFixed(2)}
              </span>
            </span>
            <span>
              Offset:{" "}
              <span
                style={{
                  color:
                    r.offset_median < 0.5
                      ? "hsl(142 71% 45%)"
                      : "hsl(var(--destructive))",
                }}
              >
                {r.offset_median.toFixed(2)}s
              </span>
            </span>
          </div>
          {r.status === "warn" && (
            <div
              className="text-xs mt-2 p-2 rounded"
              style={{
                backgroundColor: "hsl(45 93% 58% / 0.06)",
                color: "#fbbf24",
              }}
            >
              {r.message}
            </div>
          )}
          {r.status === "fail" && (
            <div
              className="text-xs mt-2"
              style={{ color: "hsl(var(--destructive))" }}
            >
              {r.message}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

function ReindexProgressView({
  progress,
}: {
  progress: ReplacementProgressEvent;
}) {
  const steps = [
    {
      key: "downloading_reindex",
      label: "Téléchargement des épisodes",
    },
    {
      key: "removing_old_index",
      label: "Suppression des anciens index",
    },
    { key: "reindexing", label: "Réindexation FAISS" },
    { key: "cache_cleanup", label: "Nettoyage du cache" },
  ];

  const currentIndex = steps.findIndex((s) => s.key === progress.phase);

  return (
    <div className="py-4 space-y-3">
      {steps.map((step, i) => {
        const isDone = i < currentIndex;
        const isCurrent = i === currentIndex;
        const isPending = i > currentIndex;

        return (
          <div key={step.key} className="flex items-center gap-3">
            {isDone && (
              <CheckCircle2
                className="h-4 w-4 shrink-0"
                style={{ color: "hsl(142 71% 45%)" }}
              />
            )}
            {isCurrent && (
              <Loader2
                className="h-4 w-4 shrink-0 animate-spin"
                style={{ color: "hsl(var(--primary))" }}
              />
            )}
            {isPending && (
              <div
                className="h-4 w-4 shrink-0 rounded-full border"
                style={{ borderColor: "hsl(var(--muted-foreground) / 0.3)" }}
              />
            )}
            <div className="flex-1 min-w-0">
              <div
                className="text-sm"
                style={{
                  color: isPending
                    ? "hsl(var(--muted-foreground) / 0.4)"
                    : "hsl(var(--foreground))",
                }}
              >
                {step.label}
              </div>
              {isCurrent && progress.progress > 0 && (
                <div
                  className="h-1 rounded-full overflow-hidden mt-1"
                  style={{ backgroundColor: "hsl(var(--secondary))" }}
                >
                  <motion.div
                    className="h-full rounded-full"
                    style={{ backgroundColor: "hsl(var(--primary))" }}
                    initial={{ width: 0 }}
                    animate={{
                      width: `${progress.progress * 100}%`,
                    }}
                    transition={{ duration: 0.3 }}
                  />
                </div>
              )}
            </div>
            {isDone && (
              <span
                className="text-xs shrink-0"
                style={{ color: "hsl(142 71% 45%)" }}
              >
                Terminé
              </span>
            )}
            {isCurrent && (
              <span
                className="text-xs shrink-0"
                style={{ color: "hsl(var(--primary))" }}
              >
                {Math.round(progress.progress * 100)}%
              </span>
            )}
          </div>
        );
      })}
      {progress.phase === "error" && progress.error && (
        <div
          className="text-sm p-3 rounded-lg"
          style={{
            backgroundColor: "hsl(var(--destructive) / 0.1)",
            color: "hsl(var(--destructive))",
          }}
        >
          {progress.error}
        </div>
      )}
    </div>
  );
}
