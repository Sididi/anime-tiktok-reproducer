import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Loader2, RefreshCw, X, Trash2 } from "lucide-react";
import { Button } from "@/components/ui";
import { api } from "@/api/client";
import { readSSEStream } from "@/utils/sse";
import { AccountSelectorDropdown } from "./AccountSelectorDropdown";
import { AccountPickerPopup } from "./AccountPickerPopup";
import { ScheduledDeleteConfirm } from "./ScheduledDeleteConfirm";
import { VideoPreviewModal } from "./VideoPreviewModal";
import { ProjectTable } from "./ProjectTable";
import type { SortColumn, SortDirection } from "./types";
import type { ProjectManagerRow, Account } from "@/types";

interface ProjectManagerModalProps {
  open: boolean;
  onClose: () => void;
}

export function ProjectManagerModal({ open, onClose }: ProjectManagerModalProps) {
  const [rows, setRows] = useState<ProjectManagerRow[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sortColumn, setSortColumn] = useState<SortColumn>("created_at");
  const [sortDirection, setSortDirection] = useState<SortDirection>("desc");

  const [selectedAccountId, setSelectedAccountId] = useState<string | null>(null);
  const [accountDropdownOpen, setAccountDropdownOpen] = useState(false);

  const [activeUploadId, setActiveUploadId] = useState<string | null>(null);
  const [activeDeleteId, setActiveDeleteId] = useState<string | null>(null);
  const [uploadMessage, setUploadMessage] = useState<string | null>(null);
  const [holdingDeleteId, setHoldingDeleteId] = useState<string | null>(null);
  const holdTimerRef = useRef<number | null>(null);

  const [accountPickerForProject, setAccountPickerForProject] = useState<string | null>(null);
  const [deleteConfirmRow, setDeleteConfirmRow] = useState<ProjectManagerRow | null>(null);
  const [previewVideoId, setPreviewVideoId] = useState<string | null>(null);

  // Multi-delete state
  const [multiDeleteMode, setMultiDeleteMode] = useState(false);
  const [selectedProjectIds, setSelectedProjectIds] = useState<Set<string>>(new Set());
  const [showMultiDeleteConfirm, setShowMultiDeleteConfirm] = useState(false);
  const [multiDeleting, setMultiDeleting] = useState(false);

  const selectedAccount = useMemo(
    () => accounts.find((a) => a.id === selectedAccountId) ?? null,
    [accounts, selectedAccountId],
  );

  /* ── Data loading (accounts fast, projects slow) ── */
  const loadData = useCallback(async () => {
    setError(null);
    api.listAccounts().then((res) => setAccounts(res.accounts)).catch(() => {});
    setLoading(true);
    try {
      const projectsRes = await api.listProjectManagerProjects();
      setRows(projectsRes.projects);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (open) loadData();
  }, [open, loadData]);

  /* ── Escape key ── */
  useEffect(() => {
    if (!open) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        if (previewVideoId) return;
        if (multiDeleteMode) { exitMultiDeleteMode(); return; }
        onClose();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [open, onClose, previewVideoId, multiDeleteMode]);

  useEffect(
    () => () => {
      if (holdTimerRef.current) window.clearTimeout(holdTimerRef.current);
    },
    [],
  );

  /* ── Multi-delete ── */
  const exitMultiDeleteMode = () => {
    setMultiDeleteMode(false);
    setSelectedProjectIds(new Set());
  };

  const toggleMultiDeleteMode = () => {
    if (multiDeleteMode) exitMultiDeleteMode();
    else setMultiDeleteMode(true);
  };

  const toggleSelectProject = (id: string) => {
    setSelectedProjectIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const handleMultiDelete = async () => {
    setShowMultiDeleteConfirm(false);
    setMultiDeleting(true);
    setError(null);
    const ids = Array.from(selectedProjectIds);
    const failedIds: string[] = [];

    try {
      for (const id of ids) {
        try {
          await api.deleteManagedProject(id);
        } catch {
          failedIds.push(id);
        }
      }
    } finally {
      setSelectedProjectIds(new Set());
      setMultiDeleteMode(false);
      setMultiDeleting(false);
    }

    if (failedIds.length > 0) {
      const deletedCount = ids.length - failedIds.length;
      setError(
        deletedCount > 0
          ? `Deleted ${deletedCount}/${ids.length} selected projects. ${failedIds.length} failed.`
          : `Failed to delete ${failedIds.length} selected project${failedIds.length === 1 ? "" : "s"}.`,
      );
    }

    await loadData();
  };

  /* ── Filtering ── */
  const filteredRows = useMemo(() => {
    if (!selectedAccount) return rows;
    return rows.filter((r) => {
      if (r.uploaded || r.scheduled_at) return r.scheduled_account_id === selectedAccount.id;
      return r.language === selectedAccount.language;
    });
  }, [rows, selectedAccount]);

  /* ── Sorting ── */
  const sortedRows = useMemo(() => {
    const statusWeight = (value: "green" | "orange" | "red") =>
      value === "green" ? 2 : value === "orange" ? 1 : 0;
    const direction = sortDirection === "asc" ? 1 : -1;
    return [...filteredRows].sort((a, b) => {
      let aVal: string | number = "";
      let bVal: string | number = "";
      if (sortColumn === "uploaded") {
        aVal = statusWeight(a.uploaded_status);
        bVal = statusWeight(b.uploaded_status);
      } else if (sortColumn === "language") {
        aVal = (a.language || "").toLowerCase();
        bVal = (b.language || "").toLowerCase();
      } else if (sortColumn === "anime_title") {
        aVal = (a.anime_title || "").toLowerCase();
        bVal = (b.anime_title || "").toLowerCase();
      } else if (sortColumn === "scheduled_at") {
        aVal = a.scheduled_at || "";
        bVal = b.scheduled_at || "";
      } else if (sortColumn === "created_at") {
        aVal = a.created_at || "";
        bVal = b.created_at || "";
      } else {
        aVal = a.local_size_bytes;
        bVal = b.local_size_bytes;
      }
      if (aVal < bVal) return -1 * direction;
      if (aVal > bVal) return 1 * direction;
      return 0;
    });
  }, [filteredRows, sortColumn, sortDirection]);

  const toggleSort = (column: SortColumn) => {
    if (sortColumn === column) {
      setSortDirection((prev) => (prev === "asc" ? "desc" : "asc"));
      return;
    }
    setSortColumn(column);
    setSortDirection("desc");
  };

  /* ── Upload ── */
  const runUpload = useCallback(
    async (projectId: string, accountId?: string) => {
      setActiveUploadId(projectId);
      setUploadMessage("Starting upload...");
      setError(null);
      try {
        const response = await api.runProjectUpload(projectId, accountId);
        await readSSEStream(response, (event) => {
          if (event.message) setUploadMessage(event.message);
        });
        await loadData();
        setUploadMessage("Upload finished");
      } catch (err) {
        setError((err as Error).message);
      } finally {
        setActiveUploadId(null);
      }
    },
    [loadData],
  );

  const handleUploadClick = useCallback(
    (row: ProjectManagerRow) => {
      if (selectedAccountId) {
        runUpload(row.project_id, selectedAccountId);
      } else if (accounts.length > 0) {
        setAccountPickerForProject(row.project_id);
      } else {
        runUpload(row.project_id);
      }
    },
    [selectedAccountId, accounts, runUpload],
  );

  const compatibleAccounts = useMemo(() => {
    if (!accountPickerForProject) return [];
    const row = rows.find((r) => r.project_id === accountPickerForProject);
    if (!row || !row.language) return accounts;
    return accounts.filter((a) => a.language === row.language);
  }, [accountPickerForProject, rows, accounts]);

  /* ── Delete ── */
  const runDelete = useCallback(
    async (projectId: string) => {
      setActiveDeleteId(projectId);
      setError(null);
      try {
        await api.deleteManagedProject(projectId);
        await loadData();
      } catch (err) {
        setError((err as Error).message);
      } finally {
        setActiveDeleteId(null);
      }
    },
    [loadData],
  );

  const startDeleteHold = (row: ProjectManagerRow) => {
    if (row.scheduled_at) {
      const scheduledDate = new Date(row.scheduled_at);
      if (scheduledDate > new Date()) {
        setDeleteConfirmRow(row);
        return;
      }
    }
    if (holdTimerRef.current) window.clearTimeout(holdTimerRef.current);
    setHoldingDeleteId(row.project_id);
    holdTimerRef.current = window.setTimeout(() => {
      setHoldingDeleteId(null);
      runDelete(row.project_id);
      holdTimerRef.current = null;
    }, 1000);
  };

  const cancelDeleteHold = () => {
    if (holdTimerRef.current) {
      window.clearTimeout(holdTimerRef.current);
      holdTimerRef.current = null;
    }
    setHoldingDeleteId(null);
  };

  if (!open) return null;

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className="fixed inset-0 z-50 bg-black/70 flex items-center justify-center p-6"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          onClick={onClose}
        >
          <motion.div
            className="w-full max-w-6xl h-[85vh] bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-xl flex flex-col overflow-hidden"
            initial={{ scale: 0.95, opacity: 0 }}
            animate={{ scale: 1, opacity: 1 }}
            exit={{ scale: 0.95, opacity: 0 }}
            transition={{ duration: 0.2 }}
            onClick={(e) => {
              e.stopPropagation();
              setAccountDropdownOpen(false);
            }}
          >
            {/* Header */}
            <header className="px-6 py-4 border-b border-[hsl(var(--border))] flex items-center justify-between">
              <div className="flex items-center gap-4">
                <AccountSelectorDropdown
                  accounts={accounts}
                  selectedAccount={selectedAccount}
                  isOpen={accountDropdownOpen}
                  onToggle={() => setAccountDropdownOpen((prev) => !prev)}
                  onSelect={(id) => {
                    setSelectedAccountId(id);
                    setAccountDropdownOpen(false);
                  }}
                />
                <div>
                  <h2 className="text-xl font-semibold">Project Manager</h2>
                  <p className="text-sm text-[hsl(var(--muted-foreground))]">
                    Local projects with Google Drive upload readiness.
                  </p>
                </div>
              </div>
              <div className="flex items-center gap-2">
                {/* Multi-delete controls */}
                <AnimatePresence mode="wait">
                  {multiDeleteMode ? (
                    <motion.div
                      key="multi-delete-active"
                      className="flex items-center gap-2"
                      initial={{ opacity: 0, x: 10 }}
                      animate={{ opacity: 1, x: 0 }}
                      exit={{ opacity: 0, x: 10 }}
                      transition={{ duration: 0.15 }}
                    >
                      <Button
                        variant="destructive"
                        size="sm"
                        disabled={selectedProjectIds.size === 0 || multiDeleting}
                        onClick={() => setShowMultiDeleteConfirm(true)}
                        className="active:scale-95 transition-transform"
                      >
                        {multiDeleting ? (
                          <Loader2 className="h-4 w-4 animate-spin mr-1.5" />
                        ) : (
                          <Trash2 className="h-4 w-4 mr-1.5" />
                        )}
                        Delete{selectedProjectIds.size > 0 ? ` (${selectedProjectIds.size})` : ""}
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={exitMultiDeleteMode}
                        className="active:scale-95 transition-transform"
                      >
                        Cancel
                      </Button>
                    </motion.div>
                  ) : (
                    <motion.div
                      key="multi-delete-inactive"
                      initial={{ opacity: 0, x: 10 }}
                      animate={{ opacity: 1, x: 0 }}
                      exit={{ opacity: 0, x: 10 }}
                      transition={{ duration: 0.15 }}
                    >
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={toggleMultiDeleteMode}
                        title="Multi-select delete"
                        className="active:scale-95 transition-transform"
                      >
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    </motion.div>
                  )}
                </AnimatePresence>

                <Button
                  variant="outline"
                  size="sm"
                  onClick={loadData}
                  disabled={loading}
                  className="active:scale-95 transition-transform"
                >
                  {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={onClose}
                  className="active:scale-95 transition-transform"
                >
                  <X className="h-4 w-4" />
                </Button>
              </div>
            </header>

            {/* Messages */}
            {error && (
              <div className="mx-6 mt-4 p-3 rounded-md bg-[hsl(var(--destructive))]/10 text-sm text-[hsl(var(--destructive))]">
                {error}
              </div>
            )}
            {uploadMessage && (
              <div className="mx-6 mt-4 p-3 rounded-md bg-[hsl(var(--muted))] text-sm text-[hsl(var(--muted-foreground))]">
                {uploadMessage}
              </div>
            )}

            {/* Table */}
            <div className="flex-1 overflow-y-auto overflow-x-hidden p-6">
              <ProjectTable
                rows={sortedRows}
                accounts={accounts}
                loading={loading}
                sortColumn={sortColumn}
                sortDirection={sortDirection}
                onToggleSort={toggleSort}
                activeUploadId={activeUploadId}
                activeDeleteId={activeDeleteId}
                holdingDeleteId={holdingDeleteId}
                onUpload={handleUploadClick}
                onDeleteHoldStart={startDeleteHold}
                onDeleteHoldCancel={cancelDeleteHold}
                onPreview={(id) => setPreviewVideoId(id)}
                multiDeleteMode={multiDeleteMode}
                selectedProjectIds={selectedProjectIds}
                onToggleSelect={toggleSelectProject}
              />
            </div>
          </motion.div>

          {/* Account picker popup */}
          <AccountPickerPopup
            open={!!accountPickerForProject && compatibleAccounts.length > 0}
            accounts={compatibleAccounts}
            onPick={(accountId) => {
              const projectId = accountPickerForProject!;
              setAccountPickerForProject(null);
              runUpload(projectId, accountId);
            }}
            onClose={() => setAccountPickerForProject(null)}
          />

          {/* Scheduled delete confirmation */}
          <ScheduledDeleteConfirm
            open={!!deleteConfirmRow?.scheduled_at}
            scheduledAt={deleteConfirmRow?.scheduled_at || ""}
            onConfirm={() => {
              const projectId = deleteConfirmRow!.project_id;
              setDeleteConfirmRow(null);
              runDelete(projectId);
            }}
            onCancel={() => setDeleteConfirmRow(null)}
          />

          {/* Multi-delete confirmation */}
          <AnimatePresence>
            {showMultiDeleteConfirm && (
              <motion.div
                className="fixed inset-0 z-[60] bg-black/50 flex items-center justify-center"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                onClick={() => setShowMultiDeleteConfirm(false)}
              >
                <motion.div
                  className="bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-lg p-5 max-w-sm shadow-xl"
                  initial={{ scale: 0.95, opacity: 0 }}
                  animate={{ scale: 1, opacity: 1 }}
                  exit={{ scale: 0.95, opacity: 0 }}
                  transition={{ duration: 0.15 }}
                  onClick={(e) => e.stopPropagation()}
                >
                  <h3 className="font-semibold mb-2">
                    Delete {selectedProjectIds.size} project{selectedProjectIds.size !== 1 ? "s" : ""}?
                  </h3>
                  <p className="text-sm text-[hsl(var(--muted-foreground))] mb-4">
                    This will permanently delete all selected projects. This action cannot be undone.
                  </p>
                  <div className="flex justify-end gap-2">
                    <Button variant="ghost" size="sm" onClick={() => setShowMultiDeleteConfirm(false)}>
                      Cancel
                    </Button>
                    <Button variant="destructive" size="sm" onClick={handleMultiDelete}>
                      Delete All
                    </Button>
                  </div>
                </motion.div>
              </motion.div>
            )}
          </AnimatePresence>

          {/* Video preview */}
          <VideoPreviewModal
            driveVideoId={previewVideoId}
            onClose={() => setPreviewVideoId(null)}
          />
        </motion.div>
      )}
    </AnimatePresence>
  );
}
