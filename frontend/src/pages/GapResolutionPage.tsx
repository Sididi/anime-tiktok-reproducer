import {
  useEffect,
  useState,
  useCallback,
  useRef,
  useMemo,
  forwardRef,
  useImperativeHandle,
} from "react";
import { useParams, useNavigate, useLocation } from "react-router-dom";
import {
  Check,
  Loader2,
  AlertTriangle,
  Play,
  Pause,
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
import { useSceneStore } from "@/stores";
import { api } from "@/api/client";
import { cn, formatTime } from "@/utils";
import type { Scene, SourceStreamDescriptor } from "@/types";

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

const CANDIDATE_MATCH_EPSILON = 1e-4;
const MAX_DYNAMIC_CHUNK_SECONDS = 120;

interface GapCardProps {
  gap: GapInfo;
  scene: Scene | undefined;
  projectId: string;
  episodes: string[];
  getSourceDescriptor: (
    episode: string,
  ) => Promise<SourceStreamDescriptor | null>;
  isResolved: boolean;
  isSkipped: boolean;
  resolvedTiming: { start: number; end: number; speed: number } | null;
  candidates: GapCandidate[];
  loadingCandidates: boolean;
  isActive?: boolean;
  shouldLoadSourcePreview?: boolean;
  playbackRate?: number;
  controlsDisabled?: boolean;
  preloadMode?: "metadata" | "auto";
  ensureEpisodesLoaded: () => Promise<string[]>;
  onUpdate: (
    sceneIndex: number,
    startTime: number,
    endTime: number,
    speed: number,
  ) => void;
  onSkip: (sceneIndex: number) => void;
}

interface GapCardHandle {
  playBothAndWait: () => Promise<void>;
  prepareForAutoplay: () => Promise<boolean>;
  releasePreload: () => void;
  stop: () => void;
}

const GapCard = forwardRef<GapCardHandle, GapCardProps>(function GapCard(
  {
    gap,
    scene,
    projectId,
    episodes,
    getSourceDescriptor,
    isResolved,
    isSkipped,
    resolvedTiming,
    candidates,
    loadingCandidates,
    isActive = false,
    shouldLoadSourcePreview = false,
    playbackRate = 1,
    controlsDisabled = false,
    preloadMode = "metadata",
    ensureEpisodesLoaded,
    onUpdate,
    onSkip,
  },
  ref,
) {
  const [showManualModal, setShowManualModal] = useState(false);
  const [openingManual, setOpeningManual] = useState(false);
  const tiktokPlayerRef = useRef<ClippedVideoPlayerHandle>(null);
  const sourcePlayerRef = useRef<ClippedVideoPlayerHandle>(null);
  const pendingResolverRef = useRef<(() => void) | null>(null);
  const pendingTimeoutRef = useRef<number | null>(null);
  const endedRef = useRef({ tiktok: false, source: false });
  const [sourceDescriptor, setSourceDescriptor] =
    useState<SourceStreamDescriptor | null>(null);
  const [sourceDescriptorLoading, setSourceDescriptorLoading] = useState(false);
  const [sourceChunkStart, setSourceChunkStart] = useState(0);
  const [sourceChunkDuration, setSourceChunkDuration] = useState(0);

  const tiktokVideoUrl = api.getVideoUrl(projectId);
  const hasPlayableScene = Boolean(scene);

  // Use resolved timing if available, otherwise current
  const displayStart = resolvedTiming?.start ?? gap.current_start;
  const displayEnd = resolvedTiming?.end ?? gap.current_end;
  const displaySpeed = resolvedTiming?.speed ?? gap.effective_speed;
  const sourceClipDuration = Math.max(0.1, displayEnd - displayStart);

  // Calculate if still has gap after resolution
  const hasGapAfterResolution = displaySpeed < 0.75;

  const shouldUseChunkedSource = useCallback(
    (descriptor: SourceStreamDescriptor | null): boolean => {
      if (!descriptor) return false;
      if (descriptor.mode === "chunked") return true;

      // High-rate Fast Watch can stutter on direct HEVC decode; prefer chunked
      // H.264 preview to stabilize playback pacing.
      const codec = (descriptor.codec || "").toLowerCase();
      const pixFmt = (descriptor.pix_fmt || "").toLowerCase();
      return playbackRate >= 8 && (codec === "hevc" || pixFmt.includes("10"));
    },
    [playbackRate],
  );

  const isChunkedSource = shouldUseChunkedSource(sourceDescriptor);

  const getChunkWindowStart = useCallback(
    (
      targetTime: number,
      descriptor: SourceStreamDescriptor,
      windowDuration: number,
    ): number => {
      const boundedDuration = Math.min(
        Math.max(windowDuration, descriptor.chunk_duration),
        MAX_DYNAMIC_CHUNK_SECONDS,
      );
      const maxStart = Math.max((descriptor.duration || 0) - boundedDuration, 0);
      const boundedTarget = Math.min(
        Math.max(targetTime, 0),
        descriptor.duration || targetTime,
      );
      const centered = Math.max(boundedTarget - boundedDuration / 2, 0);
      const step = Math.max(descriptor.chunk_step || 0.001, 0.001);
      const snapped = Math.floor(centered / step) * step;
      return Math.min(Math.max(snapped, 0), maxStart);
    },
    [],
  );

  /* eslint-disable react-hooks/set-state-in-effect, react-hooks/exhaustive-deps */
  useEffect(() => {
    if (!shouldLoadSourcePreview) {
      setSourceDescriptor(null);
      setSourceDescriptorLoading(false);
      return;
    }

    let active = true;
    setSourceDescriptorLoading(true);

    void getSourceDescriptor(gap.episode)
      .then((descriptor) => {
        if (!active) return;
        setSourceDescriptor(descriptor);

        if (shouldUseChunkedSource(descriptor)) {
          const desiredDuration = Math.min(
            Math.max(
              descriptor.chunk_duration,
              sourceClipDuration + descriptor.seek_guard_seconds * 2,
            ),
            MAX_DYNAMIC_CHUNK_SECONDS,
          );
          const centeredTarget = (displayStart + displayEnd) / 2;
          setSourceChunkDuration(desiredDuration);
          setSourceChunkStart(
            getChunkWindowStart(centeredTarget, descriptor, desiredDuration),
          );
        } else {
          setSourceChunkDuration(0);
          setSourceChunkStart(0);
        }
      })
      .catch(() => {
        if (!active) return;
        setSourceDescriptor(null);
      })
      .finally(() => {
        if (!active) return;
        setSourceDescriptorLoading(false);
      });

    return () => {
      active = false;
    };
  }, [
    gap.episode,
    getSourceDescriptor,
    getChunkWindowStart,
    shouldUseChunkedSource,
    shouldLoadSourcePreview,
  ]);

  useEffect(() => {
    if (!sourceDescriptor || !isChunkedSource) return;

    const duration =
      sourceChunkDuration > 0 ? sourceChunkDuration : sourceDescriptor.chunk_duration;
    const guard = sourceDescriptor.seek_guard_seconds;
    const safeStart = sourceChunkStart + guard;
    const safeEnd = sourceChunkStart + duration - guard;

    if (displayStart >= safeStart && displayEnd <= safeEnd) {
      return;
    }

    const requestedDuration = Math.min(
      Math.max(
        sourceDescriptor.chunk_duration,
        sourceClipDuration + guard * 2,
      ),
      MAX_DYNAMIC_CHUNK_SECONDS,
    );
    const centeredTarget = (displayStart + displayEnd) / 2;
    setSourceChunkDuration(requestedDuration);
    setSourceChunkStart(
      getChunkWindowStart(centeredTarget, sourceDescriptor, requestedDuration),
    );
  }, [
    displayEnd,
    displayStart,
    getChunkWindowStart,
    sourceChunkDuration,
    sourceChunkStart,
    sourceClipDuration,
    sourceDescriptor,
    isChunkedSource,
  ]);
  /* eslint-enable react-hooks/set-state-in-effect, react-hooks/exhaustive-deps */

  const sourceVideoUrl = useMemo(() => {
    if (isChunkedSource && sourceDescriptor) {
      const duration =
        sourceChunkDuration > 0
          ? sourceChunkDuration
          : sourceDescriptor.chunk_duration;
      return api.getSourceChunkUrl(
        projectId,
        gap.episode,
        sourceChunkStart,
        duration,
      );
    }
    return api.getSourceVideoUrl(projectId, gap.episode);
  }, [
    gap.episode,
    isChunkedSource,
    projectId,
    sourceChunkDuration,
    sourceChunkStart,
    sourceDescriptor,
  ]);

  const sourceStartForPlayer = isChunkedSource
    ? Math.max(0, displayStart - sourceChunkStart)
    : displayStart;
  const sourceEndForPlayer = isChunkedSource
    ? Math.max(
        sourceStartForPlayer + 0.05,
        Math.min(
          displayEnd - sourceChunkStart,
          (sourceChunkDuration > 0
            ? sourceChunkDuration
            : sourceDescriptor?.chunk_duration || displayEnd),
        ),
      )
    : displayEnd;

  const fastWatchMinReadyState =
    playbackRate >= 3
      ? HTMLMediaElement.HAVE_FUTURE_DATA
      : HTMLMediaElement.HAVE_CURRENT_DATA;
  const fastWatchReadyTimeoutMs = playbackRate >= 4 ? 9000 : 7000;

  const resolvePendingPlayback = useCallback(() => {
    if (pendingTimeoutRef.current !== null) {
      window.clearTimeout(pendingTimeoutRef.current);
      pendingTimeoutRef.current = null;
    }
    const resolver = pendingResolverRef.current;
    pendingResolverRef.current = null;
    resolver?.();
  }, []);

  const handleClipEnded = useCallback(
    (track: "tiktok" | "source") => {
      endedRef.current[track] = true;
      if (endedRef.current.tiktok && endedRef.current.source) {
        resolvePendingPlayback();
      }
    },
    [resolvePendingPlayback],
  );

  const warmupPair = useCallback(
    async (timeoutMs: number): Promise<boolean> => {
      if (!hasPlayableScene) return false;
      const tiktok = tiktokPlayerRef.current;
      const source = sourcePlayerRef.current;
      if (!tiktok || !source) return false;

      await Promise.all([
        tiktok.waitUntilReady({
          minReadyState: fastWatchMinReadyState,
          timeoutMs,
        }),
        source.waitUntilReady({
          minReadyState: fastWatchMinReadyState,
          timeoutMs,
        }),
      ]);

      if (tiktok.hasLoadError() || source.hasLoadError()) {
        return false;
      }

      if (
        tiktok.getReadyState() < fastWatchMinReadyState ||
        source.getReadyState() < fastWatchMinReadyState
      ) {
        return false;
      }

      await Promise.all([tiktok.seekToStart(), source.seekToStart()]);
      return !(tiktok.hasLoadError() || source.hasLoadError());
    },
    [fastWatchMinReadyState, hasPlayableScene],
  );

  const recoverPairLoadOnce = useCallback(async (): Promise<boolean> => {
    if (!hasPlayableScene) return false;
    const tiktok = tiktokPlayerRef.current;
    const source = sourcePlayerRef.current;
    if (!tiktok || !source) return false;

    await Promise.all([tiktok.retryLoad(), source.retryLoad()]);
    const recoveryTimeout = Math.max(fastWatchReadyTimeoutMs + 1500, 9000);
    return warmupPair(recoveryTimeout);
  }, [fastWatchReadyTimeoutMs, hasPlayableScene, warmupPair]);

  const playBothFromStart = useCallback(async () => {
    if (!hasPlayableScene) return;

    const tiktok = tiktokPlayerRef.current;
    const source = sourcePlayerRef.current;
    if (!tiktok || !source) {
      tiktok?.playFromStart();
      source?.playFromStart();
      return;
    }

    endedRef.current = { tiktok: false, source: false };
    await Promise.all([tiktok.seekToStart(), source.seekToStart()]);
    tiktok.play();
    source.play();
  }, [hasPlayableScene]);

  useImperativeHandle(
    ref,
    () => ({
      playBothAndWait: async () => {
        if (!hasPlayableScene || !scene) {
          return;
        }

        resolvePendingPlayback();
        await new Promise<void>((resolve) => {
          pendingResolverRef.current = resolve;
          const clipDurationSeconds =
            Math.max(0.1, scene.end_time - scene.start_time) /
            Math.max(playbackRate, 0.1);
          pendingTimeoutRef.current = window.setTimeout(
            () => resolvePendingPlayback(),
            clipDurationSeconds * 1000 + 6000,
          );
          void playBothFromStart().catch(() => {
            resolvePendingPlayback();
          });
        });
      },
      prepareForAutoplay: async () => {
        if (!hasPlayableScene) return false;

        const tiktok = tiktokPlayerRef.current;
        const source = sourcePlayerRef.current;
        if (!tiktok || !source) return false;

        tiktok.forceLoad();
        source.forceLoad();

        const warmed = await warmupPair(fastWatchReadyTimeoutMs);
        if (warmed) {
          return true;
        }

        return recoverPairLoadOnce();
      },
      releasePreload: () => {
        tiktokPlayerRef.current?.releaseLoad();
        sourcePlayerRef.current?.releaseLoad();
      },
      stop: () => {
        tiktokPlayerRef.current?.pause();
        sourcePlayerRef.current?.pause();
        resolvePendingPlayback();
      },
    }),
    [
      fastWatchReadyTimeoutMs,
      hasPlayableScene,
      playbackRate,
      playBothFromStart,
      recoverPairLoadOnce,
      resolvePendingPlayback,
      scene,
      warmupPair,
    ],
  );

  useEffect(() => {
    return () => {
      resolvePendingPlayback();
    };
  }, [resolvePendingPlayback]);

  const handleSelectCandidate = useCallback(
    async (candidate: GapCandidate) => {
      onUpdate(
        gap.scene_index,
        candidate.start_time,
        candidate.end_time,
        candidate.effective_speed,
      );

      // Reset and auto-play both previews after a short delay for state update
      window.setTimeout(() => {
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

  const handleOpenManual = useCallback(async () => {
    setOpeningManual(true);
    try {
      await ensureEpisodesLoaded();
      setShowManualModal(true);
    } catch {
      return;
    } finally {
      setOpeningManual(false);
    }
  }, [ensureEpisodesLoaded]);

  const handleSyncPlay = useCallback(async () => {
    await playBothFromStart();
  }, [playBothFromStart]);

  const formatSpeed = (speed: number) => {
    return `${Math.round(speed * 100)}%`;
  };

  return (
    <div
      data-gap-scene-index={gap.scene_index}
      className={cn(
        "bg-[hsl(var(--card))] rounded-lg p-4 space-y-4",
        isResolved
          ? "border-2 border-green-500/30"
          : "border-2 border-amber-500/30",
        isActive && "ring-2 ring-[hsl(var(--primary))]/50",
      )}
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
                className={cn(
                  "w-full h-full",
                  controlsDisabled && "pointer-events-none",
                )}
                playbackRate={playbackRate}
                eager={preloadMode === "auto"}
                onClipEnded={() => handleClipEnded("tiktok")}
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
            {!shouldLoadSourcePreview ? (
              <div className="w-full h-full flex items-center justify-center text-xs text-white/70 px-4 text-center">
                Source preview loads when this card is nearby or visible.
              </div>
            ) : sourceDescriptorLoading ? (
              <div className="w-full h-full flex items-center justify-center text-xs text-white/70">
                <Loader2 className="h-4 w-4 animate-spin mr-2" />
                Preparing source stream...
              </div>
            ) : (
              <ClippedVideoPlayer
                ref={sourcePlayerRef}
                src={sourceVideoUrl}
                startTime={sourceStartForPlayer}
                endTime={sourceEndForPlayer}
                className={cn(
                  "w-full h-full",
                  controlsDisabled && "pointer-events-none",
                )}
                playbackRate={playbackRate}
                eager={preloadMode === "auto"}
                onClipEnded={() => handleClipEnded("source")}
              />
            )}
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
                    <div>
                      Source clip:{" "}
                      <span className="font-mono">
                        {(displayEnd - displayStart).toFixed(2)}s
                      </span>
                    </div>
                    <div>
                      TTS duration:{" "}
                      <span className="font-mono">
                        {gap.target_duration.toFixed(2)}s
                      </span>
                    </div>
                    <div className="border-t border-[hsl(var(--border))] pt-1 mt-1">
                      <span className="font-mono">
                        {(displayEnd - displayStart).toFixed(2)}s ÷{" "}
                        {gap.target_duration.toFixed(2)}s
                      </span>{" "}
                      ={" "}
                      <span className="font-semibold">
                        {formatSpeed(displaySpeed)}
                      </span>
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
            {candidates.map((candidate, idx) => {
              const isSelected =
                !!resolvedTiming &&
                Math.abs(resolvedTiming.start - candidate.start_time) <
                  CANDIDATE_MATCH_EPSILON &&
                Math.abs(resolvedTiming.end - candidate.end_time) <
                  CANDIDATE_MATCH_EPSILON;

              return (
                <button
                  key={idx}
                  onClick={() => handleSelectCandidate(candidate)}
                  className={`flex flex-col px-3 py-2 rounded text-sm text-left transition-colors ${
                    isSelected
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
              );
            })}
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
          disabled={controlsDisabled}
        >
          <Play className="h-4 w-4 mr-2" />
          Play Both
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            void handleOpenManual();
          }}
          disabled={controlsDisabled || openingManual}
        >
          {openingManual ? (
            <Loader2 className="h-4 w-4 mr-1 animate-spin" />
          ) : (
            <Clock className="h-4 w-4 mr-1" />
          )}
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
});

export function GapResolutionPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const { scenes, loadScenes } = useSceneStore();

  const autoResolveRequested = Boolean(
    (location.state as { autoResolve?: boolean } | null)?.autoResolve,
  );
  const [autoResolving, setAutoResolving] = useState(autoResolveRequested);
  const autoResolveAttemptedRef = useRef(false);

  const [gaps, setGaps] = useState<GapInfo[]>([]);
  const [episodes, setEpisodes] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [resolvedGaps, setResolvedGaps] = useState<
    Map<number, { start: number; end: number; speed: number }>
  >(new Map());
  const [skippedGaps, setSkippedGaps] = useState<Set<number>>(new Set());
  const [saving, setSaving] = useState(false);
  const [candidatesByScene, setCandidatesByScene] = useState<
    Record<number, GapCandidate[]>
  >({});
  const [loadingCandidates, setLoadingCandidates] = useState(false);
  const [activeSceneIndex, setActiveSceneIndex] = useState(-1);
  const [autoScroll, setAutoScroll] = useState(true);
  const [fastWatchPlaying, setFastWatchPlaying] = useState(false);
  const [playbackRate, setPlaybackRate] = useState(1);
  const [visibleSceneIndices, setVisibleSceneIndices] = useState<Set<number>>(
    new Set(),
  );
  const gapRefs = useRef<Map<number, HTMLDivElement>>(new Map());
  const cardRefs = useRef<Map<number, GapCardHandle>>(new Map());
  const descriptorCacheRef = useRef<Map<string, SourceStreamDescriptor | null>>(
    new Map(),
  );
  const descriptorRequestsRef = useRef<
    Map<string, Promise<SourceStreamDescriptor | null>>
  >(new Map());
  const autoplayTokenRef = useRef(0);
  const preparedScenesRef = useRef<Set<number>>(new Set());
  const failedPreparedScenesRef = useRef<Set<number>>(new Set());
  const preparingScenesRef = useRef<Map<number, Promise<void>>>(new Map());
  const prefetchQueueRef = useRef<Promise<void>>(Promise.resolve());
  const episodesLoadedRef = useRef(false);
  const episodesRequestRef = useRef<Promise<string[]> | null>(null);
  const autoScrollRef = useRef(true);
  autoScrollRef.current = autoScroll;

  const sortedGaps = useMemo(
    () => [...gaps].sort((a, b) => a.scene_index - b.scene_index),
    [gaps],
  );
  const sceneByIndex = useMemo(
    () => new Map(scenes.map((scene) => [scene.index, scene])),
    [scenes],
  );

  const fastWatchPrefetchAhead = useMemo(() => {
    if (playbackRate >= 8) return 3;
    if (playbackRate >= 2) return 2;
    return 1;
  }, [playbackRate]);

  const getSourceDescriptorCached = useCallback(
    (episode: string): Promise<SourceStreamDescriptor | null> => {
      if (!projectId) {
        return Promise.resolve(null);
      }

      if (descriptorCacheRef.current.has(episode)) {
        return Promise.resolve(
          descriptorCacheRef.current.get(episode) ?? null,
        );
      }

      const existing = descriptorRequestsRef.current.get(episode);
      if (existing) {
        return existing;
      }

      const request = api
        .getSourceDescriptor(projectId, episode)
        .then((descriptor) => {
          descriptorCacheRef.current.set(episode, descriptor);
          return descriptor;
        })
        .catch(() => {
          descriptorCacheRef.current.set(episode, null);
          return null;
        })
        .finally(() => {
          descriptorRequestsRef.current.delete(episode);
        });

      descriptorRequestsRef.current.set(episode, request);
      return request;
    },
    [projectId],
  );

  const ensureEpisodesLoaded = useCallback(async (): Promise<string[]> => {
    if (!projectId) {
      return [];
    }
    if (episodesLoadedRef.current) {
      return episodes;
    }

    const existing = episodesRequestRef.current;
    if (existing) {
      return existing;
    }

    const request = api
      .getEpisodes(projectId)
      .then(({ episodes: loadedEpisodes }) => {
        episodesLoadedRef.current = true;
        setEpisodes(loadedEpisodes);
        return loadedEpisodes;
      })
      .catch((err) => {
        setError((err as Error).message);
        throw err;
      })
      .finally(() => {
        episodesRequestRef.current = null;
      });

    episodesRequestRef.current = request;
    return request;
  }, [episodes, projectId]);

  useEffect(() => {
    descriptorCacheRef.current.clear();
    descriptorRequestsRef.current.clear();
    episodesLoadedRef.current = false;
    episodesRequestRef.current = null;
    setEpisodes([]);
    setVisibleSceneIndices(new Set());
  }, [projectId]);

  useEffect(() => {
    const observedEntries = Array.from(gapRefs.current.entries());
    if (observedEntries.length === 0) {
      setVisibleSceneIndices(new Set());
      return;
    }

    const observer = new IntersectionObserver(
      (entries) => {
        setVisibleSceneIndices((previous) => {
          const next = new Set(previous);
          let changed = false;

          for (const entry of entries) {
            const rawSceneIndex = (entry.target as HTMLElement).dataset.gapSceneIndex;
            const sceneIndex = Number(rawSceneIndex);
            if (!Number.isFinite(sceneIndex)) {
              continue;
            }

            if (entry.isIntersecting) {
              if (!next.has(sceneIndex)) {
                next.add(sceneIndex);
                changed = true;
              }
            } else if (next.delete(sceneIndex)) {
              changed = true;
            }
          }

          return changed ? next : previous;
        });
      },
      {
        rootMargin: "400px 0px",
        threshold: 0.01,
      },
    );

    for (const [, element] of observedEntries) {
      observer.observe(element);
    }

    return () => {
      observer.disconnect();
    };
  }, [sortedGaps]);

  const clearFastWatchBuffers = useCallback(() => {
    preparedScenesRef.current.clear();
    failedPreparedScenesRef.current.clear();
    preparingScenesRef.current.clear();
    prefetchQueueRef.current = Promise.resolve();
  }, []);

  const stopFastWatch = useCallback(() => {
    autoplayTokenRef.current += 1;
    setFastWatchPlaying(false);
    cardRefs.current.forEach((card) => {
      card.stop();
      card.releasePreload();
    });
    clearFastWatchBuffers();
  }, [clearFastWatchBuffers]);

  const scrollToScene = useCallback((sceneIndex: number, force = false) => {
    if (!force && !autoScrollRef.current) return;
    const el = gapRefs.current.get(sceneIndex);
    if (el) {
      el.scrollIntoView({ behavior: "instant", block: "center" });
    }
  }, []);

  const ensureScenePrepared = useCallback(
    (sceneIndex: number, token: number): Promise<void> => {
      if (autoplayTokenRef.current !== token) {
        return Promise.resolve();
      }
      if (failedPreparedScenesRef.current.has(sceneIndex)) {
        return Promise.resolve();
      }
      if (preparedScenesRef.current.has(sceneIndex)) {
        return Promise.resolve();
      }

      const existing = preparingScenesRef.current.get(sceneIndex);
      if (existing) {
        return existing;
      }

      const card = cardRefs.current.get(sceneIndex);
      if (!card) {
        return Promise.resolve();
      }

      const promise = card
        .prepareForAutoplay()
        .then((isPrepared) => {
          if (autoplayTokenRef.current !== token) {
            card.releasePreload();
            return;
          }
          if (!isPrepared) {
            failedPreparedScenesRef.current.add(sceneIndex);
            preparedScenesRef.current.delete(sceneIndex);
            return;
          }
          failedPreparedScenesRef.current.delete(sceneIndex);
          preparedScenesRef.current.add(sceneIndex);
        })
        .catch(() => {
          failedPreparedScenesRef.current.add(sceneIndex);
        })
        .finally(() => {
          preparingScenesRef.current.delete(sceneIndex);
        });

      preparingScenesRef.current.set(sceneIndex, promise);
      return promise;
    },
    [],
  );

  const scheduleScenePreparation = useCallback(
    (sceneIndex: number, token: number) => {
      if (autoplayTokenRef.current !== token) return;
      if (failedPreparedScenesRef.current.has(sceneIndex)) return;
      if (preparedScenesRef.current.has(sceneIndex)) return;
      if (preparingScenesRef.current.has(sceneIndex)) return;

      prefetchQueueRef.current = prefetchQueueRef.current
        .then(() => ensureScenePrepared(sceneIndex, token))
        .catch(() => {
          // Keep queue alive even if one preparation fails.
        });
    },
    [ensureScenePrepared],
  );

  const releaseOutsideFastWatchWindow = useCallback(
    (orderedGaps: GapInfo[], currentOffset: number) => {
      const keepBehind = 0;
      const keepStart = Math.max(0, currentOffset - keepBehind);
      const keepEnd = Math.min(
        orderedGaps.length - 1,
        currentOffset + fastWatchPrefetchAhead,
      );
      const keepSceneIndices = new Set<number>();

      for (let i = keepStart; i <= keepEnd; i += 1) {
        const gap = orderedGaps[i];
        if (gap) {
          keepSceneIndices.add(gap.scene_index);
        }
      }

      for (const preparedSceneIndex of Array.from(preparedScenesRef.current)) {
        if (keepSceneIndices.has(preparedSceneIndex)) continue;
        cardRefs.current.get(preparedSceneIndex)?.releasePreload();
        preparedScenesRef.current.delete(preparedSceneIndex);
      }

      for (const failedSceneIndex of Array.from(
        failedPreparedScenesRef.current,
      )) {
        if (keepSceneIndices.has(failedSceneIndex)) continue;
        cardRefs.current.get(failedSceneIndex)?.releasePreload();
        failedPreparedScenesRef.current.delete(failedSceneIndex);
      }
    },
    [fastWatchPrefetchAhead],
  );

  const playFastWatchFromScene = useCallback(
    async (startSceneIndex: number) => {
      if (!sortedGaps.length) return;

      const token = autoplayTokenRef.current + 1;
      autoplayTokenRef.current = token;
      setFastWatchPlaying(true);
      clearFastWatchBuffers();

      const startPos = sortedGaps.findIndex(
        (gap) => gap.scene_index === startSceneIndex,
      );
      const orderedGaps = startPos >= 0 ? sortedGaps.slice(startPos) : sortedGaps;
      const initialWindow = orderedGaps.slice(0, fastWatchPrefetchAhead + 1);
      const mandatoryWarmup = initialWindow.slice(
        0,
        Math.min(2, initialWindow.length),
      );

      for (const gap of mandatoryWarmup) {
        await ensureScenePrepared(gap.scene_index, token);
        if (autoplayTokenRef.current !== token) {
          return;
        }
      }

      for (const gap of initialWindow.slice(mandatoryWarmup.length)) {
        scheduleScenePreparation(gap.scene_index, token);
      }

      if (autoplayTokenRef.current !== token) {
        return;
      }

      for (let i = 0; i < orderedGaps.length; i += 1) {
        if (autoplayTokenRef.current !== token) {
          return;
        }

        const gap = orderedGaps[i];
        const card = cardRefs.current.get(gap.scene_index);
        if (!card) {
          continue;
        }

        await ensureScenePrepared(gap.scene_index, token);
        if (autoplayTokenRef.current !== token) {
          return;
        }

        if (failedPreparedScenesRef.current.has(gap.scene_index)) {
          setActiveSceneIndex(gap.scene_index);
          scrollToScene(gap.scene_index);
          releaseOutsideFastWatchWindow(orderedGaps, i + 1);
          continue;
        }

        for (let offset = 1; offset <= fastWatchPrefetchAhead; offset += 1) {
          const nextGap = orderedGaps[i + offset];
          if (!nextGap) break;
          scheduleScenePreparation(nextGap.scene_index, token);
        }

        setActiveSceneIndex(gap.scene_index);
        const playback = card.playBothAndWait();
        scrollToScene(gap.scene_index);
        await playback;
        releaseOutsideFastWatchWindow(orderedGaps, i + 1);
      }

      if (autoplayTokenRef.current === token) {
        autoplayTokenRef.current = token + 1;
        setFastWatchPlaying(false);
        for (const preparedSceneIndex of Array.from(
          preparedScenesRef.current,
        )) {
          cardRefs.current.get(preparedSceneIndex)?.releasePreload();
        }
        clearFastWatchBuffers();
      }
    },
    [
      sortedGaps,
      clearFastWatchBuffers,
      fastWatchPrefetchAhead,
      ensureScenePrepared,
      scheduleScenePreparation,
      scrollToScene,
      releaseOutsideFastWatchWindow,
    ],
  );

  const handleToggleFastWatch = useCallback(() => {
    if (fastWatchPlaying) {
      stopFastWatch();
      return;
    }

    const startSceneIndex =
      activeSceneIndex >= 0
        ? activeSceneIndex
        : sortedGaps.length > 0
          ? sortedGaps[0].scene_index
          : undefined;
    if (startSceneIndex === undefined) return;

    void playFastWatchFromScene(startSceneIndex);
  }, [
    fastWatchPlaying,
    stopFastWatch,
    activeSceneIndex,
    sortedGaps,
    playFastWatchFromScene,
  ]);

  const handleTimelineSeek = useCallback(
    (position: number) => {
      const targetGap = sortedGaps[position];
      if (!targetGap) return;

      setActiveSceneIndex(targetGap.scene_index);
      scrollToScene(targetGap.scene_index, true);

      if (fastWatchPlaying) {
        void playFastWatchFromScene(targetGap.scene_index);
      }
    },
    [sortedGaps, scrollToScene, fastWatchPlaying, playFastWatchFromScene],
  );

  // Load data
  useEffect(() => {
    if (!projectId) return;

    let cancelled = false;

    const loadCandidates = async () => {
      if (autoResolveRequested) {
        return;
      }

      setLoadingCandidates(true);
      try {
        const candidatesResponse = await fetch(
          `/api/projects/${projectId}/gaps/all-candidates`,
        );
        if (!candidatesResponse.ok) {
          throw new Error("Failed to load gap candidates");
        }
        const candidatesData = await candidatesResponse.json();
        if (!cancelled) {
          setCandidatesByScene(candidatesData.candidates_by_scene || {});
        }
      } catch (err) {
        console.error("Failed to batch-load candidates:", err);
      } finally {
        if (!cancelled) {
          setLoadingCandidates(false);
        }
      }
    };

    const loadData = async () => {
      setLoading(true);
      setError(null);
      setGaps([]);
      setCandidatesByScene({});
      setLoadingCandidates(false);
      try {
        const gapsRequest = fetch(`/api/projects/${projectId}/gaps`);
        await Promise.all([loadScenes(projectId), gapsRequest]);

        const gapsResponse = await gapsRequest;
        if (!gapsResponse.ok) {
          throw new Error("Failed to load gaps");
        }

        const gapsData = await gapsResponse.json();
        const loadedGaps: GapInfo[] = gapsData.gaps || [];
        if (cancelled) {
          return;
        }

        setGaps(loadedGaps);
        setLoading(false);

        if (loadedGaps.length > 0) {
          void loadCandidates();
        }
      } catch (err) {
        if (!cancelled) {
          setError((err as Error).message);
          setLoading(false);
        }
      }
    };

    void loadData();

    return () => {
      cancelled = true;
    };
  }, [autoResolveRequested, projectId, loadScenes]);

  useEffect(() => {
    if (sortedGaps.length === 0) {
      setActiveSceneIndex(-1);
      return;
    }

    setActiveSceneIndex((prev) => {
      if (sortedGaps.some((gap) => gap.scene_index === prev)) {
        return prev;
      }
      return sortedGaps[0].scene_index;
    });
  }, [sortedGaps]);

  useEffect(() => {
    const cards = cardRefs.current;
    return () => {
      autoplayTokenRef.current += 1;
      cards.forEach((card) => {
        card.stop();
        card.releasePreload();
      });
      clearFastWatchBuffers();
    };
  }, [clearFastWatchBuffers]);

  const handleUpdateGap = useCallback(
    async (
      sceneIndex: number,
      startTime: number,
      endTime: number,
      speed: number,
    ) => {
      if (!projectId) return;

      stopFastWatch();
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
    [projectId, stopFastWatch],
  );

  const handleSkipGap = useCallback(
    async (sceneIndex: number) => {
      if (!projectId) return;

      stopFastWatch();
      try {
        // Find the gap
        const gap = sortedGaps.find((g) => g.scene_index === sceneIndex);
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
    [projectId, sortedGaps, stopFastWatch],
  );

  const handleContinue = useCallback(async () => {
    if (!projectId) return;

    stopFastWatch();
    setSaving(true);
    try {
      // Mark gaps as resolved
      await fetch(`/api/projects/${projectId}/gaps/mark-resolved`, {
        method: "POST",
      });

      // Navigate back to processing page to resume
      navigate(`/project/${projectId}/processing`, {
        state: { resumeAfterGaps: true },
      });
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  }, [projectId, navigate, stopFastWatch]);

  const [resetting, setResetting] = useState(false);
  const handleReset = useCallback(async () => {
    if (!projectId) return;

    if (
      !window.confirm(
        "Reset all gap resolutions? This will restore original timings and you'll need to re-resolve all gaps.",
      )
    ) {
      return;
    }

    stopFastWatch();
    setResetting(true);
    try {
      await fetch(`/api/projects/${projectId}/gaps/reset`, {
        method: "POST",
      });

      // Navigate back to processing to re-trigger gap detection
      navigate(`/project/${projectId}/processing`, {
        state: { resumeAfterGaps: false },
      });
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setResetting(false);
    }
  }, [projectId, navigate, stopFastWatch]);

  const [autoFilling, setAutoFilling] = useState(false);
  const handleAutoFill = useCallback(async () => {
    if (!projectId) return;

    stopFastWatch();
    setAutoFilling(true);
    try {
      const response = await fetch(
        `/api/projects/${projectId}/gaps/auto-fill`,
        {
          method: "POST",
        },
      );

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
  }, [projectId, resolvedGaps, skippedGaps, stopFastWatch]);

  // Auto-resolve: when autoResolve is requested, wait for data, auto-fill, then auto-continue
  useEffect(() => {
    if (
      !autoResolving ||
      !projectId ||
      loading ||
      autoResolveAttemptedRef.current
    ) {
      return;
    }

    autoResolveAttemptedRef.current = true;

    const runAutoResolve = async () => {
      try {
        if (sortedGaps.length === 0) {
          // No gaps — mark resolved and go back
          await fetch(`/api/projects/${projectId}/gaps/mark-resolved`, {
            method: "POST",
          });
          navigate(`/project/${projectId}/processing`, {
            state: { resumeAfterGaps: true },
          });
          return;
        }

        // Auto-fill all gaps and mark resolved in a single request
        const response = await fetch(
          `/api/projects/${projectId}/gaps/auto-fill-and-resolve`,
          { method: "POST" },
        );
        if (!response.ok) {
          throw new Error("Failed to auto-fill gaps");
        }

        navigate(`/project/${projectId}/processing`, {
          state: { resumeAfterGaps: true },
        });
      } catch (err) {
        setError((err as Error).message);
        setAutoResolving(false);
      }
    };

    runAutoResolve();
  }, [autoResolving, projectId, loading, sortedGaps, navigate]);

  // Count resolved + skipped
  const handledCount = resolvedGaps.size + skippedGaps.size;
  const totalGaps = sortedGaps.length;
  const allHandled = handledCount === totalGaps;

  // Count skipped that still have warnings
  const warningCount = skippedGaps.size;

  const activeScenePosition = useMemo(() => {
    if (sortedGaps.length === 0) return 0;
    const index = sortedGaps.findIndex(
      (gap) => gap.scene_index === activeSceneIndex,
    );
    return index >= 0 ? index : 0;
  }, [sortedGaps, activeSceneIndex]);

  if (loading || autoResolving) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="flex flex-col items-center gap-3">
          <Loader2 className="h-8 w-8 animate-spin text-[hsl(var(--muted-foreground))]" />
          {autoResolving && (
            <p className="text-sm text-[hsl(var(--muted-foreground))]">
              Auto-resolving gaps...
            </p>
          )}
        </div>
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
    <div className="min-h-screen p-4 pb-28">
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
          {sortedGaps.map((gap, gapPosition) => {
            const scene = sceneByIndex.get(gap.scene_index);
            const isResolved = resolvedGaps.has(gap.scene_index);
            const isSkipped = skippedGaps.has(gap.scene_index);
            const resolvedTiming = resolvedGaps.get(gap.scene_index) || null;
            const shouldLoadSourcePreview =
              visibleSceneIndices.has(gap.scene_index) ||
              Math.abs(gapPosition - activeScenePosition) <= 2;

            return (
              <div
                key={gap.scene_index}
                data-gap-scene-index={gap.scene_index}
                className="[content-visibility:auto] [contain-intrinsic-size:960px]"
                ref={(el) => {
                  if (el) gapRefs.current.set(gap.scene_index, el);
                  else gapRefs.current.delete(gap.scene_index);
                }}
              >
                <GapCard
                  ref={(card) => {
                    if (card) cardRefs.current.set(gap.scene_index, card);
                    else cardRefs.current.delete(gap.scene_index);
                  }}
                  gap={gap}
                  scene={scene}
                  projectId={projectId!}
                  episodes={episodes}
                  getSourceDescriptor={getSourceDescriptorCached}
                  isResolved={isResolved || isSkipped}
                  isSkipped={isSkipped}
                  resolvedTiming={resolvedTiming}
                  candidates={candidatesByScene[gap.scene_index] || []}
                  loadingCandidates={loadingCandidates}
                  isActive={activeSceneIndex === gap.scene_index}
                  shouldLoadSourcePreview={shouldLoadSourcePreview}
                  playbackRate={playbackRate}
                  controlsDisabled={fastWatchPlaying}
                  preloadMode={
                    fastWatchPlaying &&
                    Math.abs(gapPosition - activeScenePosition) <= 2
                      ? "auto"
                      : "metadata"
                  }
                  ensureEpisodesLoaded={ensureEpisodesLoaded}
                  onUpdate={handleUpdateGap}
                  onSkip={handleSkipGap}
                />
              </div>
            );
          })}
        </div>
      </div>

      {totalGaps > 0 && (
        <div className="fixed bottom-0 left-0 right-0 z-50 bg-[hsl(var(--card))] border-t border-[hsl(var(--border))] shadow-lg">
          <div className="max-w-4xl mx-auto px-4 py-2 space-y-2">
            <div className="flex items-center gap-3">
              <Button
                variant={fastWatchPlaying ? "default" : "outline"}
                size="sm"
                onClick={handleToggleFastWatch}
                disabled={loadingCandidates || totalGaps === 0}
              >
                {fastWatchPlaying ? (
                  <>
                    <Pause className="h-4 w-4 mr-1" />
                    Pause
                  </>
                ) : (
                  <>
                    <Play className="h-4 w-4 mr-1" />
                    Fast Watch
                  </>
                )}
              </Button>

              <span className="text-xs text-[hsl(var(--muted-foreground))] min-w-[100px]">
                Gap {activeScenePosition + 1} / {totalGaps}
              </span>

              <label className="flex items-center gap-1.5 cursor-pointer select-none">
                <input
                  type="checkbox"
                  checked={autoScroll}
                  onChange={(e) => setAutoScroll(e.target.checked)}
                  className="accent-[hsl(var(--primary))] h-3.5 w-3.5"
                />
                <span className="text-xs text-[hsl(var(--muted-foreground))]">
                  Scroll
                </span>
              </label>

              <div className="flex-1" />

              <span className="text-xs font-mono text-[hsl(var(--muted-foreground))] min-w-[34px] text-right">
                {playbackRate}x
              </span>
              <input
                type="range"
                min="0.5"
                max="12"
                step="0.25"
                value={playbackRate}
                onChange={(e) => setPlaybackRate(parseFloat(e.target.value))}
                className="w-24 h-1 accent-[hsl(var(--primary))]"
                title={`Playback speed: ${playbackRate}x`}
              />
            </div>

            <input
              type="range"
              min={0}
              max={Math.max(0, totalGaps - 1)}
              step={1}
              value={activeScenePosition}
              onChange={(e) => handleTimelineSeek(parseInt(e.target.value, 10))}
              className="w-full h-1 accent-[hsl(var(--primary))]"
              title="Timeline scroller"
            />
          </div>
        </div>
      )}
    </div>
  );
}
