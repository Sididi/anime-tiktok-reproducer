import { useEffect, useState, useCallback, useRef } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { Check, Loader2, AlertCircle, Search, Edit, ArrowLeft, Play, ChevronLeft, ChevronRight } from 'lucide-react';
import { Button } from '@/components/ui';
import { ClippedVideoPlayer, ManualMatchModal } from '@/components/video';
import type { ClippedVideoPlayerHandle } from '@/components/video';
import { useProjectStore, useSceneStore } from '@/stores';
import { api } from '@/api/client';
import { formatTime } from '@/utils';
import type { SceneMatch, Scene, AlternativeMatch } from '@/types';

interface MatchProgress {
  status: string;
  progress: number;
  message: string;
  scene_index?: number;
  error?: string | null;
  matches?: SceneMatch[];
}

interface MatchCardProps {
  scene: Scene;
  match: SceneMatch;
  projectId: string;
  episodes: string[];
  onManualMatch: (sceneIndex: number, episode: string, startTime: number, endTime: number) => void;
  onSelectAlternative: (sceneIndex: number, alt: AlternativeMatch) => void;
}

/**
 * Inline carousel for quick selection of alternative matches.
 * Shows thumbnails with confidence scores for one-click selection.
 */
function AlternativeCarousel({
  alternatives,
  currentEpisode,
  projectId,
  onSelect,
}: {
  alternatives: AlternativeMatch[];
  currentEpisode: string;
  projectId: string;
  onSelect: (alt: AlternativeMatch) => void;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [canScrollLeft, setCanScrollLeft] = useState(false);
  const [canScrollRight, setCanScrollRight] = useState(false);

  // Filter out the current selection and show other alternatives
  const otherAlternatives = alternatives.filter(alt => alt.episode !== currentEpisode);

  const updateScrollState = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    setCanScrollLeft(el.scrollLeft > 0);
    setCanScrollRight(el.scrollLeft < el.scrollWidth - el.clientWidth - 1);
  }, []);

  useEffect(() => {
    updateScrollState();
    const el = scrollRef.current;
    if (el) {
      el.addEventListener('scroll', updateScrollState);
      return () => el.removeEventListener('scroll', updateScrollState);
    }
  }, [updateScrollState]);

  const scroll = (direction: 'left' | 'right') => {
    const el = scrollRef.current;
    if (!el) return;
    const scrollAmount = 120;
    el.scrollBy({ left: direction === 'left' ? -scrollAmount : scrollAmount, behavior: 'smooth' });
  };

  if (otherAlternatives.length === 0) return null;

  return (
    <div className="mt-2">
      <p className="text-xs text-[hsl(var(--muted-foreground))] mb-1">
        Other candidates ({otherAlternatives.length}):
      </p>
      <div className="relative">
        {/* Left scroll button */}
        {canScrollLeft && (
          <button
            onClick={() => scroll('left')}
            className="absolute left-0 top-1/2 -translate-y-1/2 z-10 bg-black/70 hover:bg-black/90 rounded-full p-1"
          >
            <ChevronLeft className="h-4 w-4 text-white" />
          </button>
        )}

        {/* Scrollable container */}
        <div
          ref={scrollRef}
          className="flex gap-2 overflow-x-auto scrollbar-hide px-1 py-1"
          style={{ scrollbarWidth: 'none', msOverflowStyle: 'none' }}
        >
          {otherAlternatives.map((alt, idx) => (
            <button
              key={`${alt.episode}-${idx}`}
              onClick={() => onSelect(alt)}
              className="flex-shrink-0 group relative rounded overflow-hidden border-2 border-transparent hover:border-[hsl(var(--primary))] transition-colors"
              title={`${alt.episode.split('/').pop()} - ${Math.round(alt.confidence * 100)}%`}
            >
              {/* Thumbnail preview - use video poster or frame */}
              <div className="w-20 h-12 bg-black/40 relative">
                <video
                  src={api.getSourceVideoUrl(projectId, alt.episode)}
                  className="w-full h-full object-cover"
                  muted
                  preload="metadata"
                  onLoadedMetadata={(e) => {
                    // Seek to the start time to show a frame preview
                    (e.target as HTMLVideoElement).currentTime = alt.start_time;
                  }}
                />
                {/* Confidence badge */}
                <div className="absolute bottom-0 right-0 bg-black/80 text-white text-[10px] px-1 py-0.5">
                  {Math.round(alt.confidence * 100)}%
                </div>
              </div>
              {/* Episode name */}
              <div className="text-[9px] text-center truncate max-w-20 px-0.5 bg-[hsl(var(--muted))]">
                {alt.episode.split('/').pop()?.replace(/\.[^.]+$/, '')}
              </div>
            </button>
          ))}
        </div>

        {/* Right scroll button */}
        {canScrollRight && (
          <button
            onClick={() => scroll('right')}
            className="absolute right-0 top-1/2 -translate-y-1/2 z-10 bg-black/70 hover:bg-black/90 rounded-full p-1"
          >
            <ChevronRight className="h-4 w-4 text-white" />
          </button>
        )}
      </div>
    </div>
  );
}

