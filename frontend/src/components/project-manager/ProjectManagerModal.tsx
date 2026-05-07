import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Loader2, RefreshCw, X, Trash2 } from "lucide-react";
import { Button } from "@/components/ui";
import { api } from "@/api/client";
import { readSSEStream } from "@/utils/sse";
import { AccountSelectorDropdown } from "./AccountSelectorDropdown";
import { AccountPickerPopup } from "./AccountPickerPopup";
import { CopyrightMusicModal } from "./CopyrightMusicModal";
import { CopyrightWarningModal } from "./CopyrightWarningModal";
import { FacebookDurationModal } from "./FacebookDurationModal";
import { ScheduledDeleteConfirm } from "./ScheduledDeleteConfirm";
import { UploadJobsPanel } from "./UploadJobsPanel";
import { VideoPreviewModal } from "./VideoPreviewModal";
import { ProjectTable } from "./ProjectTable";
import { SlotPickerPopover } from "./SlotPickerPopover";
import { UrgentCascadeModal } from "./UrgentCascadeModal";
import { YouTubeDurationModal } from "./YouTubeDurationModal";
import type { SortColumn, SortDirection, UploadMode, AnchorPayload } from "./types";
import {
  getLibraryTypeLabel,
  isAccountCompatibleWithProjectRow,
} from "@/utils/libraryTypes";
import type {
  Account,
  CopyrightCheckResult,
  FacebookCheckResult,
  Platform,
  ProjectManagerRow,
  ProjectUploadJob,
  UploadDurationStrategy,
  YouTubeCheckResult,
} from "@/types";

interface ProjectManagerModalProps {
  open: boolean;
  onClose: () => void;
}

interface PendingUploadContext {
  projectId: string;
  accountId?: string;
  facebookStrategy?: UploadDurationStrategy;
  youtubeStrategy?: UploadDurationStrategy;
  copyrightAudioPath?: string;
}

type UploadSessionStatus =
  | "checking_copyright"
  | "awaiting_copyright_music"
  | "awaiting_copyright_warning"
  | "checking_facebook"
  | "awaiting_facebook_choice"
  | "checking_youtube"
  | "awaiting_youtube_choice"
  | "enqueueing";

interface UploadSession {
  token: string;
  context: PendingUploadContext;
  status: UploadSessionStatus;
  message: string | null;
  copyrightResult?: CopyrightCheckResult;
  facebookResult?: FacebookCheckResult;
  youtubeResult?: YouTubeCheckResult;
  startedAt: number;
  updatedAt: number;
}

const LOAD_RETRY_DELAY_MS = 1000;
const LOAD_RETRY_WINDOW_MS = 45000;
const TERMINAL_RELOAD_DEBOUNCE_MS = 400;
const SSE_RECONNECT_DELAY_MS = 3000;

function sleep(ms: number) {
  return new Promise<void>((resolve) => {
    window.setTimeout(resolve, ms);
  });
}

function isTransientFetchError(error: unknown): boolean {
  if (!(error instanceof Error)) return false;
  const message = error.message.toLowerCase();
  return (
    message.includes("failed to fetch") ||
    message.includes("fetch failed") ||
    message.includes("networkerror") ||
    message.includes("load failed")
  );
}

