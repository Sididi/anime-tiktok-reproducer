import { useState, useCallback, useEffect, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { Loader2, ChevronDown, Plus, FolderOpen } from 'lucide-react';
import { Button, Input } from '@/components/ui';
import { useProjectStore } from '@/stores';
import { api } from '@/api/client';

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
  scenes?: import('@/types').Scene[];
  error: string | null;
}

export function ProjectSetup() {
  const navigate = useNavigate();
  const { createProject, loading: creatingProject, error: createError } = useProjectStore();
  
  // Form state
  const [tiktokUrl, setTiktokUrl] = useState('');
  const [selectedAnime, setSelectedAnime] = useState<string | null>(null);
  const [showAnimeDropdown, setShowAnimeDropdown] = useState(false);
  const [animeSearch, setAnimeSearch] = useState('');
  const [indexNewMode, setIndexNewMode] = useState(false);
  const [newAnimePath, setNewAnimePath] = useState('');
  const [newAnimeName, setNewAnimeName] = useState('');
  
  // Anime list state
  const [indexedAnime, setIndexedAnime] = useState<string[]>([]);
  const [loadingAnime, setLoadingAnime] = useState(true);
  
  // Progress state
  const [downloadProgress, setDownloadProgress] = useState<DownloadProgress | null>(null);
  const [indexProgress, setIndexProgress] = useState<IndexProgress | null>(null);
  const [detectionProgress, setDetectionProgress] = useState<DetectionProgress | null>(null);
  const [downloading, setDownloading] = useState(false);
  const [indexing, setIndexing] = useState(false);
  const [detecting, setDetecting] = useState(false);

  // Load indexed anime on mount
  useEffect(() => {
    async function loadAnime() {
      try {
        const result = await api.listIndexedAnime();
        setIndexedAnime(result.series);
      } catch (err) {
        console.error('Failed to load indexed anime:', err);
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
    return indexedAnime.filter(a => a.toLowerCase().includes(search));
  }, [indexedAnime, animeSearch]);

  // Run scene detection after download
  const handleSceneDetection = useCallback(async (projectId: string): Promise<boolean> => {
    setDetecting(true);
    setDetectionProgress({ status: 'starting', progress: 0, message: 'Starting scene detection...', error: null });

    try {
      const response = await api.detectScenes(projectId);

      if (!response.ok) {
        throw new Error('Failed to start scene detection');
      }

      const reader = response.body?.getReader();
      if (!reader) {
        throw new Error('No response body');
      }

      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          if (line.startsWith('data: ')) {
            try {
              const data = JSON.parse(line.slice(6)) as DetectionProgress;
              setDetectionProgress(data);

              if (data.status === 'complete') {
                return true;
              }

              if (data.status === 'error') {
                throw new Error(data.error || 'Scene detection failed');
              }
            } catch (e) {
              if (e instanceof SyntaxError) continue;
              throw e;
            }
          }
        }
      }
      return true;
    } catch (err) {
      setDetectionProgress({
        status: 'error',
        progress: 0,
        message: '',
        error: (err as Error).message,
      });
      return false;
    } finally {
      setDetecting(false);
    }
  }, []);

  const handleDownload = useCallback(async (projectId: string, url: string): Promise<boolean> => {
    setDownloading(true);
    setDownloadProgress({ status: 'starting', progress: 0, message: 'Starting download...', error: null });

    try {
      const response = await api.downloadVideo(projectId, url);

      if (!response.ok) {
        throw new Error('Failed to start download');
      }

      const reader = response.body?.getReader();
      if (!reader) {
        throw new Error('No response body');
      }

      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          if (line.startsWith('data: ')) {
            try {
              const data = JSON.parse(line.slice(6)) as DownloadProgress;
              setDownloadProgress(data);

              if (data.status === 'complete') {
                return true;
              }

              if (data.status === 'error') {
                throw new Error(data.error || 'Download failed');
              }
            } catch (e) {
              if (e instanceof SyntaxError) continue;
              throw e;
            }
          }
        }
      }
      return true;
    } catch (err) {
      setDownloadProgress({
        status: 'error',
        progress: 0,
        message: '',
        error: (err as Error).message,
      });
      return false;
    } finally {
      setDownloading(false);
    }
  }, []);

  const handleIndexAnime = useCallback(async (): Promise<boolean> => {
    if (!newAnimePath.trim()) return false;

    setIndexing(true);
    setIndexProgress({ status: 'starting', phase: 'starting', progress: 0, message: 'Starting indexing...', error: null });

    try {
      const animeName = newAnimeName.trim() || undefined;
      const response = await api.indexAnime(newAnimePath, animeName, 2.0);

      if (!response.ok) {
        throw new Error('Failed to start indexing');
      }

      const reader = response.body?.getReader();
      if (!reader) {
        throw new Error('No response body');
      }

      const decoder = new TextDecoder();
      let buffer = '';
      let finalAnimeName: string | null = null;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          if (line.startsWith('data: ')) {
            try {
              const data = JSON.parse(line.slice(6)) as IndexProgress & { anime_name?: string };
              setIndexProgress(data);

              if (data.status === 'complete') {
                finalAnimeName = data.anime_name || animeName || newAnimePath.split('/').pop() || null;
                // Reload anime list
                const result = await api.listIndexedAnime();
                setIndexedAnime(result.series);
              }

              if (data.status === 'error') {
                throw new Error(data.error || 'Indexing failed');
              }
            } catch (e) {
              if (e instanceof SyntaxError) continue;
              throw e;
            }
          }
        }
      }

      if (finalAnimeName) {
        setSelectedAnime(finalAnimeName);
        setIndexNewMode(false);
        setNewAnimePath('');
        setNewAnimeName('');
        setIndexProgress(null);
        return true;
      }

      return false;
    } catch (err) {
      setIndexProgress({
        status: 'error',
        phase: 'error',
        progress: 0,
        message: '',
        error: (err as Error).message,
      });
      return false;
    } finally {
      setIndexing(false);
    }
  }, [newAnimePath, newAnimeName]);

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
      ? (newAnimeName.trim() || newAnimePath.split('/').pop() || null)
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
    setAnimeSearch('');
    setIndexNewMode(false);
  };

  const startIndexNew = () => {
    setIndexNewMode(true);
    setShowAnimeDropdown(false);
    setSelectedAnime(null);
  };

  const isLoading = creatingProject || downloading || indexing || detecting;
  const error = createError || downloadProgress?.error || indexProgress?.error || detectionProgress?.error;
  const canSubmit = tiktokUrl.trim() && (selectedAnime || (indexNewMode && newAnimePath.trim()));

  return (
    <div className="min-h-screen flex items-center justify-center p-4">
      <div className="w-full max-w-md space-y-6">
        <div className="text-center">
          <h1 className="text-2xl font-bold">Anime TikTok Reproducer</h1>
          <p className="text-[hsl(var(--muted-foreground))] mt-2">
            Remaster your TikToks for other platforms
          </p>
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
              Source Anime <span className="text-[hsl(var(--destructive))]">*</span>
            </label>
            
            {!indexNewMode ? (
              <div className="relative">
                <button
                  type="button"
                  onClick={() => setShowAnimeDropdown(!showAnimeDropdown)}
                  disabled={isLoading || loadingAnime}
                  className="w-full flex items-center justify-between px-3 py-2 border border-[hsl(var(--border))] rounded-md bg-[hsl(var(--background))] text-left disabled:opacity-50"
                >
                  {loadingAnime ? (
                    <span className="text-[hsl(var(--muted-foreground))]">Loading anime...</span>
                  ) : selectedAnime ? (
                    <span>{selectedAnime}</span>
                  ) : (
                    <span className="text-[hsl(var(--muted-foreground))]">Select an anime...</span>
                  )}
                  <ChevronDown className="h-4 w-4 text-[hsl(var(--muted-foreground))]" />
                </button>

                {showAnimeDropdown && (
                  <div className="absolute z-10 mt-1 w-full bg-[hsl(var(--background))] border border-[hsl(var(--border))] rounded-md shadow-lg max-h-60 overflow-hidden">
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
                    <div className="overflow-y-auto max-h-40">
                      {filteredAnime.length === 0 ? (
                        <div className="px-3 py-2 text-sm text-[hsl(var(--muted-foreground))]">
                          {indexedAnime.length === 0 ? 'No anime indexed yet' : 'No matches found'}
                        </div>
                      ) : (
                        filteredAnime.map((anime) => (
                          <button
                            key={anime}
                            type="button"
                            onClick={() => selectAnime(anime)}
                            className="w-full text-left px-3 py-2 hover:bg-[hsl(var(--muted))] text-sm"
                          >
                            {anime}
                          </button>
                        ))
                      )}
                    </div>

                    {/* Index new option */}
                    <div className="border-t border-[hsl(var(--border))] p-2">
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
              <div className="space-y-3 p-3 border border-[hsl(var(--border))] rounded-md bg-[hsl(var(--muted))/0.3]">
                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium">Index New Anime</span>
                  <button
                    type="button"
                    onClick={() => setIndexNewMode(false)}
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
                      onClick={async () => {
                        // Use native File System Access API
                        if ('showDirectoryPicker' in window) {
                          try {
                            const dirHandle = await (window as Window & { showDirectoryPicker: () => Promise<FileSystemDirectoryHandle> }).showDirectoryPicker();
                            // Get the path - note: browser API doesn't expose full path for security
                            // We need to prompt user to enter the path manually after selection
                            // or use a backend endpoint to resolve the folder
                            setNewAnimePath(dirHandle.name);
                          } catch (err) {
                            // User cancelled or error
                            if ((err as Error).name !== 'AbortError') {
                              console.error('Failed to open folder picker:', err);
                            }
                          }
                        } else {
                          // Fallback: show alert for browsers without support
                          alert('Folder picker not supported in this browser. Please enter the path manually.');
                        }
                      }}
                    >
                      <FolderOpen className="h-4 w-4" />
                    </Button>
                  </div>
                  <p className="text-xs text-[hsl(var(--muted-foreground))] mt-1">
                    Note: Enter the full absolute path (e.g., /home/user/anime/MyAnime)
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

                {/* Indexing progress */}
                {indexProgress && indexProgress.status !== 'error' && (
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
          {downloadProgress && downloadProgress.status !== 'error' && (
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
          {detectionProgress && detectionProgress.status !== 'error' && (
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

          {error && (
            <p className="text-sm text-[hsl(var(--destructive))]">{error}</p>
          )}

          <Button 
            type="submit" 
            className="w-full" 
            disabled={isLoading || !canSubmit}
            data-testid="create-project-btn"
          >
            {isLoading ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin mr-2" />
                {indexing ? 'Indexing...' : downloading ? 'Downloading...' : detecting ? 'Detecting scenes...' : 'Creating...'}
              </>
            ) : indexNewMode ? (
              'Index & Start'
            ) : (
              'Download & Start'
            )}
          </Button>
        </form>

        <p className="text-xs text-center text-[hsl(var(--muted-foreground))]">
          Enter a TikTok URL and select the anime to reproduce from
        </p>
      </div>
    </div>
  );
}