function MatchCard({ scene, match, projectId, episodes, onManualMatch, onSelectAlternative }: MatchCardProps) {
  const [showManualModal, setShowManualModal] = useState(false);
  const tiktokPlayerRef = useRef<ClippedVideoPlayerHandle>(null);
  const sourcePlayerRef = useRef<ClippedVideoPlayerHandle>(null);
  
  const tiktokVideoUrl = api.getVideoUrl(projectId);
  const hasMatch = match.confidence > 0 && match.episode;
  const sourceVideoUrl = hasMatch ? api.getSourceVideoUrl(projectId, match.episode) : null;

  // Calculate durations
  const tiktokDuration = scene.end_time - scene.start_time;
  const sourceDuration = hasMatch ? match.end_time - match.start_time : 0;

  const handleManualSave = useCallback((episode: string, startTime: number, endTime: number) => {
    onManualMatch(scene.index, episode, startTime, endTime);
  }, [scene.index, onManualMatch]);

  const handleSyncPlay = useCallback(() => {
    // Play both videos simultaneously from their start times
    tiktokPlayerRef.current?.play();
    sourcePlayerRef.current?.play();
  }, []);

  return (
    <div className="bg-[hsl(var(--card))] rounded-lg p-4 space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="font-semibold">Scene {scene.index + 1}</h3>
        {hasMatch ? (
          <span className="flex items-center gap-1 text-sm text-emerald-500">
            <Check className="h-4 w-4" />
            Matched ({Math.round(match.confidence * 100)}%)
          </span>
        ) : (
          <span className="flex items-center gap-1 text-sm text-amber-500">
            <AlertCircle className="h-4 w-4" />
            No match found
          </span>
        )}
      </div>

      <div className="grid grid-cols-2 gap-4">
        {/* TikTok clip */}
        <div>
          <p className="text-xs text-[hsl(var(--muted-foreground))] mb-2">TikTok Clip</p>
          <div className="aspect-[9/16] bg-black rounded overflow-hidden">
            <ClippedVideoPlayer
              ref={tiktokPlayerRef}
              src={tiktokVideoUrl}
              startTime={scene.start_time}
              endTime={scene.end_time}
              className="w-full h-full"
            />
          </div>
          <p className="text-xs text-[hsl(var(--muted-foreground))] mt-1">
            {formatTime(scene.start_time)} - {formatTime(scene.end_time)} (<strong>{formatTime(tiktokDuration)}</strong>)
          </p>
        </div>

        {/* Source clip */}
        <div>
          <p className="text-xs text-[hsl(var(--muted-foreground))] mb-2 truncate" title={match.episode || 'Not found'}>
            Source: {match.episode ? match.episode.split('/').pop() : 'Not found'}
          </p>
          <div className="aspect-[9/16] bg-black rounded overflow-hidden flex items-center justify-center">
            {hasMatch && sourceVideoUrl ? (
              <ClippedVideoPlayer
                ref={sourcePlayerRef}
                src={sourceVideoUrl}
                startTime={match.start_time}
                endTime={match.end_time}
                className="w-full h-full"
              />
            ) : (
              <div className="flex flex-col items-center gap-2 text-[hsl(var(--muted-foreground))]">
                <p className="text-xs">No match</p>
                {episodes.length > 0 && (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => setShowManualModal(true)}
                  >
                    <Edit className="h-3 w-3 mr-1" />
                    Select Manually
                  </Button>
                )}
              </div>
            )}
          </div>
          {hasMatch ? (
            <p className="text-xs text-[hsl(var(--muted-foreground))] mt-1">
              {formatTime(match.start_time)} - {formatTime(match.end_time)} (<strong>{formatTime(sourceDuration)}</strong> ~{match.speed_ratio.toFixed(2)}x speed)
            </p>
          ) : (
            <p className="text-xs text-[hsl(var(--muted-foreground))] mt-1">&nbsp;</p>
          )}

          {/* Alternative candidates carousel */}
          {match.alternatives && match.alternatives.length > 1 && (
            <AlternativeCarousel
              alternatives={match.alternatives}
              currentEpisode={match.episode}
              projectId={projectId}
              onSelect={(alt) => onSelectAlternative(scene.index, alt)}
            />
          )}
        </div>
      </div>

      {/* Action buttons */}
      <div className="flex gap-2">
        {hasMatch && (
          <Button
            variant="default"
            size="sm"
            className="flex-1"
            onClick={handleSyncPlay}
          >
            <Play className="h-4 w-4 mr-2" />
            Play Both
          </Button>
        )}
        {hasMatch && (
          <Button
            variant="outline"
            size="sm"
            onClick={() => setShowManualModal(true)}
          >
            <Edit className="h-4 w-4" />
          </Button>
        )}
        {!hasMatch && episodes.length > 0 && (
          <Button
            variant="default"
            size="sm"
            className="flex-1"
            onClick={() => setShowManualModal(true)}
          >
            <Edit className="h-4 w-4 mr-2" />
            Select Manually
          </Button>
        )}
      </div>

      {/* Manual match modal */}
      <ManualMatchModal
        isOpen={showManualModal}
        onClose={() => setShowManualModal(false)}
        scene={scene}
        projectId={projectId}
        episodes={episodes}
        onSave={handleManualSave}
      />
    </div>
  );
}

