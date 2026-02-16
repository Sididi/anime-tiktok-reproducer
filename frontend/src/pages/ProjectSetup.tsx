import { useState, useCallback, useEffect, useMemo, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { Loader2, ChevronDown, Plus, FolderOpen, FolderPlus, FolderKanban } from "lucide-react";
import { Button, Input } from "@/components/ui";
import { FolderBrowserModal } from "@/components/FolderBrowserModal";
import { ProjectManagerModal } from "@/components/ProjectManagerModal";
import { useProjectStore } from "@/stores";
import { api } from "@/api/client";
import { readSSEStream } from "@/utils/sse";

interface DownloadProgress {
  status: string;
  progress: number;
  message: string;
  error: string | null;
}

interface IndexProgress {
  status: string;
  phase: string;
  progress: number;
  message: string;
  error: string | null;
}

interface DetectionProgress {
  status: string;
  progress: number;
  message: string;
  scenes?: import("@/types").Scene[];
  error: string | null;
}

const sortAnimeNames = (series: string[]) =>
  [...series].sort((a, b) =>
    a.localeCompare(b, undefined, { sensitivity: "base", numeric: true }),
  );

export function ProjectSetup() {
  const navigate = useNavigate();
  const {
    createProject,
    loading: creatingProject,
    error: createError,
  } = useProjectStore();

  // Form state
  const [tiktokUrl, setTiktokUrl] = useState("");
  const [selectedAnime, setSelectedAnime] = useState<string | null>(null);
  const [showAnimeDropdown, setShowAnimeDropdown] = useState(false);
  const [animeSearch, setAnimeSearch] = useState("");
  const [indexNewMode, setIndexNewMode] = useState(false);
  const [newAnimePath, setNewAnimePath] = useState("");
  const [newAnimeName, setNewAnimeName] = useState("");
  const [newAnimeFps, setNewAnimeFps] = useState(2);
  const [updateAnimeName, setUpdateAnimeName] = useState<string | null>(null);

  // Anime list state
  const [indexedAnime, setIndexedAnime] = useState<string[]>([]);
  const [loadingAnime, setLoadingAnime] = useState(true);

  // Progress state
  const [downloadProgress, setDownloadProgress] =
    useState<DownloadProgress | null>(null);
  const [indexProgress, setIndexProgress] = useState<IndexProgress | null>(
    null,
  );
  const [detectionProgress, setDetectionProgress] =
    useState<DetectionProgress | null>(null);
  const [downloading, setDownloading] = useState(false);
  const [indexing, setIndexing] = useState(false);
  const [detecting, setDetecting] = useState(false);
  const [showFolderBrowser, setShowFolderBrowser] = useState(false);
  const [showProjectManager, setShowProjectManager] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

  // Load indexed anime on mount
  useEffect(() => {
    async function loadAnime() {
      try {
        const result = await api.listIndexedAnime();
        setIndexedAnime(sortAnimeNames(result.series));
      } catch (err) {
        console.error("Failed to load indexed anime:", err);
      } finally {
        setLoadingAnime(false);
      }
    }
    loadAnime();
  }, []);

  // Filter anime based on search
  const filteredAnime = useMemo(() => {
    if (!animeSearch.trim()) return indexedAnime;
    const search = animeSearch.toLowerCase();
    return indexedAnime.filter((a) => a.toLowerCase().includes(search));
  }, [indexedAnime, animeSearch]);

  useEffect(() => {
    if (!showAnimeDropdown) return;
    const handleClickOutside = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setShowAnimeDropdown(false);
      }
    };
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, [showAnimeDropdown]);

  // Run scene detection after download
  const handleSceneDetection = useCallback(
    async (projectId: string): Promise<boolean> => {
      setDetecting(true);
      setDetectionProgress({
        status: "starting",
        progress: 0,
        message: "Starting scene detection...",
        error: null,
      });

      try {
        const response = await api.detectScenes(projectId);

        await readSSEStream<DetectionProgress>(response, (data) => {
          setDetectionProgress(data);
        });
        return true;
      } catch (err) {
        setDetectionProgress({
          status: "error",
          progress: 0,
          message: "",
          error: (err as Error).message,
        });
        return false;
      } finally {
        setDetecting(false);
      }
    },
    [],
  );

  const handleDownload = useCallback(
    async (projectId: string, url: string): Promise<boolean> => {
      setDownloading(true);
      setDownloadProgress({
        status: "starting",
        progress: 0,
        message: "Starting download...",
        error: null,
      });

      try {
        const response = await api.downloadVideo(projectId, url);

        await readSSEStream<DownloadProgress>(response, (data) => {
          setDownloadProgress(data);
          if (data.status === "complete") {
            // handled by return below
          }
        });
        return true;
      } catch (err) {
        setDownloadProgress({
          status: "error",
          progress: 0,
          message: "",
          error: (err as Error).message,
        });
        return false;
      } finally {
        setDownloading(false);
      }
    },
    [],
  );

  const handleIndexAnime = useCallback(async (overrideName?: string, overrideFps?: number): Promise<boolean> => {
    if (!newAnimePath.trim()) return false;

    setIndexing(true);
    setIndexProgress({
      status: "starting",
      phase: "starting",
      progress: 0,
      message: "Starting indexing...",
      error: null,
    });

    try {
      const animeName = overrideName || newAnimeName.trim() || undefined;
      const selectedFps = overrideFps ?? newAnimeFps;
      const response = await api.indexAnime(newAnimePath, animeName, selectedFps);

      let finalAnimeName: string | null = null;
      await readSSEStream<IndexProgress & { anime_name?: string }>(response, async (data) => {
        setIndexProgress(data);
        if (data.status === "complete") {
          finalAnimeName = data.anime_name || animeName || newAnimePath.split("/").pop() || null;
          const result = await api.listIndexedAnime();
          setIndexedAnime(sortAnimeNames(result.series));
        }
      });

      if (finalAnimeName) {
        setSelectedAnime(finalAnimeName);
        setIndexNewMode(false);
        setUpdateAnimeName(null);
        setNewAnimePath("");
        setNewAnimeName("");
        setNewAnimeFps(2);
        setIndexProgress(null);
        return true;
      }

      return false;
    } catch (err) {
      setIndexProgress({
        status: "error",
        phase: "error",
        progress: 0,
        message: "",
        error: (err as Error).message,
      });
      return false;
    } finally {
      setIndexing(false);
    }
  }, [newAnimePath, newAnimeName, newAnimeFps]);

  const handleUpdateAnime = async () => {
    if (!updateAnimeName || !newAnimePath.trim()) return;
    await handleIndexAnime(updateAnimeName, 2);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    // Validate
    if (!tiktokUrl.trim()) return;

    // If indexing new anime, do that first
    if (indexNewMode) {
      const success = await handleIndexAnime();
      if (!success) return;
    }

    // Need anime selected
    const animeName = indexNewMode
      ? newAnimeName.trim() || newAnimePath.split("/").pop() || null
      : selectedAnime;

    if (!animeName) return;

    try {
      const project = await createProject(tiktokUrl, undefined, animeName);

      // Step 1: Download video
      const downloadSuccess = await handleDownload(project.id, tiktokUrl);
      if (!downloadSuccess) return;

      // Step 2: Run scene detection
      const detectionSuccess = await handleSceneDetection(project.id);
      if (!detectionSuccess) return;

      // Step 3: Navigate to scene validation (now with video and scenes ready)
      navigate(`/project/${project.id}/scenes`);
    } catch {
      // Error is handled in store
    }
  };

  const selectAnime = (anime: string) => {
    setSelectedAnime(anime);
    setShowAnimeDropdown(false);
    setAnimeSearch("");
    setIndexNewMode(false);
  };

  const startIndexNew = () => {
    setIndexNewMode(true);
    setUpdateAnimeName(null);
    setShowAnimeDropdown(false);
    setSelectedAnime(null);
    setNewAnimeFps(2);
  };

  const startUpdateAnime = (anime: string) => {
    setUpdateAnimeName(anime);
    setIndexNewMode(false);
    setShowAnimeDropdown(false);
    setNewAnimePath("");
    setIndexProgress(null);
  };

  const isLoading = creatingProject || downloading || indexing || detecting;
  const error =
    createError ||
    downloadProgress?.error ||
    indexProgress?.error ||
    detectionProgress?.error;
  const canSubmit =
    tiktokUrl.trim() &&
    (selectedAnime || (indexNewMode && newAnimePath.trim()));

  return (
    <div className="min-h-screen flex items-center justify-center p-4">
      <div className="w-full max-w-md space-y-6">
        <div className="text-center">
          <h1 className="text-2xl font-bold">Anime TikTok Reproducer</h1>
          <p className="text-[hsl(var(--muted-foreground))] mt-2">
            Remaster your TikToks for other platforms
          </p>
          <div className="mt-4">
            <Button
              type="button"
              variant="outline"
              onClick={() => setShowProjectManager(true)}
            >
              <FolderKanban className="h-4 w-4 mr-2" />
              Open Project Manager
            </Button>
          </div>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          {/* TikTok URL */}
          <div>
            <label className="text-sm text-[hsl(var(--muted-foreground))] mb-1 block">
              TikTok URL
            </label>
            <Input
              type="url"
              placeholder="https://www.tiktok.com/@user/video/..."
              value={tiktokUrl}
              onChange={(e) => setTiktokUrl(e.target.value)}
              disabled={isLoading}
              data-testid="tiktok-url-input"
              required
            />
          </div>

          {/* Anime Selection */}
          <div>
            <label className="text-sm text-[hsl(var(--muted-foreground))] mb-1 block">
              Source Anime{" "}
              <span className="text-[hsl(var(--destructive))]">*</span>
            </label>

            {updateAnimeName ? (
              /* Update Episodes Mode */
              <div className="space-y-3 p-3 border border-[hsl(var(--border))] rounded-md bg-[hsl(var(--muted)/0.3)]">
                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium">Update episodes for {updateAnimeName}</span>
                  <button
                    type="button"
                    onClick={() => {
                      setUpdateAnimeName(null);
                      setNewAnimePath("");
                      setIndexProgress(null);
                    }}
                    className="text-xs text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]"
                  >
                    Cancel
                  </button>
                </div>

                <div>
                  <label className="text-xs text-[hsl(var(--muted-foreground))] mb-1 block">
                    Folder path with episodes (existing + new)
                  </label>
                  <div className="flex gap-2">
                    <Input
                      type="text"
                      placeholder="/path/to/anime/episodes"
                      value={newAnimePath}
                      onChange={(e) => setNewAnimePath(e.target.value)}
                      disabled={isLoading}
                      className="flex-1"
                    />
                    <Button
                      type="button"
                      variant="outline"
                      disabled={isLoading}
                      onClick={() => setShowFolderBrowser(true)}
                    >
                      <FolderOpen className="h-4 w-4" />
                    </Button>
                  </div>
                  <p className="text-xs text-[hsl(var(--muted-foreground))] mt-1">
                    Only new episodes will be copied and indexed
                  </p>
                </div>

                <Button
                  type="button"
                  className="w-full"
                  disabled={isLoading || !newAnimePath.trim()}
                  onClick={handleUpdateAnime}
                >
                  {indexing ? (
                    <>
                      <Loader2 className="h-4 w-4 animate-spin mr-2" />
                      Updating...
                    </>
                  ) : (
                    "Update Episodes"
                  )}
                </Button>

                {/* Indexing progress */}
                {indexProgress && indexProgress.status !== "error" && (
                  <div className="space-y-2">
                    <div className="flex items-center gap-2 text-xs text-[hsl(var(--muted-foreground))]">
                      <Loader2 className="h-3 w-3 animate-spin" />
                      <span>{indexProgress.message}</span>
                    </div>
                    <div className="h-1.5 bg-[hsl(var(--muted))] rounded-full overflow-hidden">
                      <div
                        className="h-full bg-[hsl(var(--primary))] transition-all duration-300"
                        style={{ width: `${indexProgress.progress * 100}%` }}
                      />
                    </div>
                  </div>
                )}

                {indexProgress?.error && (
                  <p className="text-xs text-[hsl(var(--destructive))]">{indexProgress.error}</p>
                )}
              </div>
            ) : !indexNewMode ? (
              <div className="relative" ref={dropdownRef}>
                <button
                  type="button"
                  onClick={() => setShowAnimeDropdown(!showAnimeDropdown)}
                  disabled={isLoading || loadingAnime}
                  className="w-full flex items-center justify-between px-3 py-2 border border-[hsl(var(--border))] rounded-md bg-[hsl(var(--background))] text-left disabled:opacity-50"
                >
                  {loadingAnime ? (
                    <span className="text-[hsl(var(--muted-foreground))]">
                      Loading anime...
                    </span>
                  ) : selectedAnime ? (
                    <span>{selectedAnime}</span>
                  ) : (
                    <span className="text-[hsl(var(--muted-foreground))]">
                      Select an anime...
                    </span>
                  )}
                  <ChevronDown className="h-4 w-4 text-[hsl(var(--muted-foreground))]" />
                </button>

                {showAnimeDropdown && (
                  <div className="absolute z-10 mt-1 w-full bg-[hsl(var(--background))] border border-[hsl(var(--border))] rounded-md shadow-lg flex max-h-[min(80vh,24rem)] flex-col overflow-hidden">
                    {/* Search input */}
                    <div className="p-2 border-b border-[hsl(var(--border))]">
                      <Input
                        type="text"
                        placeholder="Search anime..."
                        value={animeSearch}
                        onChange={(e) => setAnimeSearch(e.target.value)}
                        className="text-sm"
                        autoFocus
                      />
                    </div>

                    {/* Anime list */}
                    <div className="overflow-y-auto min-h-0 flex-1">
                      {filteredAnime.length === 0 ? (
                        <div className="px-3 py-2 text-sm text-[hsl(var(--muted-foreground))]">
                          {indexedAnime.length === 0
                            ? "No anime indexed yet"
                            : "No matches found"}
                        </div>
                      ) : (
                        filteredAnime.map((anime) => (
                          <div
                            key={anime}
                            className="flex items-center hover:bg-[hsl(var(--muted))]"
                          >
                            <button
                              type="button"
                              onClick={() => selectAnime(anime)}
                              className="flex-1 text-left px-3 py-2 text-sm"
                            >
                              {anime}
                            </button>
                            <button
                              type="button"
                              onClick={(e) => {
                                e.stopPropagation();
                                startUpdateAnime(anime);
                              }}
                              className="px-2 py-2 text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--primary))]"
                              title="Update episodes"
                            >
                              <FolderPlus className="h-3.5 w-3.5" />
                            </button>
                          </div>
                        ))
                      )}
                    </div>

                    {/* Index new option */}
                    <div className="shrink-0 border-t border-[hsl(var(--border))] p-2">
                      <button
                        type="button"
                        onClick={startIndexNew}
                        className="w-full flex items-center gap-2 px-3 py-2 rounded-md hover:bg-[hsl(var(--muted))] text-sm text-[hsl(var(--primary))]"
                      >
                        <Plus className="h-4 w-4" />
                        Index New Anime
                      </button>
                    </div>
                  </div>
                )}
              </div>
            ) : (
              /* Index New Mode */
              <div className="space-y-3 p-3 border border-[hsl(var(--border))] rounded-md bg-[hsl(var(--muted)/0.3)]">
                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium">Index New Anime</span>
                  <button
                    type="button"
                    onClick={() => {
                      setIndexNewMode(false);
                      setNewAnimeFps(2);
                    }}
                    className="text-xs text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]"
                  >
                    Cancel
                  </button>
                </div>

                <div>
                  <label className="text-xs text-[hsl(var(--muted-foreground))] mb-1 block">
                    Anime folder path (with episode video files)
                  </label>
                  <div className="flex gap-2">
                    <Input
                      type="text"
                      placeholder="/path/to/anime/episodes"
                      value={newAnimePath}
                      onChange={(e) => setNewAnimePath(e.target.value)}
                      disabled={isLoading}
                      className="flex-1"
                    />
                    <Button
                      type="button"
                      variant="outline"
                      disabled={isLoading}
                      onClick={() => setShowFolderBrowser(true)}
                    >
                      <FolderOpen className="h-4 w-4" />
                    </Button>
                  </div>
                  <p className="text-xs text-[hsl(var(--muted-foreground))] mt-1">
                    Note: Enter the full absolute path (e.g.,
                    /home/user/anime/MyAnime)
                  </p>
                </div>

                <div>
                  <label className="text-xs text-[hsl(var(--muted-foreground))] mb-1 block">
                    Anime name (optional, defaults to folder name)
                  </label>
                  <Input
                    type="text"
                    placeholder="e.g. Attack on Titan"
                    value={newAnimeName}
                    onChange={(e) => setNewAnimeName(e.target.value)}
                    disabled={isLoading}
                  />
                </div>

                <div>
                  <label className="text-xs text-[hsl(var(--muted-foreground))] mb-1 block">
                    Index FPS
                  </label>
                  <select
                    value={newAnimeFps}
                    onChange={(e) => setNewAnimeFps(Number(e.target.value))}
                    disabled={isLoading}
                    className="w-full rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--background))] px-3 py-2 text-sm"
                  >
                    <option value={1}>1 FPS</option>
                    <option value={2}>2 FPS</option>
                    <option value={4}>4 FPS</option>
                  </select>
                </div>

                {/* Indexing progress */}
                {indexProgress && indexProgress.status !== "error" && (
                  <div className="space-y-2">
                    <div className="flex items-center gap-2 text-xs text-[hsl(var(--muted-foreground))]">
                      <Loader2 className="h-3 w-3 animate-spin" />
                      <span>{indexProgress.message}</span>
                    </div>
                    <div className="h-1.5 bg-[hsl(var(--muted))] rounded-full overflow-hidden">
                      <div
                        className="h-full bg-[hsl(var(--primary))] transition-all duration-300"
                        style={{ width: `${indexProgress.progress * 100}%` }}
                      />
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Download progress */}
          {downloadProgress && downloadProgress.status !== "error" && (
            <div className="space-y-2">
              <div className="flex items-center gap-2 text-sm text-[hsl(var(--muted-foreground))]">
                <Loader2 className="h-4 w-4 animate-spin" />
                <span>{downloadProgress.message}</span>
              </div>
              <div className="h-2 bg-[hsl(var(--muted))] rounded-full overflow-hidden">
                <div
                  className="h-full bg-[hsl(var(--primary))] transition-all duration-300"
                  style={{ width: `${downloadProgress.progress * 100}%` }}
                />
              </div>
            </div>
          )}

          {/* Scene detection progress */}
          {detectionProgress && detectionProgress.status !== "error" && (
            <div className="space-y-2">
              <div className="flex items-center gap-2 text-sm text-[hsl(var(--muted-foreground))]">
                <Loader2 className="h-4 w-4 animate-spin" />
                <span>{detectionProgress.message}</span>
              </div>
              <div className="h-2 bg-[hsl(var(--muted))] rounded-full overflow-hidden">
                <div
                  className="h-full bg-[hsl(var(--primary))] transition-all duration-300"
                  style={{ width: `${detectionProgress.progress * 100}%` }}
                />
              </div>
            </div>
          )}

          {error && !updateAnimeName && (
            <p className="text-sm text-[hsl(var(--destructive))]">{error}</p>
          )}

          {!updateAnimeName && (
            <Button
              type="submit"
              className="w-full"
              disabled={isLoading || !canSubmit}
              data-testid="create-project-btn"
            >
              {isLoading ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin mr-2" />
                  {indexing
                    ? "Indexing..."
                    : downloading
                      ? "Downloading..."
                      : detecting
                        ? "Detecting scenes..."
                        : "Creating..."}
                </>
              ) : indexNewMode ? (
                "Index & Start"
              ) : (
                "Download & Start"
              )}
            </Button>
          )}
        </form>

        <p className="text-xs text-center text-[hsl(var(--muted-foreground))]">
          Enter a TikTok URL and select the anime to reproduce from
        </p>
      </div>

      <FolderBrowserModal
        open={showFolderBrowser}
        onClose={() => setShowFolderBrowser(false)}
        onSelect={(path) => setNewAnimePath(path)}
      />
      <ProjectManagerModal
        open={showProjectManager}
        onClose={() => setShowProjectManager(false)}
      />
    </div>
  );
}
