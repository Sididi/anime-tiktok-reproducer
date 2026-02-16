import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Loader2, UploadCloud, Trash2, X, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui";
import { api } from "@/api/client";
import { readSSEStream } from "@/utils/sse";
import type { ProjectManagerRow } from "@/types";

type SortColumn = "uploaded" | "can_upload" | "anime_title" | "local_size_bytes";
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

function statusCircle(color: "green" | "orange" | "red", title: string) {
  const className =
    color === "green"
      ? "bg-green-500"
      : color === "orange"
        ? "bg-amber-500"
        : "bg-red-500";
  return <span className={`h-3 w-3 rounded-full inline-block ${className}`} title={title} />;
}

export function ProjectManagerModal({ open, onClose }: ProjectManagerModalProps) {
  const [rows, setRows] = useState<ProjectManagerRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sortColumn, setSortColumn] = useState<SortColumn>("uploaded");
  const [sortDirection, setSortDirection] = useState<SortDirection>("desc");

  const [activeUploadId, setActiveUploadId] = useState<string | null>(null);
  const [activeDeleteId, setActiveDeleteId] = useState<string | null>(null);
  const [uploadMessage, setUploadMessage] = useState<string | null>(null);
  const [holdingDeleteId, setHoldingDeleteId] = useState<string | null>(null);
  const holdTimerRef = useRef<number | null>(null);

  const loadRows = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const { projects } = await api.listProjectManagerProjects();
      setRows(projects);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (open) {
      loadRows();
    }
  }, [open, loadRows]);

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

  const sortedRows = useMemo(() => {
    const statusWeight = (value: "green" | "orange" | "red") =>
      value === "green" ? 2 : value === "orange" ? 1 : 0;
    const direction = sortDirection === "asc" ? 1 : -1;
    return [...rows].sort((a, b) => {
      let aValue: string | number = "";
      let bValue: string | number = "";
      if (sortColumn === "uploaded") {
        aValue = statusWeight(a.uploaded_status);
        bValue = statusWeight(b.uploaded_status);
      } else if (sortColumn === "can_upload") {
        aValue = statusWeight(a.can_upload_status);
        bValue = statusWeight(b.can_upload_status);
      } else if (sortColumn === "anime_title") {
        aValue = (a.anime_title || "").toLowerCase();
        bValue = (b.anime_title || "").toLowerCase();
      } else {
        aValue = a.local_size_bytes;
        bValue = b.local_size_bytes;
      }
      if (aValue < bValue) return -1 * direction;
      if (aValue > bValue) return 1 * direction;
      return 0;
    });
  }, [rows, sortColumn, sortDirection]);

  const toggleSort = (column: SortColumn) => {
    if (sortColumn === column) {
      setSortDirection((prev) => (prev === "asc" ? "desc" : "asc"));
      return;
    }
    setSortColumn(column);
    setSortDirection("desc");
  };

  const runUpload = useCallback(
    async (projectId: string) => {
      setActiveUploadId(projectId);
      setUploadMessage("Starting upload...");
      setError(null);
      try {
        const response = await api.runProjectUpload(projectId);
        await readSSEStream(response, (event) => {
          if (event.message) setUploadMessage(event.message);
        });
        await loadRows();
        setUploadMessage("Upload finished");
      } catch (err) {
        setError((err as Error).message);
      } finally {
        setActiveUploadId(null);
      }
    },
    [loadRows],
  );

  const runDelete = useCallback(
    async (projectId: string) => {
      setActiveDeleteId(projectId);
      setError(null);
      try {
        await api.deleteManagedProject(projectId);
        await loadRows();
      } catch (err) {
        setError((err as Error).message);
      } finally {
        setActiveDeleteId(null);
      }
    },
    [loadRows],
  );

  const startDeleteHold = (projectId: string) => {
    if (holdTimerRef.current) {
      window.clearTimeout(holdTimerRef.current);
    }
    setHoldingDeleteId(projectId);
    holdTimerRef.current = window.setTimeout(() => {
      setHoldingDeleteId(null);
      runDelete(projectId);
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

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 bg-black/70 flex items-center justify-center p-6" onClick={onClose}>
      <div className="w-full max-w-6xl h-[85vh] bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-xl flex flex-col overflow-hidden" onClick={(e) => e.stopPropagation()}>
        <header className="px-6 py-4 border-b border-[hsl(var(--border))] flex items-center justify-between">
          <div>
            <h2 className="text-xl font-semibold">Project Manager</h2>
            <p className="text-sm text-[hsl(var(--muted-foreground))]">
              Local projects with Google Drive upload readiness.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={loadRows} disabled={loading}>
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
                <th className="py-2 pr-3">Meta</th>
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
                <th className="py-2 pr-3">Actions</th>
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr>
                  <td colSpan={6} className="py-8 text-center text-[hsl(var(--muted-foreground))]">
                    <Loader2 className="h-4 w-4 animate-spin inline mr-2" />
                    Loading projects...
                  </td>
                </tr>
              ) : sortedRows.length === 0 ? (
                <tr>
                  <td colSpan={6} className="py-8 text-center text-[hsl(var(--muted-foreground))]">
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
                          row.uploaded ? "Uploaded" : "Not uploaded",
                        )}
                      </td>
                      <td className="py-3 pr-3">
                        {statusCircle(
                          row.can_upload_status,
                          row.can_upload_reasons.join("; ") || "Ready for upload",
                        )}
                      </td>
                      <td className="py-3 pr-3">
                        <span
                          className={`h-3 w-3 rounded-full inline-block ${row.has_metadata ? "bg-green-500" : "bg-[hsl(var(--border))]"}`}
                          title={row.has_metadata ? "Metadata ready" : "No metadata"}
                        />
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
                        <div className="flex items-center gap-2">
                          <Button
                            size="sm"
                            onClick={() => runUpload(row.project_id)}
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
                            onMouseDown={() => startDeleteHold(row.project_id)}
                            onMouseUp={cancelDeleteHold}
                            onMouseLeave={cancelDeleteHold}
                            onTouchStart={() => startDeleteHold(row.project_id)}
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
    </div>
  );
}
