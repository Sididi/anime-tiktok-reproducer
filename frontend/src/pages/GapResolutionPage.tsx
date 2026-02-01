import { useEffect, useState, useCallback, useRef } from "react";
import { useParams, useNavigate } from "react-router-dom";
import {
  Check,
  Loader2,
  AlertTriangle,
  Play,
  ArrowLeft,
  SkipForward,
  Sparkles,
  Clock,
  RotateCcw,
  Wand2,
} from "lucide-react";
import { Button } from "@/components/ui";
import { ClippedVideoPlayer, ManualMatchModal } from "@/components/video";
import type { ClippedVideoPlayerHandle } from "@/components/video/ClippedVideoPlayer";
import { useProjectStore, useSceneStore } from "@/stores";
import { api } from "@/api/client";
import { formatTime } from "@/utils";
import type { Scene } from "@/types";

interface GapInfo {
  scene_index: number;
  episode: string;
  current_start: number;
  current_end: number;
  current_duration: number;
  timeline_start: number;
  timeline_end: number;
  target_duration: number;
  required_speed: number;
  effective_speed: number;
  gap_duration: number;
}

interface GapCandidate {
  start_time: number;
  end_time: number;
  duration: number;
  effective_speed: number;
  speed_diff: number;
  extend_type: string;
  snap_description: string;
}

interface GapCardProps {
  gap: GapInfo;
  scene: Scene | undefined;
  projectId: string;
  episodes: string[];
  isResolved: boolean;
  isSkipped: boolean;
  resolvedTiming: { start: number; end: number; speed: number } | null;
  onUpdate: (
    sceneIndex: number,
    startTime: number,
    endTime: number,
    speed: number,
  ) => void;
  onSkip: (sceneIndex: number) => void;
}