export function MatchValidation() {
  const { projectId } = useParams<{ projectId: string }>();
  const navigate = useNavigate();
  const { project, loadProject } = useProjectStore();
  const { scenes, loadScenes } = useSceneStore();

  const [matches, setMatches] = useState<SceneMatch[]>([]);
  const [episodes, setEpisodes] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [matching, setMatching] = useState(false);
  const [matchProgress, setMatchProgress] = useState<MatchProgress | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Load data
  useEffect(() => {
    if (!projectId) return;

    const loadData = async () => {
      setLoading(true);
      try {
        await loadProject(projectId);
        await loadScenes(projectId);
        const { matches: loadedMatches } = await api.getMatches(projectId);
        setMatches(loadedMatches);
        // Load available episodes for manual matching
        const { episodes: loadedEpisodes } = await api.getEpisodes(projectId);
        setEpisodes(loadedEpisodes);
      } catch (err) {
        setError((err as Error).message);
      } finally {
        setLoading(false);
      }
    };

    loadData();
  }, [projectId, loadProject, loadScenes]);

  const handleFindMatches = useCallback(async () => {
    if (!projectId) return;

    setMatching(true);
    setMatchProgress({ status: 'starting', progress: 0, message: 'Starting match search...' });

    try {
      // No longer need source_path - uses configured library path on backend
      const response = await api.findMatches(projectId);

      if (!response.ok) {
        throw new Error('Failed to start matching');
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
              const data = JSON.parse(line.slice(6)) as MatchProgress;
              setMatchProgress(data);

              if (data.status === 'complete' && data.matches) {
                // data.matches is {matches: [...]} from MatchList.model_dump()
                const matchesData = data.matches as unknown as { matches: SceneMatch[] };
                setMatches(matchesData.matches || []);
              }

              if (data.status === 'error') {
                throw new Error(data.error || 'Matching failed');
              }
            } catch (e) {
              if (e instanceof SyntaxError) continue;
              throw e;
            }
          }
        }
      }
    } catch (err) {
      setError((err as Error).message);
      setMatchProgress(null);
    } finally {
      setMatching(false);
    }
  }, [projectId]);

  const handleManualMatch = useCallback(
    async (sceneIndex: number, episode: string, startTime: number, endTime: number) => {
      if (!projectId) return;

      try {
        const { match: updatedMatch } = await api.updateMatch(projectId, sceneIndex, {
          episode,
          start_time: startTime,
          end_time: endTime,
          confirmed: true,
        });

        setMatches((prev) =>
          prev.map((m) => (m.scene_index === sceneIndex ? updatedMatch : m))
        );
      } catch (err) {
        setError((err as Error).message);
      }
    },
    [projectId]
  );

  // Quick select an alternative match (one-click from carousel)
  const handleSelectAlternative = useCallback(
    async (sceneIndex: number, alt: AlternativeMatch) => {
      if (!projectId) return;

      try {
        const { match: updatedMatch } = await api.updateMatch(projectId, sceneIndex, {
          episode: alt.episode,
          start_time: alt.start_time,
          end_time: alt.end_time,
          confirmed: true,
        });

        setMatches((prev) =>
          prev.map((m) => (m.scene_index === sceneIndex ? updatedMatch : m))
        );
      } catch (err) {
        setError((err as Error).message);
      }
    },
    [projectId]
  );

  // Count confirmed: matches with a match (confidence > 0) are auto-confirmed
  // Unmatched scenes (confidence === 0, no episode) need manual selection
  const matchedCount = matches.filter((m) => m.confidence > 0 && m.episode).length;
  const unmatchedCount = matches.filter((m) => m.confidence === 0 || !m.episode).length;
  const allMatched = matches.length > 0 && unmatchedCount === 0;

  const handleContinue = () => {
    if (projectId) {
      navigate(`/project/${projectId}/transcription`);
    }
  };

  const handleBack = () => {
    if (projectId) {
      navigate(`/project/${projectId}/scenes`);
    }
  };

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <Loader2 className="h-8 w-8 animate-spin text-[hsl(var(--muted-foreground))]" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <p className="text-[hsl(var(--destructive))]">{error}</p>
      </div>
    );
  }

  return (
    <div className="min-h-screen p-4">
      <div className="max-w-4xl mx-auto space-y-6">
        <header className="flex items-center justify-between">
          <div className="flex items-center gap-4">
            <Button variant="ghost" size="sm" onClick={handleBack}>
              <ArrowLeft className="h-4 w-4 mr-2" />
              Back to Scenes
            </Button>
            <div>
              <h1 className="text-xl font-bold">Match Validation</h1>
              <p className="text-sm text-[hsl(var(--muted-foreground))]">
                Verify the detected anime source clips
              </p>
            </div>
          </div>
          <div className="flex items-center gap-4">
            <span className="text-sm text-[hsl(var(--muted-foreground))]">
              {matchedCount} / {matches.length} matched
              {unmatchedCount > 0 && (
                <span className="text-amber-500 ml-1">({unmatchedCount} need manual selection)</span>
              )}
            </span>
            <Button onClick={handleContinue} disabled={!allMatched}>
              Continue to Transcription
            </Button>
          </div>
        </header>

        {/* No matches yet - show Find Matches button */}
        {matches.length === 0 && !matching && (
          <div className="bg-[hsl(var(--card))] rounded-lg p-8 text-center space-y-4">
            <Search className="h-12 w-12 mx-auto text-[hsl(var(--muted-foreground))]" />
            <div>
              <h2 className="text-lg font-semibold">No Matches Found Yet</h2>
              <p className="text-sm text-[hsl(var(--muted-foreground))]">
                Click to search for anime source clips matching your TikTok scenes
              </p>
              {project?.anime_name && (
                <p className="text-xs text-[hsl(var(--muted-foreground))] mt-2">
                  Searching in: {project.anime_name}
                </p>
              )}
            </div>
            <Button onClick={handleFindMatches} disabled={!projectId}>
              <Search className="h-4 w-4 mr-2" />
              Find Matches
            </Button>
          </div>
        )}

        {/* Matching in progress */}
        {matching && matchProgress && (
          <div className="bg-[hsl(var(--card))] rounded-lg p-8 text-center space-y-4">
            <Loader2 className="h-12 w-12 mx-auto animate-spin text-[hsl(var(--primary))]" />
            <div>
              <h2 className="text-lg font-semibold">Finding Matches...</h2>
              <p className="text-sm text-[hsl(var(--muted-foreground))]">
                {matchProgress.message}
              </p>
              {matchProgress.scene_index !== undefined && (
                <p className="text-xs text-[hsl(var(--muted-foreground))] mt-1">
                  Processing scene {matchProgress.scene_index + 1} of {scenes.length}
                </p>
              )}
            </div>
            <div className="h-2 bg-[hsl(var(--muted))] rounded-full overflow-hidden max-w-md mx-auto">
              <div
                className="h-full bg-[hsl(var(--primary))] transition-all duration-300"
                style={{ width: `${matchProgress.progress * 100}%` }}
              />
            </div>
          </div>
        )}

        {/* Show matches */}
        <div className="space-y-4">
          {scenes.map((scene) => {
            const match = matches.find((m) => m.scene_index === scene.index);
            if (!match) return null;

            return (
              <MatchCard
                key={scene.index}
                scene={scene}
                match={match}
                projectId={projectId!}
                episodes={episodes}
                onManualMatch={handleManualMatch}
                onSelectAlternative={handleSelectAlternative}
              />
            );
          })}
        </div>
      </div>
    </div>
  );
}