function createUploadToken(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function isPromptSessionStatus(status: UploadSessionStatus): boolean {
  return (
    status === "awaiting_copyright_music" ||
    status === "awaiting_copyright_warning" ||
    status === "awaiting_facebook_choice" ||
    status === "awaiting_youtube_choice"
  );
}

function uploadButtonLabelForSession(session: UploadSession): string {
  switch (session.status) {
    case "checking_copyright":
    case "checking_facebook":
    case "checking_youtube":
      return "Checking";
    case "awaiting_copyright_music":
    case "awaiting_copyright_warning":
    case "awaiting_facebook_choice":
    case "awaiting_youtube_choice":
      return "Confirm";
    case "enqueueing":
      return "Queueing";
  }
}

function uploadButtonLabelForJob(job: ProjectUploadJob): string | null {
  if (job.status === "queued") {
    return "Queued";
  }
  if (job.status !== "running") {
    return null;
  }
  if (job.phase === "download") {
    return "Download";
  }
  if (job.phase === "platform_upload") {
    return "Uploading";
  }
  if (job.phase === "finalize") {
    return "Saving";
  }
  return "Working";
}

export function ProjectManagerModal({
  open,
  onClose,
}: ProjectManagerModalProps) {
  const [rows, setRows] = useState<ProjectManagerRow[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sortColumn, setSortColumn] = useState<SortColumn>("created_at");
  const [sortDirection, setSortDirection] = useState<SortDirection>("desc");

  const [selectedAccountId, setSelectedAccountId] = useState<string | null>(
    null,
  );
  const [accountDropdownOpen, setAccountDropdownOpen] = useState(false);

  const [uploadSessions, setUploadSessions] = useState<
    Record<string, UploadSession>
  >({});
  const [uploadJobs, setUploadJobs] = useState<
    Record<string, ProjectUploadJob>
  >({});
  const uploadSessionsRef = useRef<Record<string, UploadSession>>({});

  const [activeDeleteId, setActiveDeleteId] = useState<string | null>(null);
  const [holdingDeleteId, setHoldingDeleteId] = useState<string | null>(null);
  const holdTimerRef = useRef<number | null>(null);
  const reloadRowsTimerRef = useRef<number | null>(null);

  const [accountPickerForProject, setAccountPickerForProject] = useState<
    string | null
  >(null);
  const [deleteConfirmRow, setDeleteConfirmRow] =
    useState<ProjectManagerRow | null>(null);
  const [previewVideoId, setPreviewVideoId] = useState<string | null>(null);
  const loadRequestIdRef = useRef(0);

  const [multiDeleteMode, setMultiDeleteMode] = useState(false);
  const [selectedProjectIds, setSelectedProjectIds] = useState<Set<string>>(
    new Set(),
  );
  const [showMultiDeleteConfirm, setShowMultiDeleteConfirm] = useState(false);
  const [multiDeleting, setMultiDeleting] = useState(false);

  useEffect(() => {
    uploadSessionsRef.current = uploadSessions;
  }, [uploadSessions]);

  const selectedAccount = useMemo(
    () => accounts.find((a) => a.id === selectedAccountId) ?? null,
    [accounts, selectedAccountId],
  );

  const rowsByProjectId = useMemo(
    () =>
      rows.reduce<Record<string, ProjectManagerRow>>((acc, row) => {
        acc[row.project_id] = row;
        return acc;
      }, {}),
    [rows],
  );

  const loadData = useCallback(async () => {
    const requestId = ++loadRequestIdRef.current;
    const startedAt = Date.now();
    const isStale = () => loadRequestIdRef.current !== requestId;

    setError(null);
    setLoading(true);

    try {
      while (!isStale()) {
        try {
          const [projectsRes, accountsRes] = await Promise.allSettled([
            api.listProjectManagerProjects(),
            api.listAccounts(),
          ]);

          if (isStale()) return;

          if (accountsRes.status === "fulfilled") {
            setAccounts(accountsRes.value.accounts);
          }

          if (projectsRes.status === "rejected") {
            throw projectsRes.reason;
          }

          setRows(projectsRes.value.projects);
          setError(null);
          return;
        } catch (err) {
          if (isStale()) return;

          if (
            !isTransientFetchError(err) ||
            Date.now() - startedAt >= LOAD_RETRY_WINDOW_MS
          ) {
            setError((err as Error).message);
            return;
          }

          setError("Backend is still starting. Retrying...");
          await sleep(LOAD_RETRY_DELAY_MS);
        }
      }
    } finally {
      if (!isStale()) {
        setLoading(false);
      }
    }
  }, []);

  const scheduleRowsReload = useCallback(() => {
    if (reloadRowsTimerRef.current) {
      window.clearTimeout(reloadRowsTimerRef.current);
    }
    reloadRowsTimerRef.current = window.setTimeout(() => {
      reloadRowsTimerRef.current = null;
      void loadData();
    }, TERMINAL_RELOAD_DEBOUNCE_MS);
  }, [loadData]);

  const uploadJobsRef = useRef<Record<string, ProjectUploadJob>>({});
  const upsertUploadJob = useCallback(
    (job: ProjectUploadJob) => {
      const existing = uploadJobsRef.current[job.project_id];
      // Only schedule reload when a job reaches a NEW terminal state,
      // not when SSE reconnects and re-streams the same terminal jobs.
      const isNewTerminalState =
        (job.status === "complete" || job.status === "error") &&
        existing?.status !== job.status;

      uploadJobsRef.current[job.project_id] = job;
      setUploadJobs((prev) => ({
        ...prev,
        [job.project_id]: job,
      }));

      if (isNewTerminalState) {
        scheduleRowsReload();
      }
    },
    [scheduleRowsReload],
  );

  useEffect(() => {
    if (!open) {
      loadRequestIdRef.current += 1;
      return;
    }

    void loadData();

    return () => {
      loadRequestIdRef.current += 1;
    };
  }, [open, loadData]);

  useEffect(() => {
    if (!open) return;

    const controller = new AbortController();
    let reconnectTimer: number | null = null;

    const scheduleReconnect = () => {
      if (controller.signal.aborted || reconnectTimer !== null) {
        return;
      }
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null;
        void connect();
      }, SSE_RECONNECT_DELAY_MS);
    };

    const connect = async () => {
      try {
        const response = await api.streamProjectUploadJobs();
        await readSSEStream<ProjectUploadJob>(
          response,
          (job) => {
            upsertUploadJob(job);
          },
          { signal: controller.signal },
        );
      } catch {
        // Ignore transient SSE failures and reconnect below.
      } finally {
        scheduleReconnect();
      }
    };

    void api
      .listProjectUploadJobs()
      .then(({ jobs }) => {
        setUploadJobs((prev) => {
          const next = { ...prev };
          jobs.forEach((job) => {
            next[job.project_id] = job;
          });
          return next;
        });
      })
      .catch(() => {
        // SSE bootstrap below is the primary source of truth.
      });

    void connect();

    return () => {
      controller.abort();
      if (reconnectTimer !== null) {
        window.clearTimeout(reconnectTimer);
      }
    };
  }, [open, upsertUploadJob]);

  useEffect(() => {
    if (!open) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (previewVideoId) return;
      if (Object.values(uploadSessionsRef.current).some((session) => isPromptSessionStatus(session.status))) {
        return;
      }
      if (multiDeleteMode) {
        setMultiDeleteMode(false);
        setSelectedProjectIds(new Set());
        return;
      }
      onClose();
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [open, onClose, previewVideoId, multiDeleteMode]);

  useEffect(
    () => () => {
      if (holdTimerRef.current) window.clearTimeout(holdTimerRef.current);
      if (reloadRowsTimerRef.current) {
        window.clearTimeout(reloadRowsTimerRef.current);
      }
    },
    [],
  );

  useEffect(() => {
    if (!open) {
      setUploadSessions({});
      setAccountPickerForProject(null);
      setAccountDropdownOpen(false);
      setHoldingDeleteId(null);
      setPreviewVideoId(null);
    }
  }, [open]);

  const isSessionCurrent = useCallback((projectId: string, token: string) => {
    return uploadSessionsRef.current[projectId]?.token === token;
  }, []);

  const setUploadSession = useCallback((session: UploadSession) => {
    setUploadSessions((prev) => ({
      ...prev,
      [session.context.projectId]: session,
    }));
  }, []);

  const patchUploadSession = useCallback(
    (
      projectId: string,
      token: string,
      patch: Partial<UploadSession>,
    ) => {
      setUploadSessions((prev) => {
        const current = prev[projectId];
        if (!current || current.token !== token) {
          return prev;
        }
        return {
          ...prev,
          [projectId]: {
            ...current,
            ...patch,
            updatedAt: Date.now(),
          },
        };
      });
    },
    [],
  );

  const removeUploadSession = useCallback((projectId: string) => {
    setUploadSessions((prev) => {
      if (!prev[projectId]) {
        return prev;
      }
      const next = { ...prev };
      delete next[projectId];
      return next;
    });
  }, []);

  const enqueueUpload = useCallback(
    async (context: PendingUploadContext, token: string) => {
      patchUploadSession(context.projectId, token, {
        context,
        status: "enqueueing",
        message: "Queueing upload...",
      });
      setError(null);
      try {
        const job = await api.runProjectUpload(
          context.projectId,
          context.accountId,
          context.facebookStrategy,
          context.youtubeStrategy,
          context.copyrightAudioPath,
        );
        upsertUploadJob(job);
        if (isSessionCurrent(context.projectId, token)) {
          removeUploadSession(context.projectId);
        }
      } catch (err) {
        if (isSessionCurrent(context.projectId, token)) {
          removeUploadSession(context.projectId);
        }
        setError((err as Error).message);
      }
    },
    [isSessionCurrent, patchUploadSession, removeUploadSession, upsertUploadJob],
  );

  const continueUploadAfterFacebook = useCallback(
    async (context: PendingUploadContext, token: string) => {
      patchUploadSession(context.projectId, token, {
        context,
        status: "checking_youtube",
        message: "Vérification de la durée YouTube...",
        facebookResult: undefined,
      });
      setError(null);
      try {
        const result = await api.checkYouTubeDuration(
          context.projectId,
          context.accountId,
        );
        if (!isSessionCurrent(context.projectId, token)) return;

        if (result.needed) {
          patchUploadSession(context.projectId, token, {
            context,
            status: "awaiting_youtube_choice",
            message: null,
            youtubeResult: result,
          });
          return;
        }

        await enqueueUpload(context, token);
      } catch (err) {
        console.warn("YouTube check failed, proceeding with auto:", err);
        if (!isSessionCurrent(context.projectId, token)) return;
        await enqueueUpload(context, token);
      }
    },
    [enqueueUpload, isSessionCurrent, patchUploadSession],
  );

  const continueUploadAfterCopyright = useCallback(
    async (context: PendingUploadContext, token: string) => {
      patchUploadSession(context.projectId, token, {
        context,
        status: "checking_facebook",
        message: "Vérification de la durée Facebook...",
        copyrightResult: undefined,
      });
      setError(null);
      try {
        const result = await api.checkFacebookDuration(
          context.projectId,
          context.accountId,
        );
        if (!isSessionCurrent(context.projectId, token)) return;

        if (result.needed) {
          patchUploadSession(context.projectId, token, {
            context,
            status: "awaiting_facebook_choice",
            message: null,
            facebookResult: result,
          });
          return;
        }

        await continueUploadAfterFacebook(context, token);
      } catch (err) {
        console.warn("Facebook check failed, proceeding with auto:", err);
        if (!isSessionCurrent(context.projectId, token)) return;
        await continueUploadAfterFacebook(context, token);
      }
    },
    [continueUploadAfterFacebook, isSessionCurrent, patchUploadSession],
  );

  const startUploadWithChecks = useCallback(
    async (
      projectId: string,
      accountId?: string,
      mode: UploadMode = "auto",
      anchorPayload?: AnchorPayload,
    ) => {
      if (mode !== "auto" && !accountId) {
        setError("Manual scheduling requires an account selection");
        return;
      }

      if (mode === "scheduled" && anchorPayload) {
        try {
          await api.reserveAnchor(projectId, {
            account_id: accountId!,
            tiktok_slot: anchorPayload.tiktok_slot,
            overrides: anchorPayload.overrides,
          });
        } catch (err) {
          setError((err as Error).message);
          return;
        }
      }

      if (mode === "urgent") {
        try {
          await api.cascadeApply(projectId, accountId!);
        } catch (err) {
          setError((err as Error).message);
          return;
        }
      }

      const token = createUploadToken();
      const context: PendingUploadContext = { projectId, accountId };
      setUploadSession({
        token,
        context,
        status: "checking_copyright",
        message: "Vérification des droits musicaux...",
        startedAt: Date.now(),
        updatedAt: Date.now(),
      });
      setError(null);
      try {
        const result = await api.checkCopyright(projectId, accountId);
        if (!isSessionCurrent(projectId, token)) return;

        if (result.copyrighted) {
          patchUploadSession(projectId, token, {
            context,
            status: result.no_music_available
              ? "awaiting_copyright_music"
              : "awaiting_copyright_warning",
            message: null,
            copyrightResult: result,
          });
          return;
        }
      } catch (err) {
        console.warn("Copyright check failed, proceeding:", err);
        if (!isSessionCurrent(projectId, token)) return;
      }

      await continueUploadAfterCopyright(context, token);
    },
    [continueUploadAfterCopyright, isSessionCurrent, patchUploadSession, setUploadSession],
  );

  const exitMultiDeleteMode = useCallback(() => {
    setMultiDeleteMode(false);
    setSelectedProjectIds(new Set());
  }, []);

  const toggleMultiDeleteMode = () => {
    if (multiDeleteMode) {
      exitMultiDeleteMode();
      return;
    }
    setMultiDeleteMode(true);
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

  const filteredRows = useMemo(() => {
    if (!selectedAccount) return rows;
    return rows.filter((r) => {
      if (r.uploaded || r.scheduled_at) {
        return r.scheduled_account_id === selectedAccount.id;
      }
      return isAccountCompatibleWithProjectRow(selectedAccount, r);
    });
  }, [rows, selectedAccount]);

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
      } else if (sortColumn === "library_type") {
        aVal = getLibraryTypeLabel(a.library_type).toLowerCase();
        bVal = getLibraryTypeLabel(b.library_type).toLowerCase();
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

  const handleUploadClick = useCallback(
    (row: ProjectManagerRow) => {
      if (selectedAccountId) {
        void startUploadWithChecks(row.project_id, selectedAccountId);
      } else if (accounts.length > 0) {
        setAccountPickerForProject(row.project_id);
      } else {
        void startUploadWithChecks(row.project_id);
      }
    },
    [selectedAccountId, accounts, startUploadWithChecks],
  );

  const [schedulingForProject, setSchedulingForProject] = useState<{
    row: ProjectManagerRow;
    accountId: string;
  } | null>(null);
  const [urgentForProject, setUrgentForProject] = useState<{
    row: ProjectManagerRow;
    accountId: string;
  } | null>(null);

  const resolveAccountIdForRow = useCallback(
    (row: ProjectManagerRow): string | null => {
      if (selectedAccountId) return selectedAccountId;
      const compatible = accounts.find((a) =>
        isAccountCompatibleWithProjectRow(a, row),
      );
      return compatible?.id ?? null;
    },
    [selectedAccountId, accounts],
  );

  const handleUploadSchedule = useCallback(
    (row: ProjectManagerRow) => {
      const accountId = resolveAccountIdForRow(row);
      if (!accountId) {
        setError("Pick an account before scheduling manually.");
        return;
      }
      setSchedulingForProject({ row, accountId });
    },
    [resolveAccountIdForRow],
  );

  const handleUploadUrgent = useCallback(
    (row: ProjectManagerRow) => {
      const accountId = resolveAccountIdForRow(row);
      if (!accountId) {
        setError("Pick an account before urgent upload.");
        return;
      }
      setUrgentForProject({ row, accountId });
    },
    [resolveAccountIdForRow],
  );

  const compatibleAccounts = useMemo(() => {
    if (!accountPickerForProject) return [];
    const row = rows.find((r) => r.project_id === accountPickerForProject);
    if (!row) return [];
    return accounts.filter((a) => isAccountCompatibleWithProjectRow(a, row));
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
      void runDelete(row.project_id);
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

  const promptSessions = useMemo(
    () =>
      Object.values(uploadSessions)
        .filter((session) => isPromptSessionStatus(session.status))
        .sort((a, b) => a.startedAt - b.startedAt),
    [uploadSessions],
  );

  const uploadStateByProjectId = useMemo(() => {
    const next: Record<string, { active: boolean; label: string | null }> = {};

    Object.entries(uploadSessions).forEach(([projectId, session]) => {
      next[projectId] = {
        active: true,
        label: uploadButtonLabelForSession(session),
      };
    });

    Object.values(uploadJobs).forEach((job) => {
      const label = uploadButtonLabelForJob(job);
      if (!label) return;
      next[job.project_id] = {
        active: true,
        label,
      };
    });

    return next;
  }, [uploadJobs, uploadSessions]);

  if (!open) return null;

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className="fixed inset-0 z-50 bg-black/70 flex items-center justify-center p-6"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          onClick={() => {
            if (promptSessions.length > 0) return;
            onClose();
          }}
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
                        disabled={
                          selectedProjectIds.size === 0 || multiDeleting
                        }
                        onClick={() => setShowMultiDeleteConfirm(true)}
                        className="active:scale-95 transition-transform"
                      >
                        {multiDeleting ? (
                          <Loader2 className="h-4 w-4 animate-spin mr-1.5" />
                        ) : (
                          <Trash2 className="h-4 w-4 mr-1.5" />
                        )}
                        Delete
                        {selectedProjectIds.size > 0
                          ? ` (${selectedProjectIds.size})`
                          : ""}
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
                  {loading ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <RefreshCw className="h-4 w-4" />
                  )}
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={onClose}
                  disabled={promptSessions.length > 0}
                  className="active:scale-95 transition-transform"
                >
                  <X className="h-4 w-4" />
                </Button>
              </div>
            </header>

            {error && (
              <div className="mx-6 mt-4 p-3 rounded-md bg-[hsl(var(--destructive))]/10 text-sm text-[hsl(var(--destructive))]">
                {error}
              </div>
            )}

            <UploadJobsPanel
              jobs={Object.values(uploadJobs)}
              rowsByProjectId={rowsByProjectId}
            />

            <div className="flex-1 overflow-y-auto overflow-x-hidden p-6">
              <ProjectTable
                rows={sortedRows}
                accounts={accounts}
                selectedAccount={selectedAccount}
                loading={loading}
                sortColumn={sortColumn}
                sortDirection={sortDirection}
                onToggleSort={toggleSort}
                uploadStateByProjectId={uploadStateByProjectId}
                activeDeleteId={activeDeleteId}
                holdingDeleteId={holdingDeleteId}
                onUpload={handleUploadClick}
                onUploadSchedule={handleUploadSchedule}
                onUploadUrgent={handleUploadUrgent}
                onDeleteHoldStart={startDeleteHold}
                onDeleteHoldCancel={cancelDeleteHold}
                onPreview={(id) => setPreviewVideoId(id)}
                multiDeleteMode={multiDeleteMode}
                selectedProjectIds={selectedProjectIds}
                onToggleSelect={toggleSelectProject}
              />
            </div>
          </motion.div>

          <AccountPickerPopup
            open={!!accountPickerForProject && compatibleAccounts.length > 0}
            accounts={compatibleAccounts}
            onPick={(accountId) => {
              const projectId = accountPickerForProject!;
              setAccountPickerForProject(null);
              void startUploadWithChecks(projectId, accountId);
            }}
            onClose={() => setAccountPickerForProject(null)}
          />

          <AnimatePresence>
            {promptSessions.length > 0 && (
              <motion.div
                className="fixed inset-0 z-[60] bg-black/55 flex items-start justify-center p-6 pointer-events-none"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
              >
                <div className="max-w-6xl max-h-full overflow-y-auto flex flex-col items-center gap-5 py-4 pointer-events-auto">
                  {promptSessions.map((session) => {
                    const row = rowsByProjectId[session.context.projectId];
                    const projectTitle = row?.anime_title || "Projet";

                    if (
                      session.status === "awaiting_copyright_music" &&
                      session.copyrightResult?.no_music_available &&
                      session.copyrightResult.no_music_file_id
                    ) {
                      return (
                        <CopyrightMusicModal
                          key={`${session.context.projectId}:${session.token}:copyright-music`}
                          open
                          stacked
                          projectId={session.context.projectId}
                          projectTitle={projectTitle}
                          musicDisplayName={
                            session.copyrightResult.music_display_name || ""
                          }
                          noMusicFileId={session.copyrightResult.no_music_file_id}
                          availableMusics={
                            session.copyrightResult.available_musics || []
                          }
                          onConfirm={(copyrightAudioPath) => {
                            const nextContext: PendingUploadContext = {
                              ...session.context,
                              copyrightAudioPath: copyrightAudioPath ?? undefined,
                            };
                            void continueUploadAfterCopyright(
                              nextContext,
                              session.token,
                            );
                          }}
                          onCancel={() =>
                            removeUploadSession(session.context.projectId)
                          }
                        />
                      );
                    }

                    if (
                      session.status === "awaiting_copyright_warning" &&
                      session.copyrightResult
                    ) {
                      return (
                        <CopyrightWarningModal
                          key={`${session.context.projectId}:${session.token}:copyright-warning`}
                          open
                          stacked
                          projectTitle={projectTitle}
                          projectId={session.context.projectId}
                          musicDisplayName={
                            session.copyrightResult.music_display_name || ""
                          }
                          onContinueWithOriginal={() => {
                            void continueUploadAfterCopyright(
                              session.context,
                              session.token,
                            );
                          }}
                          onCancel={() =>
                            removeUploadSession(session.context.projectId)
                          }
                        />
                      );
                    }

                    if (
                      session.status === "awaiting_facebook_choice" &&
                      session.facebookResult
                    ) {
                      return (
                        <FacebookDurationModal
                          key={`${session.context.projectId}:${session.token}:facebook`}
                          open
                          stacked
                          projectId={session.context.projectId}
                          projectTitle={projectTitle}
                          durationSeconds={session.facebookResult.duration_seconds}
                          speedFactor={session.facebookResult.speed_factor}
                          spedUpAvailable={session.facebookResult.sped_up_available}
                          onChoice={(strategy) => {
                            const nextContext: PendingUploadContext = {
                              ...session.context,
                              facebookStrategy: strategy,
                            };
                            void continueUploadAfterFacebook(
                              nextContext,
                              session.token,
                            );
                          }}
                          onClose={() =>
                            removeUploadSession(session.context.projectId)
                          }
                        />
                      );
                    }

                    if (
                      session.status === "awaiting_youtube_choice" &&
                      session.youtubeResult
                    ) {
                      return (
                        <YouTubeDurationModal
                          key={`${session.context.projectId}:${session.token}:youtube`}
                          open
                          stacked
                          projectId={session.context.projectId}
                          projectTitle={projectTitle}
                          durationSeconds={session.youtubeResult.duration_seconds}
                          speedFactor={session.youtubeResult.speed_factor}
                          spedUpAvailable={session.youtubeResult.sped_up_available}
                          onChoice={(strategy) => {
                            const nextContext: PendingUploadContext = {
                              ...session.context,
                              youtubeStrategy: strategy,
                            };
                            void enqueueUpload(nextContext, session.token);
                          }}
                          onClose={() =>
                            removeUploadSession(session.context.projectId)
                          }
                        />
                      );
                    }

                    return null;
                  })}
                </div>
              </motion.div>
            )}
          </AnimatePresence>

          <ScheduledDeleteConfirm
            open={!!deleteConfirmRow?.scheduled_at}
            scheduledAt={deleteConfirmRow?.scheduled_at || ""}
            onConfirm={() => {
              const projectId = deleteConfirmRow!.project_id;
              setDeleteConfirmRow(null);
              void runDelete(projectId);
            }}
            onCancel={() => setDeleteConfirmRow(null)}
          />

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
                    Delete {selectedProjectIds.size} project
                    {selectedProjectIds.size !== 1 ? "s" : ""}?
                  </h3>
                  <p className="text-sm text-[hsl(var(--muted-foreground))] mb-4">
                    This will permanently delete all selected projects. This
                    action cannot be undone.
                  </p>
                  <div className="flex justify-end gap-2">
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setShowMultiDeleteConfirm(false)}
                    >
                      Cancel
                    </Button>
                    <Button
                      variant="destructive"
                      size="sm"
                      onClick={handleMultiDelete}
                    >
                      Delete All
                    </Button>
                  </div>
                </motion.div>
              </motion.div>
            )}
          </AnimatePresence>

          <VideoPreviewModal
            driveVideoId={previewVideoId}
            onClose={() => setPreviewVideoId(null)}
          />

          {schedulingForProject && (
            <SlotPickerPopover
              open
              mode="anchor"
              projectId={schedulingForProject.row.project_id}
              accountId={schedulingForProject.accountId}
              platformsForAnchor={
                ["tiktok", "youtube", "facebook", "instagram"] as Platform[]
              }
              onClose={() => setSchedulingForProject(null)}
              onConfirm={async (payload) => {
                const anchor = payload as {
                  tiktok_slot: string;
                  overrides?: Partial<Record<Platform, string>>;
                };
                const ctx = schedulingForProject;
                setSchedulingForProject(null);
                await startUploadWithChecks(
                  ctx.row.project_id,
                  ctx.accountId,
                  "scheduled",
                  anchor,
                );
              }}
            />
          )}

          {urgentForProject && (
            <UrgentCascadeModal
              open
              projectId={urgentForProject.row.project_id}
              projectTitle={urgentForProject.row.anime_title || "Project"}
              accountId={urgentForProject.accountId}
              onClose={() => setUrgentForProject(null)}
              onConfirmed={() => {
                const ctx = urgentForProject;
                setUrgentForProject(null);
                // Cascade already applied — call upload flow with mode=auto
                // so it consumes freshly-reserved slots via
                // _try_reuse_platform_reservation.
                void startUploadWithChecks(
                  ctx.row.project_id,
                  ctx.accountId,
                  "auto",
                );
              }}
            />
          )}
        </motion.div>
      )}
    </AnimatePresence>
  );
}
