import { useState, useEffect, useCallback, useRef } from "react";
import {
  LibraryHeader,
  IndexJobsPanel,
  StartupJobsPanel,
  SourceList,
  SearchBar,
  BottomBar,
  NewSourceModal,
  PurgeModal,
  TorrentManagementModal,
  DeleteSourceModal,
  RenameSourceModal,
} from "@/components/library";
import { FolderBrowserModal } from "@/components/FolderBrowserModal";
import { ProjectManagerModal } from "@/components/project-manager";
import { PlanningModal } from "@/components/planning";
import { DuplicateTikTokWarning } from "@/components/DuplicateTikTokWarning";
import { api, SeriesDeleteConflictError } from "@/api/client";
import { readSSEStream } from "@/utils/sse";
import type {
  LibraryType,
  SourceDetails,
  IndexationJob,
  ProjectStartupJob,
  SeriesDeleteReferencingProject,
} from "@/types";

export function ProjectSetup() {
  // Library state
  const [selectedLibraryType, setSelectedLibraryType] = useState<LibraryType>(
    () => (localStorage.getItem("libraryType") as LibraryType) || "anime",
  );
  const [sources, setSources] = useState<SourceDetails[]>([]);
  const [selectedSource, setSelectedSource] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState("");

  // TikTok URL + Start flow
  const [tiktokUrl, setTiktokUrl] = useState("");
  const [processing, setProcessing] = useState(false);
  const [statusText, setStatusText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [startupJobs, setStartupJobs] = useState<ProjectStartupJob[]>([]);
  const startupAbortRef = useRef<AbortController | null>(null);
  const startupTabsRef = useRef<Map<string, Window | null>>(new Map());

  // Modals
  const [showProjectManager, setShowProjectManager] = useState(false);
  const [showPlanning, setShowPlanning] = useState(false);
  const [showPurge, setShowPurge] = useState(false);
  const [showNewSource, setShowNewSource] = useState(false);
  const [showFolderBrowser, setShowFolderBrowser] = useState(false);
  const [updateSourceName, setUpdateSourceName] = useState<string | null>(null);
  const [episodeSource, setEpisodeSource] = useState<SourceDetails | null>(null);
  const [renameSource, setRenameSource] = useState<SourceDetails | null>(null);
  const [renameLoading, setRenameLoading] = useState(false);
  const [renameError, setRenameError] = useState<string | null>(null);
  const [deleteSource, setDeleteSource] = useState<SourceDetails | null>(null);
  const [deletingSourceId, setDeletingSourceId] = useState<string | null>(null);
  const [deleteLoading, setDeleteLoading] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [deleteBlockingProjects, setDeleteBlockingProjects] = useState<
    SeriesDeleteReferencingProject[]
  >([]);

  // Duplicate TikTok warning
  const [duplicateWarning, setDuplicateWarning] = useState<{
    videoId: string;
    registeredAt: string | null;
  } | null>(null);

  // Purge estimate
  const [purgeEstimatedBytes, setPurgeEstimatedBytes] = useState(0);
  const [purgeSourceCount, setPurgeSourceCount] = useState(0);

  // ---------------------------------------------------------------------------
  // Load sources
  // ---------------------------------------------------------------------------
  const loadSources = useCallback(async () => {
    try {
      const details = await api.getSourceDetails(selectedLibraryType);
      setSources(details);
    } catch (err) {
      console.error("Failed to load sources:", err);
      setSources([]);
    }
  }, [selectedLibraryType]);

  useEffect(() => {
    void loadSources();
  }, [loadSources]);

  const handleJobComplete = useCallback(
    async (job: IndexationJob) => {
      await loadSources();
      if (job.library_type === selectedLibraryType && job.series_id) {
        setSelectedSource(job.series_id);
      }
    },
    [loadSources, selectedLibraryType],
  );

  // Persist library type to localStorage
  useEffect(() => {
    localStorage.setItem("libraryType", selectedLibraryType);
  }, [selectedLibraryType]);

  // ---------------------------------------------------------------------------
  // Background startup flow
  // ---------------------------------------------------------------------------
  const renderStartupWindow = useCallback(
    (
      popup: Window | null,
      title: string,
      message: string,
      isError = false,
    ) => {
      if (!popup || popup.closed) {
        return;
      }
      try {
        const doc = popup.document;
        doc.title = title;
        if (!doc.body) {
          doc.write("<!doctype html><html><head><title></title></head><body></body></html>");
          doc.close();
        }
        const body = doc.body;
        body.innerHTML = "";
        body.style.margin = "0";
        body.style.fontFamily = "Inter, sans-serif";
        body.style.background = isError ? "#1f1010" : "#0f172a";
        body.style.color = "#f8fafc";

        const wrapper = doc.createElement("div");
        wrapper.style.minHeight = "100vh";
        wrapper.style.display = "flex";
        wrapper.style.alignItems = "center";
        wrapper.style.justifyContent = "center";
        wrapper.style.padding = "24px";

        const card = doc.createElement("div");
        card.style.width = "min(520px, 100%)";
        card.style.borderRadius = "16px";
        card.style.padding = "24px";
        card.style.background = isError ? "#3b1616" : "#111827";
        card.style.boxSizing = "border-box";

        const heading = doc.createElement("h1");
        heading.textContent = title;
        heading.style.margin = "0 0 12px 0";
        heading.style.fontSize = "22px";

        const detail = doc.createElement("p");
        detail.textContent = message;
        detail.style.margin = "0";
        detail.style.lineHeight = "1.5";
        detail.style.color = "#cbd5e1";

        card.appendChild(heading);
        card.appendChild(detail);
        wrapper.appendChild(card);
        body.appendChild(wrapper);
      } catch {
        // Ignore popup update failures once the user navigates away.
      }
    },
    [],
  );

  const openStartupWindow = useCallback(() => {
    const popup = window.open("", "_blank");
    renderStartupWindow(
      popup,
      "Project startup in progress",
      "Preparing download, scene detection, and library activation...",
    );
    return popup;
  }, [renderStartupWindow]);

  const upsertStartupJob = useCallback(
    (job: ProjectStartupJob) => {
      setStartupJobs((prev) => {
        const idx = prev.findIndex((entry) => entry.project_id === job.project_id);
        if (idx >= 0) {
          const next = [...prev];
          next[idx] = job;
          return next;
        }
        return [job, ...prev];
      });

      const popup = startupTabsRef.current.get(job.project_id) ?? null;
      if (job.status === "complete" && job.ready_url) {
        if (popup && !popup.closed) {
          try {
            popup.location.href = job.ready_url;
          } catch {
            window.open(job.ready_url, "_blank");
          }
        }
        startupTabsRef.current.delete(job.project_id);
        return;
      }

      if (job.status === "error") {
        renderStartupWindow(
          popup,
          "Project startup failed",
          job.error || "Startup failed.",
          true,
        );
        return;
      }

      renderStartupWindow(
        popup,
        "Project startup in progress",
        job.message || "Working...",
      );
    },
    [renderStartupWindow],
  );

  useEffect(() => {
    startupAbortRef.current?.abort();
    const controller = new AbortController();
    startupAbortRef.current = controller;
    let reconnectTimer: number | null = null;

    const scheduleReconnect = () => {
      if (controller.signal.aborted || reconnectTimer !== null) {
        return;
      }
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null;
        void connect();
      }, 3000);
    };

    const connect = async () => {
      try {
        const response = await api.streamProjectStartupJobs();
        await readSSEStream<ProjectStartupJob>(
          response,
          (job) => {
            upsertStartupJob(job);
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
      .listProjectStartupJobs()
      .then(({ jobs }) => {
        jobs.forEach((job) => {
          upsertStartupJob(job);
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
  }, [upsertStartupJob]);

  const proceedWithStart = useCallback(async (popup: Window | null) => {
    if (!tiktokUrl.trim() || !selectedSource) return;
    setProcessing(true);
    setError(null);
    setStatusText("Launching...");

    try {
      const selectedSourceDetails = sources.find(
        (source) => source.series_id === selectedSource,
      );
      if (!selectedSourceDetails) {
        throw new Error("Selected source not found");
      }

      const job = await api.startProjectAsync(
        tiktokUrl,
        selectedSourceDetails.name,
        selectedSourceDetails.series_id,
        selectedLibraryType,
      );
      if (popup) {
        startupTabsRef.current.set(job.project_id, popup);
      }
      upsertStartupJob(job);
      setTiktokUrl("");
    } catch (err) {
      if (popup && !popup.closed) {
        popup.close();
      }
      setError((err as Error).message);
    } finally {
      setProcessing(false);
      setStatusText("");
    }
  }, [tiktokUrl, selectedSource, selectedLibraryType, sources, upsertStartupJob]);

  const handleOpenStartupJob = useCallback((job: ProjectStartupJob) => {
    if (!job.ready_url) {
      return;
    }
    const popup = startupTabsRef.current.get(job.project_id) ?? null;
    if (popup && !popup.closed) {
      try {
        popup.location.href = job.ready_url;
        return;
      } catch {
        // Fall back to a fresh tab.
      }
    }
    window.open(job.ready_url, "_blank");
  }, []);

  const handleRetryStartup = useCallback(
    async (job: ProjectStartupJob) => {
      setError(null);
      const popup = openStartupWindow();
      setProcessing(true);
      setStatusText("Relaunching...");
      try {
        const nextJob = await api.retryProjectStartup(job.project_id);
        if (popup) {
          startupTabsRef.current.set(nextJob.project_id, popup);
        }
        upsertStartupJob(nextJob);
      } catch (err) {
        if (popup && !popup.closed) {
          popup.close();
        }
        setError((err as Error).message);
      } finally {
        setProcessing(false);
        setStatusText("");
      }
    },
    [openStartupWindow, upsertStartupJob],
  );

  const handleStart = useCallback(async () => {
    if (!tiktokUrl.trim() || !selectedSource) return;
    setError(null);
    setSearchQuery("");
    const popup = openStartupWindow();

    try {
      const result = await api.checkTiktokUrl(tiktokUrl);
      if (result.exists && result.video_id) {
        if (popup && !popup.closed) {
          popup.close();
        }
        setDuplicateWarning({
          videoId: result.video_id,
          registeredAt: result.registered_at,
        });
        return;
      }
    } catch {
      // If check fails, proceed anyway
    }

    void proceedWithStart(popup);
  }, [tiktokUrl, selectedSource, openStartupWindow, proceedWithStart]);

  // ---------------------------------------------------------------------------
  // New source submission (async indexing)
  // ---------------------------------------------------------------------------
  const handleNewSourceSubmit = useCallback(
    async (
      path: string,
      name: string | undefined,
      type: LibraryType,
      fps: number,
    ) => {
      try {
        await api.indexAnimeAsync(path, type, name, fps);
        setShowNewSource(false);
        // IndexJobsPanel SSE will show progress and trigger reload on complete
      } catch (err) {
        setError((err as Error).message);
      }
    },
    [],
  );

  // ---------------------------------------------------------------------------
  // Batch source submission (async indexing for multiple sources)
  // ---------------------------------------------------------------------------
  const handleBatchSourceSubmit = useCallback(
    async (
      items: Array<{ path: string; name: string; jobType: "index" | "update" }>,
      type: LibraryType,
      fps: number,
    ) => {
      try {
        const results = await Promise.allSettled(
          items.map((item) =>
            item.jobType === "update"
              ? api.updateAnimeAsync(item.path, type, item.name)
              : api.indexAnimeAsync(item.path, type, item.name, fps),
          ),
        );
        const rejected = results.find(
          (result): result is PromiseRejectedResult => result.status === "rejected",
        );
        if (rejected) {
          throw rejected.reason;
        }
        setShowNewSource(false);
      } catch (err) {
        setError((err as Error).message);
      }
    },
    [],
  );

  // ---------------------------------------------------------------------------
  // Purge
  // ---------------------------------------------------------------------------
  const handleOpenPurge = useCallback(async () => {
    try {
      const estimate = await api.estimatePurgeSize(
        selectedLibraryType,
        false,
      );
      setPurgeEstimatedBytes(estimate.estimated_bytes);
      setPurgeSourceCount(estimate.source_count);
    } catch {
      /* ignore */
    }
    setShowPurge(true);
  }, [selectedLibraryType]);

  const handlePurgeConfirm = useCallback(
    async (allTypes: boolean) => {
      try {
        await api.purgeLibrary(selectedLibraryType, allTypes);
        setShowPurge(false);
        await loadSources();
      } catch (err) {
        setError((err as Error).message);
      }
    },
    [selectedLibraryType, loadSources],
  );

  // ---------------------------------------------------------------------------
  // Protection toggle
  // ---------------------------------------------------------------------------
  const handleToggleProtection = useCallback(
    async (seriesId: string) => {
      try {
        const result = await api.togglePermanentPin(
          selectedLibraryType,
          seriesId,
        );
        setSources((prev) =>
          prev.map((s) =>
            s.series_id === seriesId
              ? { ...s, permanent_pin: result.permanent_pin }
              : s,
          ),
        );
      } catch (err) {
        setError((err as Error).message);
      }
    },
    [selectedLibraryType],
  );

  const handleOpenDeleteSource = useCallback((source: SourceDetails) => {
    setDeleteSource(source);
    setDeleteError(null);
    setDeleteBlockingProjects([]);
  }, []);

  const handleOpenRenameSource = useCallback((source: SourceDetails) => {
    setRenameSource(source);
    setRenameError(null);
  }, []);

  const handleCloseRenameSource = useCallback(() => {
    if (renameLoading) {
      return;
    }
    setRenameSource(null);
    setRenameError(null);
  }, [renameLoading]);

  const handleRenameSource = useCallback(
    async (newName: string) => {
      if (!renameSource) {
        return;
      }

      setRenameLoading(true);
      setRenameError(null);
      try {
        await api.renameSeries(
          selectedLibraryType,
          renameSource.series_id,
          newName,
        );
        setRenameSource(null);
        await loadSources();
      } catch (err) {
        setRenameError((err as Error).message);
      } finally {
        setRenameLoading(false);
      }
    },
    [loadSources, renameSource, selectedLibraryType],
  );

  const handleCloseDeleteSource = useCallback(() => {
    if (deleteLoading) {
      return;
    }
    setDeleteSource(null);
    setDeleteError(null);
    setDeleteBlockingProjects([]);
  }, [deleteLoading]);

  const handleDeleteSource = useCallback(async () => {
    if (!deleteSource) {
      return;
    }

    setDeleteLoading(true);
    setDeleteError(null);
    setDeleteBlockingProjects([]);
    setDeletingSourceId(deleteSource.series_id);

    try {
      await api.deleteSeries(selectedLibraryType, deleteSource.series_id);
      setSelectedSource((current) =>
        current === deleteSource.series_id ? null : current,
      );
      setDeleteSource(null);
      await loadSources();
    } catch (err) {
      if (err instanceof SeriesDeleteConflictError) {
        setDeleteError(err.message);
        setDeleteBlockingProjects(err.referencingProjects);
      } else {
        setDeleteError((err as Error).message);
      }
    } finally {
      setDeleteLoading(false);
      setDeletingSourceId(null);
    }
  }, [deleteSource, loadSources, selectedLibraryType]);

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------
  return (
    <div className="flex flex-col h-screen bg-[hsl(var(--background))] p-3 gap-2">
      <LibraryHeader
        selectedType={selectedLibraryType}
        onTypeChange={setSelectedLibraryType}
        onOpenProjectManager={() => setShowProjectManager(true)}
        onOpenPlanning={() => setShowPlanning(true)}
        onOpenPurge={handleOpenPurge}
      />

      <IndexJobsPanel onJobComplete={handleJobComplete} />
      <StartupJobsPanel
        jobs={startupJobs}
        onOpen={handleOpenStartupJob}
        onRetry={handleRetryStartup}
      />

      <SearchBar
        searchQuery={searchQuery}
        onSearchChange={setSearchQuery}
        onNewSource={() => setShowNewSource(true)}
      />

      <SourceList
        sources={sources}
        selectedSource={selectedSource}
        deletingSourceId={deletingSourceId}
        onSelectSource={setSelectedSource}
        onToggleProtection={handleToggleProtection}
        onRenameSource={handleOpenRenameSource}
        onUpdateSource={(source) => {
          setUpdateSourceName(source.name);
          setShowFolderBrowser(true);
        }}
        onManageTorrents={(source) => setEpisodeSource(source)}
        onDeleteSource={handleOpenDeleteSource}
        searchQuery={searchQuery}
      />

      {error && (
        <div className="text-sm text-[hsl(var(--destructive))] px-2">
          {error}
        </div>
      )}

      <BottomBar
        tiktokUrl={tiktokUrl}
        onUrlChange={setTiktokUrl}
        onStart={handleStart}
        disabled={!tiktokUrl.trim() || !selectedSource || processing}
        loading={processing}
        statusText={statusText}
      />

      {/* Modals */}
      <ProjectManagerModal
        open={showProjectManager}
        onClose={() => setShowProjectManager(false)}
      />

      <PlanningModal
        open={showPlanning}
        onClose={() => setShowPlanning(false)}
      />

      <NewSourceModal
        open={showNewSource}
        onClose={() => setShowNewSource(false)}
        onSubmit={handleNewSourceSubmit}
        onBatchSubmit={handleBatchSourceSubmit}
        currentLibraryType={selectedLibraryType}
      />

      <PurgeModal
        open={showPurge}
        onClose={() => setShowPurge(false)}
        onConfirm={handlePurgeConfirm}
        currentLibraryType={selectedLibraryType}
        estimatedBytes={purgeEstimatedBytes}
        sourceCount={purgeSourceCount}
      />

      <DeleteSourceModal
        open={!!deleteSource}
        source={deleteSource}
        loading={deleteLoading}
        error={deleteError}
        blockingProjects={deleteBlockingProjects}
        onClose={handleCloseDeleteSource}
        onConfirm={handleDeleteSource}
      />

      <RenameSourceModal
        open={!!renameSource}
        source={renameSource}
        loading={renameLoading}
        error={renameError}
        onClose={handleCloseRenameSource}
        onSubmit={handleRenameSource}
      />

      <FolderBrowserModal
        open={showFolderBrowser}
        onClose={() => {
          setShowFolderBrowser(false);
          setUpdateSourceName(null);
        }}
        onSelect={(path) => {
          setShowFolderBrowser(false);
          if (updateSourceName) {
            api
              .updateAnimeAsync(path, selectedLibraryType, updateSourceName)
              .then(() => loadSources())
              .catch((err) => setError((err as Error).message));
            setUpdateSourceName(null);
          }
        }}
        initialPath={undefined}
      />

      <DuplicateTikTokWarning
        open={!!duplicateWarning}
        videoId={duplicateWarning?.videoId ?? ""}
        registeredAt={duplicateWarning?.registeredAt ?? null}
        onCancel={() => setDuplicateWarning(null)}
        onContinue={() => {
          setDuplicateWarning(null);
          void proceedWithStart(openStartupWindow());
        }}
      />

      {episodeSource && (
        <TorrentManagementModal
          open={!!episodeSource}
          onClose={() => setEpisodeSource(null)}
          sourceName={episodeSource.name}
          seriesId={episodeSource.series_id}
          libraryType={selectedLibraryType}
          onComplete={loadSources}
          onSourcesChanged={loadSources}
        />
      )}
    </div>
  );
}
