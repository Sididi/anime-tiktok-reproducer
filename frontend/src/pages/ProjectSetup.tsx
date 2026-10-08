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
import { DuplicateTikTokWarning } from "@/components/DuplicateTikTokWarning";
import { api, SeriesDeleteConflictError } from "@/api/client";
import { getEventHub } from "@/utils/eventHub";
import type {
  LibraryType,
  SourceDetails,
  IndexationJob,
  ProjectStartupJob,
  SeriesDeleteReferencingProject,
  TemporarySourceInput,
} from "@/types";

/** Visible-tab fallback poll of the startup-job registry (the live stream is
 * the primary channel; this only bounds how long a wedged pipe can lie). */
const STARTUP_JOBS_POLL_MS = 60_000;

export function ProjectSetup() {
  // Library state
  const [selectedLibraryType, setSelectedLibraryType] = useState<LibraryType>(
    () => (localStorage.getItem("libraryType") as LibraryType) || "anime",
  );
  const [sources, setSources] = useState<SourceDetails[]>([]);
  const [selectedSource, setSelectedSource] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  // Pure mode reproduces one of our own tiktoks from its output: no series.
  const isPure = selectedLibraryType === "pure";
  const [temporarySourceKind, setTemporarySourceKind] = useState<"url" | "folder">("url");
  const [temporarySourceValue, setTemporarySourceValue] = useState("");
  const [temporarySourceFps, setTemporarySourceFps] = useState<1 | 2 | 4>(2);
  const [showTemporaryFolderBrowser, setShowTemporaryFolderBrowser] = useState(false);

  // TikTok URL + Start flow
  const [tiktokUrl, setTiktokUrl] = useState("");
  const [processing, setProcessing] = useState(false);
  const [statusText, setStatusText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [startupJobs, setStartupJobs] = useState<ProjectStartupJob[]>([]);
  // Mirror of `startupJobs` keyed by project, used to reject stale updates
  // (e.g. the POST response racing a fresher stream event) without making
  // every consumer depend on the state value.
  const startupJobsRef = useRef<Map<string, ProjectStartupJob>>(new Map());
  const startupTabsRef = useRef<Map<string, Window | null>>(new Map());
  const sourceLoadSeqRef = useRef(0);

  // Modals
  const [showProjectManager, setShowProjectManager] = useState(false);
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
  const loadSources = useCallback(async (options?: { clearOnError?: boolean }) => {
    const requestSeq = sourceLoadSeqRef.current + 1;
    sourceLoadSeqRef.current = requestSeq;
    if (isPure) {
      // Pure mode lists no series (the TikTok is the only source), so the
      // fetch has nothing to return and the source list stays hidden.
      setSources([]);
      return null;
    }
    try {
      const details = await api.getSourceDetails(selectedLibraryType);
      if (requestSeq !== sourceLoadSeqRef.current) {
        return null;
      }
      setSources(details);
      return details;
    } catch (err) {
      console.error("Failed to load sources:", err);
      if (options?.clearOnError !== false && requestSeq === sourceLoadSeqRef.current) {
        setSources([]);
      }
      return null;
    }
  }, [isPure, selectedLibraryType]);

  useEffect(() => {
    void loadSources();
  }, [loadSources]);

  const reloadSources = useCallback(async () => {
    await loadSources();
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

  const syncStartupPopup = useCallback(
    (job: ProjectStartupJob) => {
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

  /** Merge incoming job states, dropping anything older than what we already
   * show (the POST response and the stream race; either side may be stale).
   * A snapshot additionally removes jobs the backend no longer knows, except
   * very recent local-only entries whose stream event hasn't arrived yet. */
  const applyStartupJobs = useCallback(
    (
      incoming: ProjectStartupJob[],
      mode: "upsert" | "snapshot",
    ): ProjectStartupJob[] => {
      const map = startupJobsRef.current;
      const fresh: ProjectStartupJob[] = [];
      for (const job of incoming) {
        const existing = map.get(job.project_id);
        if (
          existing &&
          String(existing.updated_at).localeCompare(String(job.updated_at)) > 0
        ) {
          continue;
        }
        map.set(job.project_id, job);
        fresh.push(job);
      }
      if (mode === "snapshot") {
        const present = new Set(incoming.map((job) => job.project_id));
        const cutoff = Date.now() - 30_000;
        for (const [projectId, job] of Array.from(map)) {
          if (present.has(projectId)) continue;
          if (new Date(String(job.updated_at)).getTime() >= cutoff) continue;
          map.delete(projectId);
        }
      }
      setStartupJobs(Array.from(map.values()));
      return fresh;
    },
    [],
  );

  const handleStartupJobsUpdate = useCallback(
    (incoming: ProjectStartupJob[], mode: "upsert" | "snapshot") => {
      for (const job of applyStartupJobs(incoming, mode)) {
        syncStartupPopup(job);
      }
    },
    [applyStartupJobs, syncStartupPopup],
  );

  const upsertStartupJob = useCallback(
    (job: ProjectStartupJob) => handleStartupJobsUpdate([job], "upsert"),
    [handleStartupJobsUpdate],
  );

  useEffect(
    () =>
      // The shared event stream replays the current snapshot on subscribe
      // and then every update; each item is the job's full state.
      getEventHub().subscribe<ProjectStartupJob>("startup_jobs", {}, (event) => {
        if (event.kind === "snapshot") {
          handleStartupJobsUpdate(
            event.items.map((item) => item.data),
            "snapshot",
          );
        } else {
          handleStartupJobsUpdate([event.item.data], "upsert");
        }
      }),
    [handleStartupJobsUpdate],
  );

  // Safety net for the live stream: even with its staleness watchdog a dead
  // pipe can go unnoticed (worker killed mid-frame, dropped port...), and a
  // job then looks "queued" forever while the backend long since finished.
  // A slow visible-tab poll guarantees the panel converges regardless.
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.visibilityState !== "visible") return;
      void api
        .listProjectStartupJobs()
        .then(({ jobs }) => handleStartupJobsUpdate(jobs, "snapshot"))
        .catch(() => {
          // Transient fetch failure: the next tick retries.
        });
    }, STARTUP_JOBS_POLL_MS);
    return () => window.clearInterval(timer);
  }, [handleStartupJobsUpdate]);

  const proceedWithStart = useCallback(async (popup: Window | null) => {
    if (!tiktokUrl.trim() || (!isPure && !selectedSource)) return;
    setProcessing(true);
    setError(null);
    setStatusText("Launching...");

    try {
      let animeName: string | undefined;
      let seriesId: string | undefined;
      if (!isPure) {
        const latestSources = await loadSources({ clearOnError: false });
        const availableSources = latestSources ?? sources;
        const selectedSourceDetails = availableSources.find(
          (source) => source.series_id === selectedSource,
        );
        if (!selectedSourceDetails) {
          throw new Error("Selected source not found");
        }
        animeName = selectedSourceDetails.name;
        seriesId = selectedSourceDetails.series_id;
      }

      const job = await api.startProjectAsync(
        tiktokUrl,
        animeName,
        seriesId,
        selectedLibraryType,
        isPure && temporarySourceValue.trim()
          ? {kind: temporarySourceKind, value: temporarySourceValue.trim(), fps: temporarySourceFps} satisfies TemporarySourceInput
          : undefined,
      );
      if (popup) {
        startupTabsRef.current.set(job.project_id, popup);
      }
      upsertStartupJob(job);
      setTiktokUrl("");
      setTemporarySourceValue("");
    } catch (err) {
      if (popup && !popup.closed) {
        popup.close();
      }
      setError((err as Error).message);
    } finally {
      setProcessing(false);
      setStatusText("");
    }
  }, [
    tiktokUrl,
    selectedSource,
    selectedLibraryType,
    isPure,
    temporarySourceKind,
    temporarySourceValue,
    temporarySourceFps,
    sources,
    loadSources,
    upsertStartupJob,
  ]);

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
    if (!tiktokUrl.trim() || (!isPure && !selectedSource)) return;
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
  }, [tiktokUrl, selectedSource, isPure, openStartupWindow, proceedWithStart]);

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
        onOpenPurge={handleOpenPurge}
      />

      <IndexJobsPanel onJobComplete={handleJobComplete} />
      <StartupJobsPanel
        jobs={startupJobs}
        onOpen={handleOpenStartupJob}
        onRetry={handleRetryStartup}
      />

      {!isPure && (
        <SearchBar
          searchQuery={searchQuery}
          onSearchChange={setSearchQuery}
          onNewSource={() => setShowNewSource(true)}
        />
      )}

      {!isPure && (
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
      )}

      {isPure && (
        <div className="flex-1 flex items-center justify-center overflow-y-auto px-4 py-6">
          <div className="w-full max-w-xl space-y-4 rounded-xl border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-6">
            <div>
              <h2 className="font-semibold">Mode Pure</h2>
              <p className="mt-1 text-sm text-[hsl(var(--muted-foreground))]">Reproduis un TikTok à partir de sa vidéo, ou retrouve ses plans dans une source originale.</p>
            </div>
            <label htmlFor="temporary-source" className="block text-sm font-medium">Source originale <span className="text-[hsl(var(--muted-foreground))] font-normal">— facultative</span></label>
            <div className="flex gap-2">
              {(["url", "folder"] as const).map(kind => (
                <button key={kind} type="button" disabled={processing} aria-pressed={temporarySourceKind === kind}
                  onClick={() => { setTemporarySourceKind(kind); setTemporarySourceValue(""); }}
                  className={`rounded-md px-3 py-2 text-sm ${temporarySourceKind === kind ? "bg-[hsl(var(--primary))] text-white" : "bg-[hsl(var(--secondary))]"}`}>
                  {kind === "url" ? "Lien vidéo" : "Dossier local"}
                </button>
              ))}
            </div>
            <div className="flex gap-2">
              <input id="temporary-source" type={temporarySourceKind === "url" ? "url" : "text"} value={temporarySourceValue}
                onChange={event => setTemporarySourceValue(event.target.value)} disabled={processing}
                placeholder={temporarySourceKind === "url" ? "https://www.youtube.com/watch?v=…" : "/chemin/vers/les/videos"}
                className="min-w-0 flex-1 rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--background))] px-3 py-2 text-sm" />
              {temporarySourceKind === "folder" && <button type="button" disabled={processing} onClick={() => setShowTemporaryFolderBrowser(true)} className="rounded-md bg-[hsl(var(--secondary))] px-3 py-2 text-sm">Parcourir</button>}
              {temporarySourceValue && <button type="button" disabled={processing} onClick={() => setTemporarySourceValue("")} className="text-sm text-[hsl(var(--muted-foreground))]">Retirer</button>}
            </div>
            <p className="text-xs leading-relaxed text-[hsl(var(--muted-foreground))]">
              {temporarySourceValue.trim()
                ? "La source sera copiée et indexée pour ce projet. Le projet s’ouvrira une fois prêt, avec le matching des scènes originales et sans nettoyage du TikTok. La copie locale sera supprimée avec le projet ; tes fichiers d’origine seront conservés."
                : "Laisse ce champ vide pour utiliser le nettoyage et le parcours Pure classique."}
              {temporarySourceKind === "folder" && " Seules les vidéos directement dans le dossier sont incluses."}
              {temporarySourceKind === "url" && " Liens vidéo : 1080p maximum. Les vidéos YouTube doivent être accessibles sans connexion."}
            </p>
            {temporarySourceValue.trim() && <details className="text-sm">
              <summary className="cursor-pointer text-[hsl(var(--muted-foreground))]">Réglages avancés</summary>
              <label className="mt-3 flex items-center justify-between gap-3">Densité d’indexation
                <select value={temporarySourceFps} disabled={processing} onChange={event => setTemporarySourceFps(Number(event.target.value) as 1 | 2 | 4)} className="rounded-md bg-[hsl(var(--secondary))] px-3 py-2">
                  {[1, 2, 4].map(fps => <option key={fps} value={fps}>{fps} image{fps > 1 ? "s" : ""}/seconde</option>)}
                </select>
              </label>
            </details>}
          </div>
        </div>
      )}

      {error && (
        <div className="text-sm text-[hsl(var(--destructive))] px-2">
          {error}
        </div>
      )}

      <BottomBar
        tiktokUrl={tiktokUrl}
        onUrlChange={setTiktokUrl}
        onStart={handleStart}
        disabled={!tiktokUrl.trim() || (!isPure && !selectedSource) || processing}
        loading={processing}
        statusText={statusText}
      />

      {/* Modals */}
      <FolderBrowserModal open={showTemporaryFolderBrowser} onClose={() => setShowTemporaryFolderBrowser(false)}
        initialPath={temporarySourceKind === "folder" ? temporarySourceValue || undefined : undefined}
        onSelect={path => {setTemporarySourceKind("folder"); setTemporarySourceValue(path); setShowTemporaryFolderBrowser(false);}} />
      <ProjectManagerModal
        open={showProjectManager}
        onClose={() => setShowProjectManager(false)}
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
          onComplete={reloadSources}
          onSourcesChanged={reloadSources}
        />
      )}
    </div>
  );
}
