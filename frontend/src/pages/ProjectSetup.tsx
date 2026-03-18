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
} from "@/components/library";
import { FolderBrowserModal } from "@/components/FolderBrowserModal";
import { ProjectManagerModal } from "@/components/project-manager";
import { api } from "@/api/client";
import { readSSEStream } from "@/utils/sse";
import type { LibraryType, SourceDetails } from "@/types";

export function ProjectSetup() {
  const navigate = useNavigate();

  // Library state
  const [selectedLibraryType, setSelectedLibraryType] = useState<LibraryType>(
    () => (localStorage.getItem("libraryType") as LibraryType) || "anime",
  );
  const [sources, setSources] = useState<SourceDetails[]>([]);
  const [selectedSource, setSelectedSource] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [loadingSources, setLoadingSources] = useState(true);

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

  // Purge estimate
  const [purgeEstimatedBytes, setPurgeEstimatedBytes] = useState(0);
  const [purgeSourceCount, setPurgeSourceCount] = useState(0);

  // ---------------------------------------------------------------------------
  // Load sources
  // ---------------------------------------------------------------------------
  const loadSources = useCallback(async () => {
    setLoadingSources(true);
    try {
      const details = await api.getSourceDetails(selectedLibraryType);
      setSources(details);
    } catch (err) {
      console.error("Failed to load sources:", err);
      setSources([]);
    } finally {
      setLoadingSources(false);
    }
  }, [selectedLibraryType]);

  useEffect(() => {
    void loadSources();
  }, [loadSources]);

  // Persist library type to localStorage
  useEffect(() => {
    localStorage.setItem("libraryType", selectedLibraryType);
  }, [selectedLibraryType]);

  // ---------------------------------------------------------------------------
  // Start flow: create project -> download -> detect scenes -> navigate
  // ---------------------------------------------------------------------------
  const handleStart = useCallback(async () => {
    if (!tiktokUrl.trim() || !selectedSource) return;
    setProcessing(true);
    setError(null);

    try {
      // Create project
      setStatusText("Creating project...");
      const project = await api.createProject(
        tiktokUrl,
        undefined,
        selectedSource,
        selectedLibraryType,
      );

      // Download video
      setStatusText("Downloading video...");
      const downloadResp = await api.downloadVideo(project.id, tiktokUrl);
      await readSSEStream(downloadResp, (data: { progress?: number }) => {
        if (data.progress !== undefined) {
          setStatusText(`Downloading... ${Math.round(data.progress)}%`);
        }
      });

      // Detect scenes
      setStatusText("Detecting scenes...");
      const detectResp = await api.detectScenes(project.id);
      await readSSEStream(detectResp, (data: { progress?: number }) => {
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

      navigate(
        skipScenesUi
          ? `/project/${project.id}/matches`
          : `/project/${project.id}/scenes`,
      );
    } catch (err) {
      setError((err as Error).message);
      setProcessing(false);
    }
  }, [tiktokUrl, selectedSource, selectedLibraryType, navigate]);

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
    [loadSources],
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
    async (name: string) => {
      try {
        const result = await api.togglePurgeProtection(
          selectedLibraryType,
          name,
        );
        setSources((prev) =>
          prev.map((s) =>
            s.name === name
              ? { ...s, purge_protected: result.purge_protected }
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

      <IndexJobsPanel onJobComplete={loadSources} />

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
        onUpdateSource={(name) => {
          setUpdateSourceName(name);
          setShowFolderBrowser(true);
        }}
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
            // Handle update: call indexAnime for update
            // For now use sync index (Phase 2 will use async)
            api
              .indexAnime(path, selectedLibraryType, updateSourceName)
              .then((resp) => {
                readSSEStream(resp, () => {}).then(() => loadSources());
              });
            setUpdateSourceName(null);
          }
        }}
        initialPath={
          updateSourceName
            ? (sources.find((s) => s.name === updateSourceName)
                ?.original_index_path ?? undefined)
            : undefined
        }
      />
    </div>
  );
}
