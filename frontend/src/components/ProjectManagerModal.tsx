import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Loader2, UploadCloud, Trash2, X, RefreshCw, ChevronDown, User } from "lucide-react";
import { Button } from "@/components/ui";
import { api } from "@/api/client";
import { readSSEStream } from "@/utils/sse";
import type { ProjectManagerRow, Account } from "@/types";

type SortColumn = "uploaded" | "can_upload" | "language" | "anime_title" | "local_size_bytes" | "scheduled_at";
type SortDirection = "asc" | "desc";

interface ProjectManagerModalProps {
  open: boolean;
  onClose: () => void;
}

function formatBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let idx = 0;
  while (value >= 1024 && idx < units.length - 1) {
    value /= 1024;
    idx += 1;
  }
  return `${value.toFixed(value >= 10 ? 1 : 2)} ${units[idx]}`;
}

function formatScheduledAt(isoString: string | null): string {
  if (!isoString) return "";
  const date = new Date(isoString);
  if (isNaN(date.getTime())) return "";
  const now = new Date();
  if (date <= now) return "Uploaded";
  const day = date.getDate();
  const month = date.toLocaleString("en", { month: "short" });
  const hours = date.getHours().toString().padStart(2, "0");
  const minutes = date.getMinutes().toString().padStart(2, "0");
  return `${day} ${month} ${hours}:${minutes}`;
}

function statusCircle(color: "green" | "orange" | "red", title: string) {
  const className =
    color === "green"
      ? "bg-green-500"
      : color === "orange"
        ? "bg-amber-500"
        : "bg-red-500";
  return <span className={`h-3 w-3 rounded-full inline-block ${className}`} title={title} />;
}

/* ─── Account Picker Popup (for upload without account) ─── */
function AccountPickerPopup({
  accounts,
  onPick,
  onClose,
}: {
  accounts: Account[];
  onPick: (accountId: string) => void;
  onClose: () => void;
}) {
  return (
    <div className="fixed inset-0 z-[60] bg-black/50 flex items-center justify-center" onClick={onClose}>
      <div
        className="bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-lg p-4 min-w-[280px] max-w-sm"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="font-semibold mb-3">Select Account</h3>
        <div className="space-y-1">
          {accounts.map((acc) => (
            <button
              key={acc.id}
              type="button"
              className="w-full flex items-center gap-3 px-3 py-2 rounded-md hover:bg-[hsl(var(--muted))] text-left"
              onClick={() => onPick(acc.id)}
            >
              <img
                src={acc.avatar_url}
                alt=""
                className="h-7 w-7 rounded-full object-cover bg-[hsl(var(--muted))]"
              />
              <span className="flex-1 text-sm">{acc.name}</span>
              <span className="text-xs text-[hsl(var(--muted-foreground))] uppercase">{acc.language}</span>
            </button>
          ))}
        </div>
        <div className="mt-3 text-right">
          <Button variant="ghost" size="sm" onClick={onClose}>Cancel</Button>
        </div>
      </div>
    </div>
  );
}

/* ─── Delete Confirmation for Scheduled Projects ─── */
function ScheduledDeleteConfirm({
  scheduledAt,
  onConfirm,
  onCancel,
}: {
  scheduledAt: string;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="fixed inset-0 z-[60] bg-black/50 flex items-center justify-center" onClick={onCancel}>
      <div
        className="bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-lg p-5 max-w-sm"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="font-semibold mb-2">Delete Scheduled Project?</h3>
        <p className="text-sm text-[hsl(var(--muted-foreground))] mb-4">
          This project has a scheduled upload at <strong>{formatScheduledAt(scheduledAt)}</strong>. Delete anyway?
        </p>
        <div className="flex justify-end gap-2">
          <Button variant="ghost" size="sm" onClick={onCancel}>Cancel</Button>
          <Button variant="destructive" size="sm" onClick={onConfirm}>Delete</Button>
        </div>
      </div>
    </div>
  );
}

