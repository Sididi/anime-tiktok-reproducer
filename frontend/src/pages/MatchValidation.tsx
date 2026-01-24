import { useEffect, useState, useCallback, useRef } from "react";
import { useParams, useNavigate } from "react-router-dom";
import {
  Check,
  Loader2,
  AlertCircle,
  Edit,
  Play,
  ArrowLeft,
  RefreshCw,
  Search,
} from "lucide-react";
import { Button } from "@/components/ui";
import { ClippedVideoPlayer, ManualMatchModal } from "@/components/video";
import type { ClippedVideoPlayerHandle } from "@/components/video/ClippedVideoPlayer";
import { useProjectStore, useSceneStore } from "@/stores";
import { api } from "@/api/client";
import { formatTime } from "@/utils";
import type { SceneMatch, Scene } from "@/types";

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
  onManualMatch: (
    sceneIndex: number,
    episode: string,
    startTime: number,
    endTime: number,
  ) => void;
}

function MatchCard({
  scene,
  match,
  projectId,
  episodes,
  onManualMatch,
}: MatchCardProps) {
  const [showManualModal, setShowManualModal] = useState(false);
  const tiktokPlayerRef = useRef<ClippedVideoPlayerHandle>(null);
  const sourcePlayerRef = useRef<ClippedVideoPlayerHandle>(null);

  const tiktokVideoUrl = api.getVideoUrl(projectId);
  const hasMatch = match.confidence > 0 && match.episode;
  const sourceVideoUrl = hasMatch
    ? api.getSourceVideoUrl(projectId, match.episode)
    : null;

  // Calculate durations
  const tiktokDuration = scene.end_time - scene.start_time;
  const sourceDuration = hasMatch ? match.end_time - match.start_time : 0;

  const handleManualSave = useCallback(
    (episode: string, startTime: number, endTime: number) => {
      onManualMatch(scene.index, episode, startTime, endTime);
    },
    [scene.index, onManualMatch],
  );

  // Sync play both videos simultaneously using refs
  const handleSyncPlay = useCallback(() => {
    tiktokPlayerRef.current?.playFromStart();
    sourcePlayerRef.current?.playFromStart();
  }, []);

  return (
    <div
      className="bg-[hsl(var(--card))] rounded-lg p-4 space-y-4"
      data-scene-index={scene.index}
    >
      <div className="flex items-center justify-between">
        <h3 className="font-semibold">Scene {scene.index + 1}</h3>
        {hasMatch ? (
          <span className="flex items-center gap-1 text-sm text-emerald-500">
            <Check className="h-4 w-4" />
            {Math.round(match.confidence * 100)}% match
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
        <div data-video-type="tiktok">
          <p className="text-xs text-[hsl(var(--muted-foreground))] mb-2">
            TikTok Clip
          </p>
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
            {formatTime(scene.start_time)} - {formatTime(scene.end_time)} (
            <strong>{formatTime(tiktokDuration)}</strong>)
          </p>
        </div>

        {/* Source clip */}
        <div data-video-type="source">
          <p
            className="text-xs text-[hsl(var(--muted-foreground))] mb-2 truncate"
            title={match.episode || "Not found"}
          >
            Source:{" "}
            {match.episode ? match.episode.split("/").pop() : "Not found"}
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
              <div className="flex flex-col items-center gap-2 text-[hsl(var(--muted-foreground))] p-4">
                <AlertCircle className="h-8 w-8 text-amber-500 mb-2" />
                <p className="text-xs text-center">No automatic match found</p>
                <p className="text-xs text-center opacity-60">
                  {match.alternatives?.length || 0} AI candidates available
                </p>
                {episodes.length > 0 && (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => setShowManualModal(true)}
                    className="w-full mt-2"
                  >
                    <Edit className="h-3 w-3 mr-1" />
                    Find Match
                  </Button>
                )}
              </div>
            )}
          </div>
          {hasMatch ? (
            <p className="text-xs text-[hsl(var(--muted-foreground))] mt-1">
              {formatTime(match.start_time)} - {formatTime(match.end_time)} (
              <strong>{formatTime(sourceDuration)}</strong> ~
              {match.speed_ratio.toFixed(2)}x speed)
            </p>
          ) : (
            <p className="text-xs text-[hsl(var(--muted-foreground))] mt-1">
              &nbsp;
            </p>
          )}
        </div>
      </div>

      {/* Action buttons for matched scenes */}
      {hasMatch && (
        <div className="flex gap-2">
          <Button
            variant="outline"
            size="sm"
            className="flex-1"
            onClick={handleSyncPlay}
          >
            <Play className="h-4 w-4 mr-2" />
            Play Both
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setShowManualModal(true)}
          >
            <Edit className="h-4 w-4" />
          </Button>
        </div>
      )}

      {/* Manual match modal */}
      <ManualMatchModal
        isOpen={showManualModal}
        onClose={() => setShowManualModal(false)}
        scene={scene}
        match={match}
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
  const [matchProgress, setMatchProgress] = useState<MatchProgress | null>(
    null,
  );
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
    setMatchProgress({
      status: "starting",
      progress: 0,
      message: "Starting match search...",
    });

    try {
      const response = await api.findMatches(projectId);

      if (!response.ok) {
        throw new Error("Failed to start matching");
      }

      const reader = response.body?.getReader();
      if (!reader) {
        throw new Error("No response body");
      }

      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n\n");
        buffer = lines.pop() || "";

        for (const line of lines) {
          if (line.startsWith("data: ")) {
            try {
              const data = JSON.parse(line.slice(6)) as MatchProgress;
              setMatchProgress(data);

              if (data.status === "complete" && data.matches) {
                const matchesData = data.matches as unknown as {
                  matches: SceneMatch[];
                };
                setMatches(matchesData.matches || []);
              }

              if (data.status === "error") {
                throw new Error(data.error || "Matching failed");
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
    async (
      sceneIndex: number,
      episode: string,
      startTime: number,
      endTime: number,
    ) => {
      if (!projectId) return;

      try {
        const { match: updatedMatch } = await api.updateMatch(
          projectId,
          sceneIndex,
          {
            episode,
            start_time: startTime,
            end_time: endTime,
            confirmed: true,
          },
        );

        setMatches((prev) =>
          prev.map((m) => (m.scene_index === sceneIndex ? updatedMatch : m)),
        );
      } catch (err) {
        setError((err as Error).message);
      }
    },
    [projectId],
  );

  const handleBackToScenes = () => {
    if (projectId) {
      navigate(`/project/${projectId}/scenes`);
    }
  };

  const handleRecomputeMatches = async () => {
    // Clear existing matches and recompute
    setMatches([]);
    await handleFindMatches();
  };

  // Count confirmed matches (those with valid match data)
  const confirmedCount = matches.filter(
    (m) => m.confidence > 0 && m.episode,
  ).length;
  const totalCount = matches.length;
  const allConfirmed = totalCount > 0 && confirmedCount === totalCount;

  const handleContinue = () => {
    if (projectId) {
      navigate(`/project/${projectId}/transcription`);
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
            <Button variant="ghost" size="icon" onClick={handleBackToScenes}>
              <ArrowLeft className="h-5 w-5" />
            </Button>
            <div>
              <h1 className="text-xl font-bold">Match Validation</h1>
              <p className="text-sm text-[hsl(var(--muted-foreground))]">
                Verify the detected anime source clips
              </p>
            </div>
          </div>
          <div className="flex items-center gap-4">
            {matches.length > 0 && (
              <Button
                variant="outline"
                size="sm"
                onClick={handleRecomputeMatches}
                disabled={matching}
              >
                <RefreshCw
                  className={`h-4 w-4 mr-2 ${matching ? "animate-spin" : ""}`}
                />
                Recompute
              </Button>
            )}
            <span className="text-sm text-[hsl(var(--muted-foreground))]">
              {confirmedCount} / {totalCount} matched
            </span>
            <Button onClick={handleContinue} disabled={!allConfirmed}>
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
                Click to search for anime source clips matching your TikTok
                scenes
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
                  Processing scene {matchProgress.scene_index + 1} of{" "}
                  {scenes.length}
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
              />
            );
          })}
        </div>
      </div>
    </div>
  );
}