function GapCard({
  gap,
  scene,
  projectId,
  episodes,
  isResolved,
  isSkipped,
  resolvedTiming,
  onUpdate,
  onSkip,
}: GapCardProps) {
  const [showManualModal, setShowManualModal] = useState(false);
  const [candidates, setCandidates] = useState<GapCandidate[]>([]);
  const [loadingCandidates, setLoadingCandidates] = useState(true);
  const tiktokPlayerRef = useRef<ClippedVideoPlayerHandle>(null);
  const sourcePlayerRef = useRef<ClippedVideoPlayerHandle>(null);

  const tiktokVideoUrl = api.getVideoUrl(projectId);
  const sourceVideoUrl = api.getSourceVideoUrl(projectId, gap.episode);

  // Load AI candidates
  useEffect(() => {
    const loadCandidates = async () => {
      setLoadingCandidates(true);
      try {
        const response = await fetch(
          `/api/projects/${projectId}/gaps/${gap.scene_index}/candidates`,
        );
        if (response.ok) {
          const data = await response.json();
          setCandidates(data.candidates || []);
        }
      } catch (err) {
        console.error("Failed to load candidates:", err);
      } finally {
        setLoadingCandidates(false);
      }
    };

    loadCandidates();
  }, [projectId, gap.scene_index]);

  // Use resolved timing if available, otherwise current
  const displayStart = resolvedTiming?.start ?? gap.current_start;
  const displayEnd = resolvedTiming?.end ?? gap.current_end;
  const displaySpeed = resolvedTiming?.speed ?? gap.effective_speed;

  // Calculate if still has gap after resolution
  const hasGapAfterResolution = displaySpeed < 0.75;

  const handleSelectCandidate = useCallback(
    async (candidate: GapCandidate) => {
      onUpdate(
        gap.scene_index,
        candidate.start_time,
        candidate.end_time,
        candidate.effective_speed,
      );
      
      // Reset and auto-play both previews after a short delay for state update
      setTimeout(() => {
        tiktokPlayerRef.current?.playFromStart();
        sourcePlayerRef.current?.playFromStart();
      }, 100);
    },
    [gap.scene_index, onUpdate],
  );

  const handleManualSave = useCallback(
    async (_episode: string, startTime: number, endTime: number) => {
      // Calculate speed for this timing
      const duration = endTime - startTime;
      const speed = duration / gap.target_duration;
      const effectiveSpeed = Math.max(0.75, Math.min(1.6, speed));

      onUpdate(gap.scene_index, startTime, endTime, effectiveSpeed);
    },
    [gap.scene_index, gap.target_duration, onUpdate],
  );

  const handleSyncPlay = useCallback(() => {
    tiktokPlayerRef.current?.playFromStart();
    sourcePlayerRef.current?.playFromStart();
  }, []);

  const formatSpeed = (speed: number) => {
    return `${Math.round(speed * 100)}%`;
  };

  return (
    <div
      className={`bg-[hsl(var(--card))] rounded-lg p-4 space-y-4 ${
        isResolved
          ? "border-2 border-green-500/30"
          : "border-2 border-amber-500/30"
      }`}
    >
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <h3 className="font-semibold">Scene {gap.scene_index + 1}</h3>
          {isResolved ? (
            <span className="flex items-center gap-1 text-xs px-1.5 py-0.5 rounded bg-green-500/10 text-green-500 border border-green-500/20">
              <Check className="h-3 w-3" />
              resolved
            </span>
          ) : (
            <span className="flex items-center gap-1 text-xs px-1.5 py-0.5 rounded bg-amber-500/10 text-amber-500 border border-amber-500/20">
              <AlertTriangle className="h-3 w-3" />
              has gap
            </span>
          )}
        </div>
        <div className="text-right text-sm">
          <span className="text-[hsl(var(--muted-foreground))]">Gap: </span>
          <span className="font-mono text-amber-500">
            {gap.gap_duration.toFixed(2)}s
          </span>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-4">
        {/* TikTok clip */}
        <div>
          <p className="text-xs text-[hsl(var(--muted-foreground))] mb-2">
            TikTok Clip
          </p>
          <div className="aspect-9/16 bg-black rounded overflow-hidden">
            {scene && (
              <ClippedVideoPlayer
                ref={tiktokPlayerRef}
                src={tiktokVideoUrl}
                startTime={scene.start_time}
                endTime={scene.end_time}
                className="w-full h-full"
              />
            )}
          </div>
          {scene && (
            <p className="text-xs text-[hsl(var(--muted-foreground))] mt-1">
              {formatTime(scene.start_time)} - {formatTime(scene.end_time)}
            </p>
          )}
        </div>

        {/* Source clip */}
        <div>
          <p
            className="text-xs text-[hsl(var(--muted-foreground))] mb-2 truncate"
            title={gap.episode}
          >
            Source: {gap.episode.split("/").pop()}
          </p>
          <div className="aspect-9/16 bg-black rounded overflow-hidden">
            <ClippedVideoPlayer
              ref={sourcePlayerRef}
              src={sourceVideoUrl}
              startTime={displayStart}
              endTime={displayEnd}
              className="w-full h-full"
            />
          </div>
          <div className="flex items-center justify-between mt-1">
            <p className="text-xs text-[hsl(var(--muted-foreground))]">
              {formatTime(displayStart)} - {formatTime(displayEnd)}
            </p>
            <div
              className="group relative"
              title={`Source clip (${(displayEnd - displayStart).toFixed(2)}s) ÷ TTS duration (${gap.target_duration.toFixed(2)}s) = ${formatSpeed(displaySpeed)}`}
            >
              <span
                className={`text-xs font-mono cursor-help border-b border-dotted ${
                  hasGapAfterResolution
                    ? "text-red-500 border-red-500/50"
                    : displaySpeed < 0.9
                      ? "text-amber-500 border-amber-500/50"
                      : "text-green-500 border-green-500/50"
                }`}
              >
                {formatSpeed(displaySpeed)} speed
              </span>
              {/* Speed explanation tooltip */}
              <div className="absolute bottom-full right-0 mb-2 hidden group-hover:block z-50">
                <div className="bg-[hsl(var(--popover))] border border-[hsl(var(--border))] rounded-lg p-3 shadow-lg whitespace-nowrap text-xs">
                  <div className="font-medium mb-1">Speed Calculation</div>
                  <div className="text-[hsl(var(--muted-foreground))] space-y-1">
                    <div>Source clip: <span className="font-mono">{(displayEnd - displayStart).toFixed(2)}s</span></div>
                    <div>TTS duration: <span className="font-mono">{gap.target_duration.toFixed(2)}s</span></div>
                    <div className="border-t border-[hsl(var(--border))] pt-1 mt-1">
                      <span className="font-mono">{(displayEnd - displayStart).toFixed(2)}s ÷ {gap.target_duration.toFixed(2)}s</span> = <span className="font-semibold">{formatSpeed(displaySpeed)}</span>
                    </div>
                    {displaySpeed < 1 && (
                      <div className="text-amber-500 pt-1">
                        ↓ Clip plays slower than TTS
                      </div>
                    )}
                    {displaySpeed > 1 && (
                      <div className="text-green-500 pt-1">
                        ↑ Clip plays faster than TTS
                      </div>
                    )}
                  </div>
                </div>
              </div>
            </div>
          </div>
          {hasGapAfterResolution && (
            <p className="text-xs text-red-500 mt-1">
              ⚠️ Still has gap (speed &lt; 75%)
            </p>
          )}
        </div>
      </div>

      {/* AI Candidates */}
      <div className="space-y-2">
        <div className="flex items-center gap-2">
          <Sparkles className="h-4 w-4 text-amber-500" />
          <span className="text-sm font-medium">AI Candidates</span>
          {loadingCandidates && (
            <Loader2 className="h-3 w-3 animate-spin text-[hsl(var(--muted-foreground))]" />
          )}
        </div>

        {candidates.length > 0 ? (
          <div className="grid grid-cols-2 gap-2">
            {candidates.map((candidate, idx) => (
              <button
                key={idx}
                onClick={() => handleSelectCandidate(candidate)}
                className={`flex flex-col px-3 py-2 rounded text-sm text-left transition-colors ${
                  resolvedTiming?.start === candidate.start_time &&
                  resolvedTiming?.end === candidate.end_time
                    ? "bg-green-500/20 border border-green-500/50"
                    : "bg-[hsl(var(--muted))] hover:bg-[hsl(var(--accent))]"
                }`}
                title={`${candidate.duration.toFixed(2)}s source ÷ ${gap.target_duration.toFixed(2)}s TTS = ${formatSpeed(candidate.effective_speed)}`}
              >
                <div className="flex items-center justify-between w-full">
                  <span
                    className={`font-mono text-xs ${
                      candidate.effective_speed >= 0.95 &&
                      candidate.effective_speed <= 1.05
                        ? "text-green-500"
                        : candidate.effective_speed < 0.75
                          ? "text-red-500"
                          : "text-amber-500"
                    }`}
                  >
                    {formatSpeed(candidate.effective_speed)}
                  </span>
                  <span className="text-xs text-[hsl(var(--muted-foreground))]">
                    {candidate.extend_type.replace("extend_", "")}
                  </span>
                </div>
                <span className="text-xs text-[hsl(var(--muted-foreground))] mt-1 truncate w-full">
                  {candidate.snap_description}
                </span>
              </button>
            ))}
          </div>
        ) : !loadingCandidates ? (
          <p className="text-xs text-[hsl(var(--muted-foreground))]">
            No candidates found. Use manual editing.
          </p>
        ) : null}
      </div>

      {/* Action buttons */}
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
          <Clock className="h-4 w-4 mr-1" />
          Manual
        </Button>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => onSkip(gap.scene_index)}
          className={`hover:text-amber-400 ${
            isSkipped
              ? "bg-amber-500/20 text-amber-500 border border-amber-500/50"
              : "text-amber-500"
          }`}
          title="Skip this gap (keep 75% speed)"
        >
          <SkipForward className="h-4 w-4" />
        </Button>
      </div>

      {/* Manual match modal - reusing existing component */}
      {scene && (
        <ManualMatchModal
          isOpen={showManualModal}
          onClose={() => setShowManualModal(false)}
          scene={scene}
          match={{
            scene_index: gap.scene_index,
            episode: gap.episode,
            start_time: displayStart,
            end_time: displayEnd,
            confidence: 1,
            speed_ratio: displaySpeed,
            confirmed: true,
            alternatives: [],
            start_candidates: [],
            middle_candidates: [],
            end_candidates: [],
          }}
          projectId={projectId}
          episodes={episodes}
          onSave={handleManualSave}
        />
      )}
    </div>
  );
}

