import {
  useEffect,
  useState,
  useCallback,
  useRef,
  useMemo,
  memo,
  forwardRef,
  useImperativeHandle,
} from "react";
import { useParams, useNavigate } from "react-router-dom";
import {
  Check,
  Loader2,
  AlertCircle,
  Edit,
  Play,
  Pause,
  ArrowLeft,
  RefreshCw,
  Search,
  Sparkles,
  Wand2,
  Undo2,
  Merge,
} from "lucide-react";
import { Button } from "@/components/ui";
import { MatchesClipPlayer, ManualMatchModal } from "@/components/video";
import type { MatchesClipPlayerHandle } from "@/components/video/MatchesClipPlayer";
import type { ManualMatchSaveMeta } from "@/components/video/ManualMatchModal";
import { useProjectStore, useSceneStore } from "@/stores";
import { api } from "@/api/client";
import { readSSEStream } from "@/utils/sse";
import { cn, formatTime } from "@/utils";
import type {
  MatchesPlaybackManifest,
  SceneMatch,
  Scene,
  ScenePlaybackSceneAsset,
} from "@/types";

interface MatchProgress {
  status: string;
  progress: number;
  message: string;
  scene_index?: number;
  error?: string | null;
  matches?: SceneMatch[];
}

interface PlaybackPrepareProgress {
  status: string;
  progress: number;
  message: string;
  scene_index?: number;
  total_scenes?: number;
  error?: string | null;
  manifest?: MatchesPlaybackManifest;
  cached?: boolean;
  track?: "tiktok" | "source";
  scene_asset?: ScenePlaybackSceneAsset;
}

interface MatchCardProps {
  scene: Scene;
  match: SceneMatch;
  projectId: string;
  episodes: string[];
  playbackAsset: ScenePlaybackSceneAsset | null;
  isActive?: boolean;
  playbackRate?: number;
  controlsDisabled?: boolean;
  preloadMode?: "metadata" | "auto";
  onManualMatch: (
    sceneIndex: number,
    episode: string,
    startTime: number,
    endTime: number,
    meta: ManualMatchSaveMeta,
  ) => void;
  pendingUpdate?: {
    phase: "saving" | "preparing";
    message: string;
  } | null;
  warningMessage?: string | null;
  onUndoMerge?: (sceneIndex: number) => void;
}

interface MatchCardHandle {
  playBothAndWait: () => Promise<void>;
  prepareForAutoplay: () => Promise<boolean>;
  releasePreload: () => void;
  stop: () => void;
}

