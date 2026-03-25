import { useState, useEffect, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import {
  LibraryHeader,
  IndexJobsPanel,
  SourceList,
  SearchBar,
  BottomBar,
  NewSourceModal,
  PurgeModal,
  TorrentManagementModal,
} from "@/components/library";
import { FolderBrowserModal } from "@/components/FolderBrowserModal";
import { ProjectManagerModal } from "@/components/project-manager";
import { DuplicateTikTokWarning } from "@/components/DuplicateTikTokWarning";
import { api } from "@/api/client";
import { readSSEStream } from "@/utils/sse";
import type { LibraryType, SourceDetails, IndexationJob } from "@/types";

export function ProjectSetup() {
  const navigate = useNavigate();

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

  // Modals
  const [showProjectManager, setShowProjectManager] = useState(false);
  const [showPurge, setShowPurge] = useState(false);
  const [showNewSource, setShowNewSource] = useState(false);
  const [showFolderBrowser, setShowFolderBrowser] = useState(false);
  const [updateSourceName, setUpdateSourceName] = useState<string | null>(null);
  const [torrentSourceName, setTorrentSourceName] = useState<string | null>(null);

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
  // Start flow: check duplicate -> create project -> download -> detect -> nav
  // ---------------------------------------------------------------------------
  const proceedWithStart = useCallback(async () => {
    if (!tiktokUrl.trim() || !selectedSource) return;
    setProcessing(true);
    setError(null);

    try {
      const selectedSourceDetails = sources.find(
        (source) => source.series_id === selectedSource,
      );
      if (!selectedSourceDetails) {
        throw new Error("Selected source not found");
      }

      // Create project
      setStatusText("Creating project...");
      const project = await api.createProject(
        tiktokUrl,
        undefined,
        selectedSourceDetails.name,
        selectedSourceDetails.series_id,
        selectedLibraryType,
      );
      const activationPromise = api.activateProjectLibrary(project.id).catch(
        (activationError) => {
          console.error("Failed to activate library:", activationError);
          throw activationError;
        },
      );

      // Download video
      setStatusText("Downloading video...");
      const downloadResp = await api.downloadVideo(project.id, tiktokUrl);
      await readSSEStream<{ status?: string; error?: string | null; message?: string | null; progress?: number }>(downloadResp, (data) => {
        if (data.progress !== undefined) {
          setStatusText(`Downloading... ${Math.round(data.progress)}%`);
        }
      });

      // Detect scenes
      setStatusText("Detecting scenes...");
      const detectResp = await api.detectScenes(project.id);
      await readSSEStream<{ status?: string; error?: string | null; message?: string | null; progress?: number }>(detectResp, (data) => {
        if (data.progress !== undefined) {
          setStatusText(
            `Detecting scenes... ${Math.round(data.progress * 100)}%`,
          );
        }
      });

      // Check whether to skip the scenes UI
      let skipScenesUi = false;
      try {
        const scenesConfig = await api.getScenesConfig(project.id);
        skipScenesUi = Boolean(scenesConfig.skip_ui_enabled);
      } catch {
        skipScenesUi = false;
      }

      if (skipScenesUi) {
        setStatusText("Preparing library...");
        await activationPromise;
      } else {
        void activationPromise;
      }

      navigate(
        skipScenesUi
          ? `/project/${project.id}/matches`
          : `/project/${project.id}/scenes`,
      );
    } catch (err) {
      setError((err as Error).message);
      setProcessing(false);
    }
  }, [tiktokUrl, selectedSource, selectedLibraryType, navigate, sources]);

  const handleStart = useCallback(async () => {
    if (!tiktokUrl.trim() || !selectedSource) return;
    setError(null);

    try {
      const result = await api.checkTiktokUrl(tiktokUrl);
      if (result.exists && result.video_id) {
        setDuplicateWarning({
          videoId: result.video_id,
          registeredAt: result.registered_at,
        });
        return;
      }
    } catch {
      // If check fails, proceed anyway
    }

    proceedWithStart();
  }, [tiktokUrl, selectedSource, proceedWithStart]);

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
      items: Array<{ path: string; name: string }>,
      type: LibraryType,
      fps: number,
    ) => {
      try {
        for (const item of items) {
          await api.indexAnimeAsync(item.path, type, item.name, fps);
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

      <SearchBar
        searchQuery={searchQuery}
        onSearchChange={setSearchQuery}
        onNewSource={() => setShowNewSource(true)}
      />

      <SourceList
        sources={sources}
        selectedSource={selectedSource}
        onSelectSource={setSelectedSource}
        onToggleProtection={handleToggleProtection}
        onUpdateSource={(source) => {
          setUpdateSourceName(source.name);
          setShowFolderBrowser(true);
        }}
        onManageTorrents={(source) => setTorrentSourceName(source.name)}
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
              .indexAnime(path, selectedLibraryType, updateSourceName)
              .then((resp) => readSSEStream(resp, () => {}))
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
          proceedWithStart();
        }}
      />

      {torrentSourceName && (
        <TorrentManagementModal
          open={!!torrentSourceName}
          onClose={() => setTorrentSourceName(null)}
          sourceName={torrentSourceName}
          libraryType={selectedLibraryType}
          onComplete={loadSources}
        />
      )}
    </div>
  );
}