export function GapResolutionPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const navigate = useNavigate();
  const { loadProject } = useProjectStore();
  const { scenes, loadScenes } = useSceneStore();

  const [gaps, setGaps] = useState<GapInfo[]>([]);
  const [episodes, setEpisodes] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [resolvedGaps, setResolvedGaps] = useState<
    Map<number, { start: number; end: number; speed: number }>
  >(new Map());
  const [skippedGaps, setSkippedGaps] = useState<Set<number>>(new Set());
  const [saving, setSaving] = useState(false);

  // Load data
  useEffect(() => {
    if (!projectId) return;

    const loadData = async () => {
      setLoading(true);
      try {
        await loadProject(projectId);
        await loadScenes(projectId);

        // Load gaps
        const gapsResponse = await fetch(`/api/projects/${projectId}/gaps`);
        if (gapsResponse.ok) {
          const gapsData = await gapsResponse.json();
          setGaps(gapsData.gaps || []);
        }

        // Load episodes for manual editing
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

  const handleUpdateGap = useCallback(
    async (
      sceneIndex: number,
      startTime: number,
      endTime: number,
      speed: number,
    ) => {
      if (!projectId) return;

      try {
        // Update on backend
        await fetch(`/api/projects/${projectId}/gaps/${sceneIndex}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            start_time: startTime,
            end_time: endTime,
            skipped: false,
          }),
        });

        // Update local state
        setResolvedGaps((prev) => {
          const next = new Map(prev);
          next.set(sceneIndex, { start: startTime, end: endTime, speed });
          return next;
        });

        // Remove from skipped if it was skipped
        setSkippedGaps((prev) => {
          const next = new Set(prev);
          next.delete(sceneIndex);
          return next;
        });
      } catch (err) {
        setError((err as Error).message);
      }
    },
    [projectId],
  );

  const handleSkipGap = useCallback(
    async (sceneIndex: number) => {
      if (!projectId) return;

      try {
        // Find the gap
        const gap = gaps.find((g) => g.scene_index === sceneIndex);
        if (!gap) return;

        // Update on backend with original timing (skipped=true)
        await fetch(`/api/projects/${projectId}/gaps/${sceneIndex}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            start_time: gap.current_start,
            end_time: gap.current_end,
            skipped: true,
          }),
        });

        // Update local state
        setSkippedGaps((prev) => {
          const next = new Set(prev);
          next.add(sceneIndex);
          return next;
        });

        // Remove from resolved if it was resolved
        setResolvedGaps((prev) => {
          const next = new Map(prev);
          next.delete(sceneIndex);
          return next;
        });
      } catch (err) {
        setError((err as Error).message);
      }
    },
    [projectId, gaps],
  );

  const handleContinue = useCallback(async () => {
    if (!projectId) return;

    setSaving(true);
    try {
      // Mark gaps as resolved
      await fetch(`/api/projects/${projectId}/gaps/mark-resolved`, {
        method: "POST",
      });

      // Navigate back to processing page to resume
      navigate(`/project/${projectId}/processing`);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  }, [projectId, navigate]);

  const [resetting, setResetting] = useState(false);
  const handleReset = useCallback(async () => {
    if (!projectId) return;
    
    if (!window.confirm("Reset all gap resolutions? This will restore original timings and you'll need to re-resolve all gaps.")) {
      return;
    }

    setResetting(true);
    try {
      await fetch(`/api/projects/${projectId}/gaps/reset`, {
        method: "POST",
      });
      
      // Navigate back to processing to re-trigger gap detection
      navigate(`/project/${projectId}/processing`);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setResetting(false);
    }
  }, [projectId, navigate]);

  const [autoFilling, setAutoFilling] = useState(false);
  const handleAutoFill = useCallback(async () => {
    if (!projectId) return;

    setAutoFilling(true);
    try {
      const response = await fetch(`/api/projects/${projectId}/gaps/auto-fill`, {
        method: "POST",
      });

      if (!response.ok) {
        throw new Error("Failed to auto-fill gaps");
      }

      const data = await response.json();

      // Update local state with all filled gaps
      const newResolvedGaps = new Map(resolvedGaps);
      for (const result of data.results) {
        if (result.success) {
          newResolvedGaps.set(result.scene_index, {
            start: result.start_time,
            end: result.end_time,
            speed: result.speed,
          });
        }
      }
      setResolvedGaps(newResolvedGaps);

      // Clear any skipped gaps that were auto-filled
      const newSkippedGaps = new Set(skippedGaps);
      for (const result of data.results) {
        if (result.success) {
          newSkippedGaps.delete(result.scene_index);
        }
      }
      setSkippedGaps(newSkippedGaps);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setAutoFilling(false);
    }
  }, [projectId, resolvedGaps, skippedGaps]);

  // Count resolved + skipped
  const handledCount = resolvedGaps.size + skippedGaps.size;
  const totalGaps = gaps.length;
  const allHandled = handledCount === totalGaps;

  // Count skipped that still have warnings
  const warningCount = skippedGaps.size;

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
        <header className="space-y-4">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-4">
              <Button variant="ghost" size="icon" onClick={() => navigate(-1)}>
                <ArrowLeft className="h-5 w-5" />
              </Button>
              <div>
                <h1 className="text-xl font-bold">Gap Resolution</h1>
                <p className="text-sm text-[hsl(var(--muted-foreground))]">
                  Extend clips to fill timeline gaps
                </p>
              </div>
            </div>
            <div className="text-right">
              <div className="text-sm text-[hsl(var(--muted-foreground))]">
                {handledCount} / {totalGaps} handled
              </div>
            </div>
          </div>

          {/* Info banner */}
          <div className="bg-amber-500/10 border border-amber-500/30 rounded-lg p-4">
            <div className="flex items-start gap-3">
              <AlertTriangle className="h-5 w-5 text-amber-500 shrink-0 mt-0.5" />
              <div className="space-y-1">
                <p className="text-sm font-medium">
                  {totalGaps} clip{totalGaps !== 1 ? "s" : ""} hit the 75% speed
                  floor
                </p>
                <p className="text-xs text-[hsl(var(--muted-foreground))]">
                  These clips need more source footage to avoid gaps in the
                  timeline. Extend them using the AI candidates or manually
                  adjust the timings.
                </p>
              </div>
            </div>
          </div>

          {/* Action buttons */}
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              {warningCount > 0 && (
                <p className="text-sm text-red-500">
                  ⚠️ {warningCount} clip{warningCount !== 1 ? "s" : ""} will
                  have gaps (skipped)
                </p>
              )}
            </div>
            <div className="flex items-center gap-2">
              <Button
                variant="outline"
                onClick={handleReset}
                disabled={resetting}
                title="Reset all gap resolutions and restore original timings"
              >
                {resetting ? (
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                ) : (
                  <RotateCcw className="h-4 w-4 mr-2" />
                )}
                Reset
              </Button>
              <Button
                variant="outline"
                onClick={handleAutoFill}
                disabled={autoFilling || allHandled}
                title="Auto-fill all gaps with best AI candidates"
              >
                {autoFilling ? (
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                ) : (
                  <Wand2 className="h-4 w-4 mr-2" />
                )}
                Auto-Fill All
              </Button>
              <Button onClick={handleContinue} disabled={saving || !allHandled}>
                {saving ? (
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                ) : (
                  <Check className="h-4 w-4 mr-2" />
                )}
                Continue Processing
              </Button>
            </div>
          </div>
        </header>

        {/* Gap cards */}
        <div className="space-y-4">
          {gaps.map((gap) => {
            const scene = scenes.find((s) => s.index === gap.scene_index);
            const isResolved = resolvedGaps.has(gap.scene_index);
            const isSkipped = skippedGaps.has(gap.scene_index);
            const resolvedTiming = resolvedGaps.get(gap.scene_index) || null;

            return (
              <GapCard
                key={gap.scene_index}
                gap={gap}
                scene={scene}
                projectId={projectId!}
                episodes={episodes}
                isResolved={isResolved || isSkipped}
                isSkipped={isSkipped}
                resolvedTiming={resolvedTiming}
                onUpdate={handleUpdateGap}
                onSkip={handleSkipGap}
              />
            );
          })}
        </div>
      </div>
    </div>
  );
}