export function ProjectManagerModal({ open, onClose }: ProjectManagerModalProps) {
  const [rows, setRows] = useState<ProjectManagerRow[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sortColumn, setSortColumn] = useState<SortColumn>("uploaded");
  const [sortDirection, setSortDirection] = useState<SortDirection>("desc");

  const [selectedAccountId, setSelectedAccountId] = useState<string | null>(null);
  const [accountDropdownOpen, setAccountDropdownOpen] = useState(false);

  const [activeUploadId, setActiveUploadId] = useState<string | null>(null);
  const [activeDeleteId, setActiveDeleteId] = useState<string | null>(null);
  const [uploadMessage, setUploadMessage] = useState<string | null>(null);
  const [holdingDeleteId, setHoldingDeleteId] = useState<string | null>(null);
  const holdTimerRef = useRef<number | null>(null);

  // Account picker popup state (for upload without selected account)
  const [accountPickerForProject, setAccountPickerForProject] = useState<string | null>(null);
  // Scheduled delete confirmation
  const [deleteConfirmRow, setDeleteConfirmRow] = useState<ProjectManagerRow | null>(null);

  const selectedAccount = useMemo(
    () => accounts.find((a) => a.id === selectedAccountId) ?? null,
    [accounts, selectedAccountId],
  );

  const loadData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [projectsRes, accountsRes] = await Promise.all([
        api.listProjectManagerProjects(),
        api.listAccounts(),
      ]);
      setRows(projectsRes.projects);
      setAccounts(accountsRes.accounts);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (open) {
      loadData();
    }
  }, [open, loadData]);

  useEffect(() => {
    if (!open) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [open, onClose]);

  useEffect(
    () => () => {
      if (holdTimerRef.current) {
        window.clearTimeout(holdTimerRef.current);
      }
    },
    [],
  );

  // Filter rows based on selected account
  // - Uploaded/scheduled projects: only show if they were uploaded/scheduled by this account
  // - Other projects: show if their language matches the account
  const filteredRows = useMemo(() => {
    if (!selectedAccount) return rows;
    return rows.filter((r) => {
      if (r.uploaded || r.scheduled_at) {
        return r.scheduled_account_id === selectedAccount.id;
      }
      return r.language === selectedAccount.language;
    });
  }, [rows, selectedAccount]);

  const sortedRows = useMemo(() => {
    const statusWeight = (value: "green" | "orange" | "red") =>
      value === "green" ? 2 : value === "orange" ? 1 : 0;
    const direction = sortDirection === "asc" ? 1 : -1;
    return [...filteredRows].sort((a, b) => {
      let aValue: string | number = "";
      let bValue: string | number = "";
      if (sortColumn === "uploaded") {
        aValue = statusWeight(a.uploaded_status);
        bValue = statusWeight(b.uploaded_status);
      } else if (sortColumn === "can_upload") {
        aValue = statusWeight(a.can_upload_status);
        bValue = statusWeight(b.can_upload_status);
      } else if (sortColumn === "language") {
        aValue = (a.language || "").toLowerCase();
        bValue = (b.language || "").toLowerCase();
      } else if (sortColumn === "anime_title") {
        aValue = (a.anime_title || "").toLowerCase();
        bValue = (b.anime_title || "").toLowerCase();
      } else if (sortColumn === "scheduled_at") {
        aValue = a.scheduled_at || "";
        bValue = b.scheduled_at || "";
      } else {
        aValue = a.local_size_bytes;
        bValue = b.local_size_bytes;
      }
      if (aValue < bValue) return -1 * direction;
      if (aValue > bValue) return 1 * direction;
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
        // Show account picker - filter to compatible accounts
        setAccountPickerForProject(row.project_id);
      } else {
        // No accounts configured: upload with global credentials
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

  const handleDeleteAction = useCallback(
    (row: ProjectManagerRow) => {
      // If project has a future scheduled upload, show confirmation
      if (row.scheduled_at) {
        const scheduledDate = new Date(row.scheduled_at);
        if (scheduledDate > new Date()) {
          setDeleteConfirmRow(row);
          return;
        }
      }
      // Otherwise, proceed with hold-to-delete (existing behavior handled by mouse events)
    },
    [],
  );

  const startDeleteHold = (row: ProjectManagerRow) => {
    // Check for scheduled upload first
    if (row.scheduled_at) {
      const scheduledDate = new Date(row.scheduled_at);
      if (scheduledDate > new Date()) {
        setDeleteConfirmRow(row);
        return;
      }
    }
    if (holdTimerRef.current) {
      window.clearTimeout(holdTimerRef.current);
    }
    setHoldingDeleteId(row.project_id);
    holdTimerRef.current = window.setTimeout(() => {
      setHoldingDeleteId(null);
      runDelete(row.project_id);
      holdTimerRef.current = null;
    }, 1500);
  };

  const cancelDeleteHold = () => {
    if (holdTimerRef.current) {
      window.clearTimeout(holdTimerRef.current);
      holdTimerRef.current = null;
    }
    setHoldingDeleteId(null);
  };

  const colCount = 8; // uploaded, can_upload, lang, anime_title, local_size, scheduled_at, account, actions

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 bg-black/70 flex items-center justify-center p-6" onClick={onClose}>
      <div className="w-full max-w-6xl h-[85vh] bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-xl flex flex-col overflow-hidden" onClick={(e) => { e.stopPropagation(); setAccountDropdownOpen(false); }}>
        <header className="px-6 py-4 border-b border-[hsl(var(--border))] flex items-center justify-between">
          <div className="flex items-center gap-4">
            {/* Account Selector Dropdown */}
            <div className="relative">
              <button
                type="button"
                className="flex items-center gap-2 px-3 py-1.5 rounded-lg border border-[hsl(var(--border))] hover:bg-[hsl(var(--muted))] text-sm"
                onClick={(e) => { e.stopPropagation(); setAccountDropdownOpen((prev) => !prev); }}
              >
                {selectedAccount ? (
                  <>
                    <img
                      src={selectedAccount.avatar_url}
                      alt=""
                      className="h-6 w-6 rounded-full object-cover bg-[hsl(var(--muted))]"
                    />
                    <span>{selectedAccount.name}</span>
                  </>
                ) : (
                  <>
                    <User className="h-5 w-5 text-[hsl(var(--muted-foreground))]" />
                    <span>All Projects</span>
                  </>
                )}
                <ChevronDown className="h-3.5 w-3.5 text-[hsl(var(--muted-foreground))]" />
              </button>
              {accountDropdownOpen && (
                <div className="absolute top-full left-0 mt-1 z-10 bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-lg shadow-lg min-w-[200px] py-1">
                  <button
                    type="button"
                    className="w-full flex items-center gap-2 px-3 py-2 text-sm hover:bg-[hsl(var(--muted))] text-left"
                    onClick={() => {
                      setSelectedAccountId(null);
                      setAccountDropdownOpen(false);
                    }}
                  >
                    <User className="h-5 w-5 text-[hsl(var(--muted-foreground))]" />
                    <span>All Projects</span>
                  </button>
                  {accounts.map((acc) => (
                    <button
                      key={acc.id}
                      type="button"
                      className="w-full flex items-center gap-2 px-3 py-2 text-sm hover:bg-[hsl(var(--muted))] text-left"
                      onClick={() => {
                        setSelectedAccountId(acc.id);
                        setAccountDropdownOpen(false);
                      }}
                    >
                      <img
                        src={acc.avatar_url}
                        alt=""
                        className="h-5 w-5 rounded-full object-cover bg-[hsl(var(--muted))]"
                      />
                      <span className="flex-1">{acc.name}</span>
                      <span className="text-xs text-[hsl(var(--muted-foreground))] uppercase">{acc.language}</span>
                    </button>
                  ))}
                </div>
              )}
            </div>
            <div>
              <h2 className="text-xl font-semibold">Project Manager</h2>
              <p className="text-sm text-[hsl(var(--muted-foreground))]">
                Local projects with Google Drive upload readiness.
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={loadData} disabled={loading}>
              {loading ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <RefreshCw className="h-4 w-4" />
              )}
            </Button>
            <Button variant="ghost" size="sm" onClick={onClose}>
              <X className="h-4 w-4" />
            </Button>
          </div>
        </header>

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

        <div className="flex-1 overflow-auto p-6">
          <table className="w-full text-sm border-collapse">
            <thead>
              <tr className="border-b border-[hsl(var(--border))] text-left">
                <th className="py-2 pr-3">
                  <button
                    type="button"
                    className="font-medium hover:text-[hsl(var(--primary))]"
                    onClick={() => toggleSort("uploaded")}
                  >
                    Uploaded
                  </button>
                </th>
                <th className="py-2 pr-3">
                  <button
                    type="button"
                    className="font-medium hover:text-[hsl(var(--primary))]"
                    onClick={() => toggleSort("can_upload")}
                  >
                    Can Upload
                  </button>
                </th>
                <th className="py-2 pr-3">
                  <button
                    type="button"
                    className="font-medium hover:text-[hsl(var(--primary))]"
                    onClick={() => toggleSort("language")}
                  >
                    Lang
                  </button>
                </th>
                <th className="py-2 pr-3">
                  <button
                    type="button"
                    className="font-medium hover:text-[hsl(var(--primary))]"
                    onClick={() => toggleSort("anime_title")}
                  >
                    Anime Title
                  </button>
                </th>
                <th className="py-2 pr-3">
                  <button
                    type="button"
                    className="font-medium hover:text-[hsl(var(--primary))]"
                    onClick={() => toggleSort("local_size_bytes")}
                  >
                    Local Size
                  </button>
                </th>
                <th className="py-2 pr-3">
                  <button
                    type="button"
                    className="font-medium hover:text-[hsl(var(--primary))]"
                    onClick={() => toggleSort("scheduled_at")}
                  >
                    Scheduled At
                  </button>
                </th>
                <th className="py-2 pr-3">Account</th>
                <th className="py-2 pr-3">Actions</th>
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr>
                  <td colSpan={colCount} className="py-8 text-center text-[hsl(var(--muted-foreground))]">
                    <Loader2 className="h-4 w-4 animate-spin inline mr-2" />
                    Loading projects...
                  </td>
                </tr>
              ) : sortedRows.length === 0 ? (
                <tr>
                  <td colSpan={colCount} className="py-8 text-center text-[hsl(var(--muted-foreground))]">
                    No local projects found.
                  </td>
                </tr>
              ) : (
                sortedRows.map((row) => {
                  const canUpload = row.can_upload_status === "green" && !row.uploaded;
                  return (
                    <tr key={row.project_id} className="border-b border-[hsl(var(--border))/0.5]">
                      <td className="py-3 pr-3">
                        {statusCircle(
                          row.uploaded_status,
                          row.uploaded
                            ? "Uploaded"
                            : row.uploaded_status === "orange"
                              ? "Scheduled"
                              : "Not uploaded",
                        )}
                      </td>
                      <td className="py-3 pr-3">
                        {statusCircle(
                          row.can_upload_status,
                          row.can_upload_reasons.join("; ") || "Ready for upload",
                        )}
                      </td>
                      <td className="py-3 pr-3">
                        <span className="text-xs font-medium uppercase text-[hsl(var(--muted-foreground))]">
                          {row.language || ""}
                        </span>
                      </td>
                      <td className="py-3 pr-3">
                        <div className="font-medium">{row.anime_title || "Unknown"}</div>
                        <div className="text-xs text-[hsl(var(--muted-foreground))]">{row.project_id}</div>
                        {row.drive_folder_url && (
                          <a
                            href={row.drive_folder_url}
                            target="_blank"
                            rel="noreferrer"
                            className="text-xs text-[hsl(var(--primary))] hover:underline"
                            onClick={(e) => e.stopPropagation()}
                          >
                            Drive folder
                          </a>
                        )}
                      </td>
                      <td className="py-3 pr-3">{formatBytes(row.local_size_bytes)}</td>
                      <td className="py-3 pr-3">
                        <span className="text-xs text-[hsl(var(--muted-foreground))]">
                          {formatScheduledAt(row.scheduled_at) || (row.uploaded ? "Uploaded" : "")}
                        </span>
                      </td>
                      <td className="py-3 pr-3">
                        {row.scheduled_account_id && (() => {
                          const acc = accounts.find((a) => a.id === row.scheduled_account_id);
                          if (!acc) return <span className="text-xs text-[hsl(var(--muted-foreground))]">{row.scheduled_account_id}</span>;
                          return (
                            <div className="flex items-center gap-1.5">
                              <img src={acc.avatar_url} alt="" className="w-5 h-5 rounded-full object-cover" />
                              <span className="text-xs">{acc.name}</span>
                            </div>
                          );
                        })()}
                      </td>
                      <td className="py-3 pr-3">
                        <div className="flex items-center gap-2">
                          <Button
                            size="sm"
                            onClick={() => handleUploadClick(row)}
                            disabled={!canUpload || activeUploadId !== null}
                          >
                            {activeUploadId === row.project_id ? (
                              <Loader2 className="h-4 w-4 animate-spin" />
                            ) : (
                              <>
                                <UploadCloud className="h-4 w-4 mr-1.5" />
                                Upload
                              </>
                            )}
                          </Button>
                          <Button
                            size="sm"
                            variant="destructive"
                            className="relative overflow-hidden"
                            disabled={activeDeleteId !== null}
                            onMouseDown={() => startDeleteHold(row)}
                            onMouseUp={cancelDeleteHold}
                            onMouseLeave={cancelDeleteHold}
                            onTouchStart={() => startDeleteHold(row)}
                            onTouchEnd={cancelDeleteHold}
                            onTouchCancel={cancelDeleteHold}
                            title="Hold for 1.5 seconds to delete"
                          >
                            <span
                              className="absolute inset-0 bg-white/20 origin-left transition-transform"
                              style={{
                                transform: holdingDeleteId === row.project_id ? "scaleX(1)" : "scaleX(0)",
                                transitionDuration: holdingDeleteId === row.project_id ? "1.5s" : "0s",
                                transitionTimingFunction: "linear",
                              }}
                            />
                            {activeDeleteId === row.project_id ? (
                              <Loader2 className="h-4 w-4 animate-spin" />
                            ) : (
                              <>
                                <Trash2 className="h-4 w-4 mr-1.5" />
                                Hold to Delete
                              </>
                            )}
                          </Button>
                        </div>
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* Account picker popup */}
      {accountPickerForProject && compatibleAccounts.length > 0 && (
        <AccountPickerPopup
          accounts={compatibleAccounts}
          onPick={(accountId) => {
            const projectId = accountPickerForProject;
            setAccountPickerForProject(null);
            runUpload(projectId, accountId);
          }}
          onClose={() => setAccountPickerForProject(null)}
        />
      )}

      {/* Scheduled delete confirmation */}
      {deleteConfirmRow && deleteConfirmRow.scheduled_at && (
        <ScheduledDeleteConfirm
          scheduledAt={deleteConfirmRow.scheduled_at}
          onConfirm={() => {
            const projectId = deleteConfirmRow.project_id;
            setDeleteConfirmRow(null);
            runDelete(projectId);
          }}
          onCancel={() => setDeleteConfirmRow(null)}
        />
      )}
    </div>
  );
}