const MatchCard = forwardRef<MatchCardHandle, MatchCardProps>(
  function MatchCard(
    {
      scene,
      match,
      projectId,
      episodes,
      playbackAsset,
      isActive = false,
      playbackRate = 1,
      controlsDisabled = false,
      preloadMode = "metadata",
      onManualMatch,
      pendingUpdate = null,
      warningMessage = null,
      onUndoMerge,
    },
    ref,
  ) {
    const [showManualModal, setShowManualModal] = useState(false);
    const tiktokPlayerRef = useRef<MatchesClipPlayerHandle>(null);
    const sourcePlayerRef = useRef<MatchesClipPlayerHandle>(null);
    const pendingResolverRef = useRef<(() => void) | null>(null);
    const endedRef = useRef({ tiktok: false, source: false });
    const primedForFastWatchRef = useRef(false);
    const loadFailureRef = useRef(false);

    const tiktokVideoUrl = playbackAsset?.tiktok.url ?? null;
    const hasMatchedScene = Boolean(match.confidence > 0 && match.episode);
    const sourceVideoUrl = hasMatchedScene
      ? playbackAsset?.source?.url ?? null
      : null;
    const hasMatch = Boolean(hasMatchedScene && sourceVideoUrl);

    // Calculate durations
    const tiktokDuration = scene.end_time - scene.start_time;
    const sourceDuration = hasMatchedScene
      ? match.end_time - match.start_time
      : 0;
    const fastWatchMinReadyState =
      playbackRate >= 3
        ? HTMLMediaElement.HAVE_FUTURE_DATA
        : HTMLMediaElement.HAVE_CURRENT_DATA;
    const fastWatchReadyTimeoutMs = playbackRate >= 4 ? 9000 : 7000;

    const handleManualSave = useCallback(
      async (
        episode: string,
        startTime: number,
        endTime: number,
        meta: ManualMatchSaveMeta,
      ) => {
        await onManualMatch(scene.index, episode, startTime, endTime, meta);
      },
      [scene.index, onManualMatch],
    );

    const hasPairLoadError = useCallback(() => {
      const tiktok = tiktokPlayerRef.current;
      const source = sourcePlayerRef.current;
      if (!tiktok || !source) return true;
      return tiktok.hasLoadError() || source.hasLoadError();
    }, []);

    const warmupPair = useCallback(
      async (timeoutMs: number): Promise<boolean> => {
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
        if (hasPairLoadError()) {
          return false;
        }

        // waitUntilReady resolves on timeout too — verify we actually reached
        // the required readyState. Without this, a timeout is treated as
        // "ready" which causes black/stalled playback.
        if (
          tiktok.getReadyState() < fastWatchMinReadyState ||
          source.getReadyState() < fastWatchMinReadyState
        ) {
          return false;
        }

        await Promise.all([tiktok.seekToStart(), source.seekToStart()]);
        return !hasPairLoadError();
      },
      [fastWatchMinReadyState, hasPairLoadError],
    );

    const recoverPairLoadOnce = useCallback(async (): Promise<boolean> => {
      const tiktok = tiktokPlayerRef.current;
      const source = sourcePlayerRef.current;
      if (!tiktok || !source) return false;

      await Promise.all([tiktok.retryLoad(), source.retryLoad()]);
      const recoveryTimeout = Math.max(fastWatchReadyTimeoutMs + 1500, 9000);
      return warmupPair(recoveryTimeout);
    }, [fastWatchReadyTimeoutMs, warmupPair]);

    // Sync play both videos simultaneously using refs
    // Two-phase: seek both first, then play together for precise sync
    const playBothFromStart = useCallback(async () => {
      if (!hasMatch) return;

      const tiktok = tiktokPlayerRef.current;
      const source = sourcePlayerRef.current;
      if (!tiktok || !source) {
        tiktok?.playFromStart();
        source?.playFromStart();
        return;
      }

      endedRef.current = { tiktok: false, source: false };
      if (primedForFastWatchRef.current) {
        primedForFastWatchRef.current = false;
        if (hasPairLoadError()) {
          loadFailureRef.current = true;
          return;
        }
        loadFailureRef.current = false;
        tiktok.play();
        source.play();
        return;
      }
      await Promise.all([
        tiktok.waitUntilReady({
          minReadyState: fastWatchMinReadyState,
          timeoutMs: fastWatchReadyTimeoutMs,
        }),
        source.waitUntilReady({
          minReadyState: fastWatchMinReadyState,
          timeoutMs: fastWatchReadyTimeoutMs,
        }),
      ]);
      if (hasPairLoadError()) {
        loadFailureRef.current = true;
        return;
      }
      await Promise.all([tiktok.seekToStart(), source.seekToStart()]);
      if (hasPairLoadError()) {
        loadFailureRef.current = true;
        return;
      }
      loadFailureRef.current = false;
      tiktok.play();
      source.play();
    }, [
      hasMatch,
      fastWatchMinReadyState,
      fastWatchReadyTimeoutMs,
      hasPairLoadError,
    ]);

    const prepareForAutoplay = useCallback(async () => {
      if (!hasMatch) return true;

      const tiktok = tiktokPlayerRef.current;
      const source = sourcePlayerRef.current;
      if (!tiktok || !source) {
        loadFailureRef.current = true;
        return false;
      }

      tiktok.forceLoad();
      source.forceLoad();

      // First attempt: normal preload path.
      let prepared = await warmupPair(fastWatchReadyTimeoutMs);
      // Recovery attempt: retry source loads once with cache-busting.
      if (!prepared) {
        prepared = await recoverPairLoadOnce();
      }
      if (!prepared) {
        loadFailureRef.current = true;
        primedForFastWatchRef.current = false;
        return false;
      }

      loadFailureRef.current = false;
      primedForFastWatchRef.current = true;
      return true;
    }, [hasMatch, fastWatchReadyTimeoutMs, warmupPair, recoverPairLoadOnce]);

    const releasePreload = useCallback(() => {
      primedForFastWatchRef.current = false;
      loadFailureRef.current = false;
      tiktokPlayerRef.current?.releaseLoad();
      sourcePlayerRef.current?.releaseLoad();
    }, []);

    const stop = useCallback(() => {
      primedForFastWatchRef.current = false;
      loadFailureRef.current = false;
      tiktokPlayerRef.current?.pause();
      sourcePlayerRef.current?.pause();
      if (pendingResolverRef.current) {
        pendingResolverRef.current();
        pendingResolverRef.current = null;
      }
    }, []);

    const onClipEnded = useCallback((player: "tiktok" | "source") => {
      endedRef.current[player] = true;
      if (endedRef.current.tiktok && endedRef.current.source) {
        if (pendingResolverRef.current) {
          pendingResolverRef.current();
          pendingResolverRef.current = null;
        }
      }
    }, []);

    const playBothAndWait = useCallback(async () => {
      if (!hasMatch) return;
      if (loadFailureRef.current) return;

      await playBothFromStart();
      if (loadFailureRef.current) return;

      await new Promise<void>((resolve) => {
        const finalize = () => {
          pendingResolverRef.current = null;
          window.clearTimeout(hardTimeoutId);
          window.clearInterval(stallGuardId);
          resolve();
        };

        if (endedRef.current.tiktok && endedRef.current.source) {
          resolve();
          return;
        }

        pendingResolverRef.current = resolve;
        const startedAt = Date.now();
        const stallGuardId = window.setInterval(() => {
          if (pendingResolverRef.current !== resolve) {
            window.clearInterval(stallGuardId);
            return;
          }

          const tiktok = tiktokPlayerRef.current;
          const source = sourcePlayerRef.current;
          if (!tiktok || !source) {
            loadFailureRef.current = true;
            finalize();
            return;
          }

          if (tiktok.hasLoadError() || source.hasLoadError()) {
            loadFailureRef.current = true;
            finalize();
            return;
          }

          // If playback did not actually start shortly after trigger, skip
          // the scene to preserve Fast Watch continuity.
          if (
            Date.now() - startedAt > 1400 &&
            !tiktok.isPlaying() &&
            !source.isPlaying()
          ) {
            loadFailureRef.current = true;
            finalize();
          }
        }, 200);

        // Fallback to avoid deadlock if browser misses an ended callback.
        const hardTimeoutId = window.setTimeout(() => {
          if (pendingResolverRef.current === resolve) {
            loadFailureRef.current = true;
            finalize();
          }
        }, 15000);
      });
    }, [hasMatch, playBothFromStart]);

    useImperativeHandle(
      ref,
      () => ({
        playBothAndWait,
        prepareForAutoplay,
        releasePreload,
        stop,
      }),
      [playBothAndWait, prepareForAutoplay, releasePreload, stop],
    );

    useEffect(() => {
      return () => {
        primedForFastWatchRef.current = false;
        loadFailureRef.current = false;
        if (pendingResolverRef.current) {
          pendingResolverRef.current();
          pendingResolverRef.current = null;
        }
      };
    }, []);

    return (
      <div
        className={cn(
          "bg-[hsl(var(--card))] rounded-lg p-4 space-y-4",
          isActive && "ring-2 ring-[hsl(var(--primary))]",
        )}
        data-scene-index={scene.index}
      >
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <h3 className="font-semibold">Scene {scene.index + 1}</h3>
            {match.was_no_match && !match.merged_from && (
              <span className="flex items-center gap-1 text-xs px-1.5 py-0.5 rounded bg-purple-500/10 text-purple-500 border border-purple-500/20">
                <Sparkles className="h-3 w-3" />
                manually set
              </span>
            )}
            {match.merged_from && (
              <span className="flex items-center gap-1 text-xs px-1.5 py-0.5 rounded bg-blue-500/10 text-blue-500 border border-blue-500/20">
                <Merge className="h-3 w-3" />
                Merged (was scenes{" "}
                {match.merged_from.map((i) => i + 1).join("+")})
              </span>
            )}
            {match.merged_from && onUndoMerge && (
              <button
                onClick={() => onUndoMerge(scene.index)}
                className="flex items-center gap-1 text-xs px-1.5 py-0.5 rounded hover:bg-[hsl(var(--muted))] text-[hsl(var(--muted-foreground))] transition-colors"
                title="Undo merge and restore original scenes"
              >
                <Undo2 className="h-3 w-3" />
                Undo
              </button>
            )}
          </div>
          {hasMatchedScene ? (
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
            <div className="aspect-[9/16] bg-black rounded overflow-hidden flex items-center justify-center">
              {tiktokVideoUrl ? (
                <MatchesClipPlayer
                  ref={tiktokPlayerRef}
                  src={tiktokVideoUrl}
                  onClipEnded={() => onClipEnded("tiktok")}
                  playbackRate={playbackRate}
                  controls
                  preloadMode={preloadMode}
                  disableInteraction={controlsDisabled}
                  className="w-full h-full"
                />
              ) : (
                <div className="text-xs text-[hsl(var(--muted-foreground))]">
                  Preparing clip...
                </div>
              )}
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
              {pendingUpdate ? (
                <div className="flex flex-col items-center gap-2 text-[hsl(var(--muted-foreground))] p-4">
                  <Loader2 className="h-6 w-6 animate-spin text-[hsl(var(--primary))]" />
                  <p className="text-xs text-center">{pendingUpdate.message}</p>
                </div>
              ) : hasMatch && sourceVideoUrl ? (
                <MatchesClipPlayer
                  ref={sourcePlayerRef}
                  src={sourceVideoUrl}
                  onClipEnded={() => onClipEnded("source")}
                  playbackRate={playbackRate}
                  controls
                  preloadMode={preloadMode}
                  disableInteraction={controlsDisabled}
                  className="w-full h-full"
                />
              ) : hasMatchedScene && !sourceVideoUrl ? (
                <div className="text-xs text-[hsl(var(--muted-foreground))]">
                  Preparing source clip...
                </div>
              ) : (
                <div className="flex flex-col items-center gap-2 text-[hsl(var(--muted-foreground))] p-4">
                  <AlertCircle className="h-8 w-8 text-amber-500 mb-2" />
                  <p className="text-xs text-center">
                    No automatic match found
                  </p>
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
            {hasMatchedScene ? (
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
            {warningMessage && (
              <p className="text-xs text-amber-500 mt-1">{warningMessage}</p>
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
              onClick={playBothFromStart}
              disabled={Boolean(pendingUpdate)}
            >
              <Play className="h-4 w-4 mr-2" />
              Play Both
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setShowManualModal(true)}
              disabled={Boolean(pendingUpdate)}
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
  },
);

const MemoizedMatchCard = memo(MatchCard);

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
  const [playbackPreparing, setPlaybackPreparing] = useState(false);
  const [playbackProgress, setPlaybackProgress] =
    useState<PlaybackPrepareProgress | null>(null);
  const [playbackManifest, setPlaybackManifest] =
    useState<MatchesPlaybackManifest | null>(null);
  const [playbackError, setPlaybackError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pendingSceneUpdates, setPendingSceneUpdates] = useState<
    Record<number, { phase: "saving" | "preparing"; message: string }>
  >({});
  const [sceneWarnings, setSceneWarnings] = useState<Record<number, string>>(
    {},
  );
  const [toast, setToast] = useState<{
    id: number;
    type: "error" | "warning" | "success";
    message: string;
  } | null>(null);
  const [mergeContinuous, setMergeContinuous] = useState(true);
  const [skipUiEnabled, setSkipUiEnabled] = useState(false);
  const skipUiEnabledRef = useRef(false);
  const autoMatchAttemptedRef = useRef(false);
  const [activeSceneIndex, setActiveSceneIndex] = useState(-1);
  const [autoScroll, setAutoScroll] = useState(true);
  const [fastWatchPlaying, setFastWatchPlaying] = useState(false);
  const [playbackRate, setPlaybackRate] = useState(1);
  const sceneRefs = useRef<Map<number, HTMLDivElement>>(new Map());
  const cardRefs = useRef<Map<number, MatchCardHandle>>(new Map());
  const autoplayTokenRef = useRef(0);
  const preparedScenesRef = useRef<Set<number>>(new Set());
  const failedPreparedScenesRef = useRef<Set<number>>(new Set());
  const preparingScenesRef = useRef<Map<number, Promise<void>>>(new Map());
  const prefetchQueueRef = useRef<Promise<void>>(Promise.resolve());
  const preparePlaybackInFlightRef = useRef<Promise<void> | null>(null);
  const autoScrollRef = useRef(true);
  const toastCounterRef = useRef(0);
  autoScrollRef.current = autoScroll;

  const fastWatchPrefetchAhead = useMemo(() => {
    // At high speeds clips are very short (~0.3-0.6s effective).
    // More prefetch = more concurrent connections which saturate the pool.
    // 2 ahead is optimal: leaves time to load while current plays.
    if (playbackRate >= 2) return 2;
    return 1;
  }, [playbackRate]);

  const clearFastWatchBuffers = useCallback(() => {
    preparedScenesRef.current.clear();
    failedPreparedScenesRef.current.clear();
    preparingScenesRef.current.clear();
    prefetchQueueRef.current = Promise.resolve();
  }, []);

  const showToast = useCallback(
    (type: "error" | "warning" | "success", message: string) => {
      toastCounterRef.current += 1;
      setToast({
        id: toastCounterRef.current,
        type,
        message,
      });
    },
    [],
  );

  useEffect(() => {
    if (!toast) return;
    const timeout = window.setTimeout(() => {
      setToast((current) => (current?.id === toast.id ? null : current));
    }, 4500);
    return () => {
      window.clearTimeout(timeout);
    };
  }, [toast]);

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
          // Fast Watch should continue even if one card fails to preload.
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
    (orderedScenes: Scene[], currentOffset: number) => {
      // Always release scenes behind — they have no utility in Fast Watch
      // and consume connection pool slots.
      const keepBehind = 0;
      const keepStart = Math.max(0, currentOffset - keepBehind);
      const keepEnd = Math.min(
        orderedScenes.length - 1,
        currentOffset + fastWatchPrefetchAhead,
      );
      const keepSceneIndices = new Set<number>();

      for (let i = keepStart; i <= keepEnd; i += 1) {
        const scene = orderedScenes[i];
        if (scene) {
          keepSceneIndices.add(scene.index);
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
    const el = sceneRefs.current.get(sceneIndex);
    if (el) {
      el.scrollIntoView({ behavior: "instant", block: "center" });
    }
  }, []);

  const playFastWatchFromScene = useCallback(
    async (startSceneIndex: number) => {
      if (!scenes.length) return;

      const token = autoplayTokenRef.current + 1;
      autoplayTokenRef.current = token;
      setFastWatchPlaying(true);
      clearFastWatchBuffers();

      const startPos = scenes.findIndex(
        (scene) => scene.index === startSceneIndex,
      );
      const orderedScenes = startPos >= 0 ? scenes.slice(startPos) : scenes;
      const initialWindow = orderedScenes.slice(0, fastWatchPrefetchAhead + 1);
      const mandatoryWarmup = initialWindow.slice(
        0,
        Math.min(2, initialWindow.length),
      );
      for (const scene of mandatoryWarmup) {
        await ensureScenePrepared(scene.index, token);
        if (autoplayTokenRef.current !== token) {
          return;
        }
      }
      for (const scene of initialWindow.slice(mandatoryWarmup.length)) {
        scheduleScenePreparation(scene.index, token);
      }
      if (autoplayTokenRef.current !== token) {
        return;
      }

      for (let i = 0; i < orderedScenes.length; i += 1) {
        if (autoplayTokenRef.current !== token) {
          return;
        }

        const scene = orderedScenes[i];
        const card = cardRefs.current.get(scene.index);
        if (!card) continue;

        await ensureScenePrepared(scene.index, token);
        if (autoplayTokenRef.current !== token) {
          return;
        }
        if (failedPreparedScenesRef.current.has(scene.index)) {
          setActiveSceneIndex(scene.index);
          scrollToScene(scene.index);
          releaseOutsideFastWatchWindow(orderedScenes, i + 1);
          continue;
        }

        for (let offset = 1; offset <= fastWatchPrefetchAhead; offset += 1) {
          const nextScene = orderedScenes[i + offset];
          if (!nextScene) break;
          scheduleScenePreparation(nextScene.index, token);
        }

        setActiveSceneIndex(scene.index);
        const playback = card.playBothAndWait();
        scrollToScene(scene.index);
        await playback;
        releaseOutsideFastWatchWindow(orderedScenes, i + 1);
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
      scenes,
      scrollToScene,
      fastWatchPrefetchAhead,
      ensureScenePrepared,
      scheduleScenePreparation,
      releaseOutsideFastWatchWindow,
      clearFastWatchBuffers,
    ],
  );

  const patchPlaybackSceneAsset = useCallback(
    (sceneIndex: number, sceneAsset: ScenePlaybackSceneAsset) => {
      setPlaybackManifest((prev) => {
        if (!prev) return prev;
        const scenesPayload = prev.scenes.map((asset) =>
          asset.scene_index === sceneIndex ? sceneAsset : asset,
        );
        return {
          ...prev,
          scenes: scenesPayload,
          scene_status: {
            ...(prev.scene_status || {}),
            [String(sceneIndex)]: sceneAsset.status,
          },
        };
      });
    },
    [],
  );

  const preparePlaybackClips = useCallback(
    async (force = false) => {
      if (!projectId) return;

      const pending = preparePlaybackInFlightRef.current;
      if (pending) {
        if (!force) {
          await pending;
          return;
        }
        await pending;
      }

      const run = (async () => {
        setPlaybackPreparing(true);
        setPlaybackError(null);
        setPlaybackProgress({
          status: "scanning",
          progress: 0,
          message: "Preparing playback clips...",
        });

        try {
          const response = await api.prepareMatchesPlayback(projectId, force);
          const lastEvent = await readSSEStream<PlaybackPrepareProgress>(
            response,
            (data) => {
              setPlaybackProgress(data);
              if (data.status === "complete" && data.manifest) {
                setPlaybackManifest(data.manifest);
              }
            },
          );

          if (!lastEvent || lastEvent.status !== "complete") {
            const manifest = await api.getMatchesPlaybackManifest(projectId);
            if (!manifest.ready) {
              throw new Error(
                lastEvent?.error ||
                  "Playback preparation did not complete successfully",
              );
            }
            setPlaybackManifest(manifest);
            setPlaybackProgress({
              status: "complete",
              progress: 1,
              message: "Playback clips ready",
            });
            return;
          }

          if (lastEvent.manifest) {
            setPlaybackManifest(lastEvent.manifest);
            return;
          }

          const manifest = await api.getMatchesPlaybackManifest(projectId);
          if (!manifest.ready) {
            throw new Error(
              lastEvent.error || "Playback manifest is not ready after prepare",
            );
          }
          setPlaybackManifest(manifest);
        } catch (err) {
          setPlaybackManifest(null);
          setPlaybackError((err as Error).message);
        } finally {
          setPlaybackPreparing(false);
        }
      })();

      preparePlaybackInFlightRef.current = run;
      try {
        await run;
      } finally {
        if (preparePlaybackInFlightRef.current === run) {
          preparePlaybackInFlightRef.current = null;
        }
      }
    },
    [projectId],
  );

  // Load data
  useEffect(() => {
    if (!projectId) return;

    const loadData = async () => {
      setLoading(true);
      try {
        setPendingSceneUpdates({});
        setSceneWarnings({});
        setToast(null);
        await loadProject(projectId);
        await loadScenes(projectId);
        const { matches: loadedMatches } = await api.getMatches(projectId);
        // Track which scenes were initially "no match found"
        const matchesWithTracking = loadedMatches.map((m) => ({
          ...m,
          was_no_match: m.was_no_match ?? (m.confidence === 0 && !m.episode),
        }));
        setMatches(matchesWithTracking);
        if (matchesWithTracking.length > 0) {
          void preparePlaybackClips(false);
        } else {
          setPlaybackManifest(null);
          setPlaybackProgress(null);
          setPlaybackError(null);
        }
        // Load available episodes for manual matching
        const { episodes: loadedEpisodes } = await api.getEpisodes(projectId);
        setEpisodes(loadedEpisodes);
        // Check if scene UI skip is enabled (auto-match only)
        try {
          const scenesConfig = await api.getScenesConfig(projectId);
          const skip = Boolean(scenesConfig.skip_ui_enabled);
          setSkipUiEnabled(skip);
          skipUiEnabledRef.current = skip;
        } catch {
          setSkipUiEnabled(false);
          skipUiEnabledRef.current = false;
        }
      } catch (err) {
        setError((err as Error).message);
      } finally {
        setLoading(false);
      }
    };

    loadData();
  }, [projectId, loadProject, loadScenes, preparePlaybackClips]);

  useEffect(() => {
    if (scenes.length === 0) {
      setActiveSceneIndex(-1);
      return;
    }

    setActiveSceneIndex((prev) => {
      if (prev >= 0 && scenes.some((scene) => scene.index === prev)) {
        return prev;
      }
      return scenes[0].index;
    });
  }, [scenes]);

  useEffect(() => {
    if (matches.length > 0) return;
    setPlaybackManifest(null);
    setPlaybackProgress(null);
    setPlaybackError(null);
  }, [matches.length]);

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

  const handleFindMatches = useCallback(async () => {
    if (!projectId) return;

    stopFastWatch();
    setPendingSceneUpdates({});
    setSceneWarnings({});
    setToast(null);
    setMatching(true);
    setPlaybackManifest(null);
    setPlaybackProgress(null);
    setPlaybackError(null);
    setMatchProgress({
      status: "starting",
      progress: 0,
      message: "Starting match search...",
    });

    try {
      const response = await api.findMatches(
        projectId,
        undefined,
        mergeContinuous,
      );
      let completeHandled = false;

      await readSSEStream<MatchProgress>(response, async (data) => {
        setMatchProgress(data);

        if (data.status === "complete" && data.matches) {
          if (completeHandled) {
            return;
          }
          completeHandled = true;
          const matchesData = data.matches as unknown as {
            matches: SceneMatch[];
          };
          const matchesWithTracking = (matchesData.matches || []).map((m) => ({
            ...m,
            was_no_match: m.was_no_match ?? (m.confidence === 0 && !m.episode),
          }));
          setMatches(matchesWithTracking);
          await loadScenes(projectId);
          if (matchesWithTracking.length > 0) {
            await preparePlaybackClips(true);
          } else {
            setPlaybackManifest(null);
            setPlaybackProgress(null);
            setPlaybackError(null);
          }

          // Keep /matches visible so the user can manually continue.
        }
      });
    } catch (err) {
      setError((err as Error).message);
      setMatchProgress(null);
    } finally {
      setMatching(false);
    }
  }, [
    projectId,
    mergeContinuous,
    loadScenes,
    stopFastWatch,
    preparePlaybackClips,
  ]);

  // Auto-start matching when skipUiEnabled and no matches exist
  useEffect(() => {
    if (
      !projectId ||
      loading ||
      matching ||
      !skipUiEnabled ||
      matches.length > 0 ||
      autoMatchAttemptedRef.current
    ) {
      return;
    }
    autoMatchAttemptedRef.current = true;
    handleFindMatches();
  }, [
    projectId,
    loading,
    matching,
    skipUiEnabled,
    matches.length,
    handleFindMatches,
  ]);

  const handleManualMatch = useCallback(
    async (
      sceneIndex: number,
      episode: string,
      startTime: number,
      endTime: number,
      meta: ManualMatchSaveMeta,
    ) => {
      if (!projectId) return;

      const previousMatch = matches.find((m) => m.scene_index === sceneIndex) || null;
      if (!previousMatch) return;
      const previousPlaybackAsset =
        playbackManifest?.scenes?.find(
          (asset) => asset.scene_index === sceneIndex,
        ) ?? null;
      const previousWarning = sceneWarnings[sceneIndex] || null;
      const targetScene = scenes.find((scene) => scene.index === sceneIndex) || null;
      const targetSceneDuration = targetScene
        ? Math.max(0, targetScene.end_time - targetScene.start_time)
        : Math.max(0, endTime - startTime);
      const computedSpeedRatio =
        meta.sourceDuration > 0 ? targetSceneDuration / meta.sourceDuration : 1;
      const optimisticSpeedRatio = Number.isFinite(computedSpeedRatio)
        ? computedSpeedRatio
        : previousMatch.speed_ratio;

      const optimisticMatch: SceneMatch = {
        ...previousMatch,
        episode,
        start_time: startTime,
        end_time: endTime,
        confidence:
          previousMatch.confidence > 0
            ? previousMatch.confidence
            : Math.max(previousMatch.confidence, 0.01),
        speed_ratio: optimisticSpeedRatio,
        confirmed: true,
      };

      const warningMessage = meta.anomalous
        ? "Long source clip; preparation may take longer."
        : "";

      setPendingSceneUpdates((prev) => ({
        ...prev,
        [sceneIndex]: {
          phase: "saving",
          message: "Saving manual match...",
        },
      }));
      setMatches((prev) =>
        prev.map((match) =>
          match.scene_index === sceneIndex ? optimisticMatch : match,
        ),
      );
      setSceneWarnings((prev) => {
        const next = { ...prev };
        if (warningMessage) {
          next[sceneIndex] = warningMessage;
        } else {
          delete next[sceneIndex];
        }
        return next;
      });

      try {
        stopFastWatch();
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
          prev.map((m) => {
            if (m.scene_index === sceneIndex) {
              const wasNoMatch = m.confidence === 0 && !m.episode;
              return {
                ...updatedMatch,
                was_no_match: m.was_no_match || wasNoMatch,
              };
            }
            return m;
          }),
        );

        setPendingSceneUpdates((prev) => ({
          ...prev,
          [sceneIndex]: {
            phase: "preparing",
            message: "Preparing source clip...",
          },
        }));

        const response = await api.prepareMatchesPlaybackScene(
          projectId,
          sceneIndex,
          false,
        );
        const lastEvent = await readSSEStream<PlaybackPrepareProgress>(
          response,
          (data) => {
            if (data.status === "encoding_source") {
              setPendingSceneUpdates((prev) => ({
                ...prev,
                [sceneIndex]: {
                  phase: "preparing",
                  message: data.message || "Preparing source clip...",
                },
              }));
            }
            if (data.status === "complete" && data.scene_asset) {
              patchPlaybackSceneAsset(sceneIndex, data.scene_asset);
            }
          },
        );

        const completedAsset =
          lastEvent?.scene_asset ||
          lastEvent?.manifest?.scenes?.find(
            (asset) => asset.scene_index === sceneIndex,
          ) ||
          null;
        if (!completedAsset) {
          const refreshedManifest = await api.getMatchesPlaybackManifest(projectId);
          const refreshedAsset =
            refreshedManifest.scenes.find(
              (asset) => asset.scene_index === sceneIndex,
            ) || null;
          if (!refreshedAsset) {
            throw new Error("Scene playback asset missing after preparation");
          }
          patchPlaybackSceneAsset(sceneIndex, refreshedAsset);
        } else {
          patchPlaybackSceneAsset(sceneIndex, completedAsset);
        }

        setPlaybackError(null);
        setPendingSceneUpdates((prev) => {
          const next = { ...prev };
          delete next[sceneIndex];
          return next;
        });
      } catch {
        setPendingSceneUpdates((prev) => {
          const next = { ...prev };
          delete next[sceneIndex];
          return next;
        });
        setMatches((prev) =>
          prev.map((match) =>
            match.scene_index === sceneIndex && previousMatch
              ? previousMatch
              : match,
          ),
        );
        if (previousPlaybackAsset) {
          patchPlaybackSceneAsset(sceneIndex, previousPlaybackAsset);
        }
        setSceneWarnings((prev) => {
          const next = { ...prev };
          if (previousWarning) {
            next[sceneIndex] = previousWarning;
          } else {
            delete next[sceneIndex];
          }
          return next;
        });
        showToast(
          "error",
          `Manual save failed for scene ${sceneIndex + 1}. Previous match restored.`,
        );
        return;
      }
    },
    [
      projectId,
      matches,
      playbackManifest,
      sceneWarnings,
      scenes,
      stopFastWatch,
      patchPlaybackSceneAsset,
      showToast,
    ],
  );

  const handleBackToScenes = () => {
    if (projectId) {
      navigate(`/project/${projectId}/scenes`);
    }
  };

  const handleRecomputeMatches = async () => {
    // Clear existing matches and recompute
    stopFastWatch();
    setPendingSceneUpdates({});
    setSceneWarnings({});
    setMatches([]);
    setPlaybackManifest(null);
    setPlaybackProgress(null);
    setPlaybackError(null);
    await handleFindMatches();
  };

  const handleAutoFillBestCandidates = useCallback(async () => {
    if (!projectId) return;

    try {
      stopFastWatch();
      setPendingSceneUpdates({});
      // Find all scenes with no match
      const noMatchScenes = matches.filter(
        (m) => m.confidence === 0 && !m.episode && m.alternatives?.length > 0,
      );

      if (noMatchScenes.length === 0) return;

      // Build one batch payload and persist all updates in a single request.
      const updates: Array<{
        scene_index: number;
        episode: string;
        start_time: number;
        end_time: number;
        confirmed: boolean;
      }> = [];
      const bestByScene = new Map<
        number,
        {
          episode: string;
          start_time: number;
          end_time: number;
          confidence: number;
          speed_ratio: number;
        }
      >();

      for (const match of noMatchScenes) {
        const bestAlternative = [...match.alternatives].sort(
          (a, b) => b.confidence - a.confidence,
        )[0];

        updates.push({
          scene_index: match.scene_index,
          episode: bestAlternative.episode,
          start_time: bestAlternative.start_time,
          end_time: bestAlternative.end_time,
          confirmed: true,
        });
        bestByScene.set(match.scene_index, {
          episode: bestAlternative.episode,
          start_time: bestAlternative.start_time,
          end_time: bestAlternative.end_time,
          confidence: bestAlternative.confidence,
          speed_ratio: bestAlternative.speed_ratio,
        });
      }

      await api.updateMatchesBatch(projectId, updates);

      // Update local state once to avoid N re-renders.
      setMatches((prev) =>
        prev.map((m) => {
          const best = bestByScene.get(m.scene_index);
          if (!best) return m;
          return {
            ...m,
            episode: best.episode,
            start_time: best.start_time,
            end_time: best.end_time,
            confidence: best.confidence,
            speed_ratio: best.speed_ratio,
            confirmed: true,
            was_no_match: true,
          };
        }),
      );
      await preparePlaybackClips(true);
      setSceneWarnings({});
    } catch (err) {
      setError((err as Error).message);
    }
  }, [projectId, matches, stopFastWatch, preparePlaybackClips]);

  const handleUndoMerge = useCallback(
    async (sceneIndex: number) => {
      if (!projectId) return;
      try {
        stopFastWatch();
        setPendingSceneUpdates({});
        setSceneWarnings({});
        const result = await api.undoMerge(projectId, sceneIndex);
        // Reload scenes and matches after undo
        await loadScenes(projectId);
        const matchesWithTracking = result.matches.map((m) => ({
          ...m,
          was_no_match: m.was_no_match ?? (m.confidence === 0 && !m.episode),
        }));
        setMatches(matchesWithTracking);
        if (matchesWithTracking.length > 0) {
          await preparePlaybackClips(true);
        } else {
          setPlaybackManifest(null);
          setPlaybackProgress(null);
          setPlaybackError(null);
        }
      } catch (err) {
        setError((err as Error).message);
      }
    },
    [projectId, loadScenes, stopFastWatch, preparePlaybackClips],
  );

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

  const activeScenePosition = useMemo(() => {
    if (scenes.length === 0) return 0;
    const index = scenes.findIndex((scene) => scene.index === activeSceneIndex);
    return index >= 0 ? index : 0;
  }, [scenes, activeSceneIndex]);

  const hasAnyMatch = useMemo(
    () => matches.some((match) => match.confidence > 0 && match.episode),
    [matches],
  );

  const matchesBySceneIndex = useMemo(() => {
    return new Map(matches.map((match) => [match.scene_index, match]));
  }, [matches]);

  const isPlaybackReady = Boolean(playbackManifest?.ready);

  const playbackBySceneIndex = useMemo(() => {
    if (!playbackManifest?.scenes) return new Map<number, ScenePlaybackSceneAsset>();
    return new Map(
      playbackManifest.scenes.map((asset) => [asset.scene_index, asset]),
    );
  }, [playbackManifest]);

  const handleToggleFastWatch = useCallback(() => {
    if (fastWatchPlaying) {
      stopFastWatch();
      return;
    }
    if (!isPlaybackReady) {
      return;
    }

    const startSceneIndex =
      activeSceneIndex >= 0 ? activeSceneIndex : scenes[0]?.index;
    if (startSceneIndex === undefined) return;

    void playFastWatchFromScene(startSceneIndex);
  }, [
    fastWatchPlaying,
    stopFastWatch,
    activeSceneIndex,
    scenes,
    playFastWatchFromScene,
    isPlaybackReady,
  ]);

  const handleTimelineSeek = useCallback(
    (position: number) => {
      const targetScene = scenes[position];
      if (!targetScene) return;

      setActiveSceneIndex(targetScene.index);
      scrollToScene(targetScene.index, true);

      if (fastWatchPlaying) {
        void playFastWatchFromScene(targetScene.index);
      }
    },
    [scenes, scrollToScene, fastWatchPlaying, playFastWatchFromScene],
  );

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
    <div className="min-h-screen p-4 pb-28">
      <div className="max-w-4xl mx-auto space-y-6">
        <header className="space-y-4">
          <div className="flex items-center justify-between">
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
            <div className="text-right">
              <div className="text-sm text-[hsl(var(--muted-foreground))]">
                {confirmedCount} / {totalCount} matched
              </div>
            </div>
          </div>

          {matches.length > 0 && (
            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={handleRecomputeMatches}
                  disabled={matching}
                  title="Re-run the matching algorithm"
                >
                  <RefreshCw
                    className={`h-4 w-4 mr-1 ${matching ? "animate-spin" : ""}`}
                  />
                  Recompute
                </Button>
                <label className="flex items-center gap-1.5 text-xs text-[hsl(var(--muted-foreground))]">
                  <input
                    type="checkbox"
                    checked={mergeContinuous}
                    onChange={(e) => setMergeContinuous(e.target.checked)}
                    className="rounded"
                    disabled={matching}
                  />
                  Merge continuous
                </label>
                {(() => {
                  const noMatchCount = matches.filter(
                    (m) =>
                      m.confidence === 0 &&
                      !m.episode &&
                      m.alternatives?.length > 0,
                  ).length;
                  return (
                    noMatchCount > 0 && (
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={handleAutoFillBestCandidates}
                        disabled={matching}
                        className="border-purple-500/30 hover:bg-purple-500/10 text-purple-500 hover:text-purple-400"
                        title={`Auto-fill ${noMatchCount} unmatched scene${noMatchCount !== 1 ? "s" : ""} with best candidate`}
                      >
                        <Wand2 className="h-4 w-4 mr-1" />
                        <span className="text-xs">Fill {noMatchCount}</span>
                      </Button>
                    )
                  );
                })()}
              </div>
              <Button onClick={handleContinue} disabled={!allConfirmed}>
                Continue to Transcription
              </Button>
            </div>
          )}
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
            <div className="flex flex-col items-center gap-3">
              <Button onClick={handleFindMatches} disabled={!projectId}>
                <Search className="h-4 w-4 mr-2" />
                Find Matches
              </Button>
              <label className="flex items-center gap-2 text-sm text-[hsl(var(--muted-foreground))]">
                <input
                  type="checkbox"
                  checked={mergeContinuous}
                  onChange={(e) => setMergeContinuous(e.target.checked)}
                  className="rounded"
                />
                Merge continuous scenes
              </label>
            </div>
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

        {/* Playback warmup */}
        {matches.length > 0 && (playbackPreparing || !isPlaybackReady || playbackError) && (
          <div className="bg-[hsl(var(--card))] rounded-lg p-8 text-center space-y-4 border border-[hsl(var(--border))]">
            <Loader2 className="h-10 w-10 mx-auto animate-spin text-[hsl(var(--primary))]" />
            <div>
              <h2 className="text-lg font-semibold">Preparing Video Playback</h2>
              <p className="text-sm text-[hsl(var(--muted-foreground))]">
                {playbackProgress?.message || "Building browser-safe clips for all scenes..."}
              </p>
              {playbackProgress?.scene_index !== undefined && (
                <p className="text-xs text-[hsl(var(--muted-foreground))] mt-1">
                  Scene {playbackProgress.scene_index + 1} / {playbackProgress.total_scenes || scenes.length}
                </p>
              )}
              {playbackError && (
                <p className="text-xs text-[hsl(var(--destructive))] mt-2">
                  {playbackError}
                </p>
              )}
            </div>
            <div className="h-2 bg-[hsl(var(--muted))] rounded-full overflow-hidden max-w-md mx-auto">
              <div
                className="h-full bg-[hsl(var(--primary))] transition-all duration-300"
                style={{ width: `${Math.max(0, Math.min(1, playbackProgress?.progress ?? 0)) * 100}%` }}
              />
            </div>
            {playbackError && (
              <Button
                variant="outline"
                onClick={() => {
                  void preparePlaybackClips(true);
                }}
              >
                Retry Warmup
              </Button>
            )}
          </div>
        )}

        {/* Show matches */}
        {isPlaybackReady && (
          <div className="space-y-4">
          {scenes.map((scene, scenePosition) => {
            const match = matchesBySceneIndex.get(scene.index);
            if (!match) return null;

            return (
              <div
                key={scene.index}
                className="[content-visibility:auto] [contain-intrinsic-size:960px]"
                ref={(el) => {
                  if (el) sceneRefs.current.set(scene.index, el);
                  else sceneRefs.current.delete(scene.index);
                }}
              >
                <MemoizedMatchCard
                  ref={(card) => {
                    if (card) cardRefs.current.set(scene.index, card);
                    else cardRefs.current.delete(scene.index);
                  }}
                  scene={scene}
                  match={match}
                  projectId={projectId!}
                  episodes={episodes}
                  playbackAsset={playbackBySceneIndex.get(scene.index) ?? null}
                  isActive={activeSceneIndex === scene.index}
                  playbackRate={playbackRate}
                  controlsDisabled={fastWatchPlaying}
                  preloadMode={
                    Math.abs(scenePosition - activeScenePosition) <= 2
                      ? "auto"
                      : "metadata"
                  }
                  onManualMatch={handleManualMatch}
                  pendingUpdate={pendingSceneUpdates[scene.index] || null}
                  warningMessage={sceneWarnings[scene.index] || null}
                  onUndoMerge={handleUndoMerge}
                />
              </div>
            );
          })}
          </div>
        )}
      </div>

      {matches.length > 0 && scenes.length > 0 && isPlaybackReady && (
        <div className="fixed bottom-0 left-0 right-0 z-50 bg-[hsl(var(--card))] border-t border-[hsl(var(--border))] shadow-lg">
          <div className="max-w-4xl mx-auto px-4 py-2 space-y-2">
            <div className="flex items-center gap-3">
              <Button
                variant={fastWatchPlaying ? "default" : "outline"}
                size="sm"
                onClick={handleToggleFastWatch}
                disabled={!hasAnyMatch || playbackPreparing || !isPlaybackReady}
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

              <span className="text-xs text-[hsl(var(--muted-foreground))] min-w-[110px]">
                Scene {activeScenePosition + 1} / {scenes.length}
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
              max={Math.max(0, scenes.length - 1)}
              step={1}
              value={activeScenePosition}
              onChange={(e) => handleTimelineSeek(parseInt(e.target.value, 10))}
              className="w-full h-1 accent-[hsl(var(--primary))]"
              title="Timeline scroller"
            />
          </div>
        </div>
      )}

      {toast && (
        <div className="fixed top-4 right-4 z-[140]">
          <div
            className={cn(
              "rounded-md border px-4 py-3 text-sm shadow-lg max-w-sm",
              toast.type === "error" &&
                "border-red-500/30 bg-red-500/10 text-red-200",
              toast.type === "warning" &&
                "border-amber-500/30 bg-amber-500/10 text-amber-100",
              toast.type === "success" &&
                "border-emerald-500/30 bg-emerald-500/10 text-emerald-100",
            )}
          >
            {toast.message}
          </div>
        </div>
      )}
    </div>
  );
}
