import {
  useEffect,
  useState,
  useCallback,
  useMemo,
  useRef,
  forwardRef,
  useImperativeHandle,
} from "react";
import { useParams, useNavigate } from "react-router-dom";
import {
  Loader2,
  ArrowRight,
  Volume2,
  VolumeX,
  RotateCcw,
  Play,
  Pause,
} from "lucide-react";
import { Button } from "@/components/ui";
import { ClippedVideoPlayer } from "@/components/video";
import type { ClippedVideoPlayerHandle } from "@/components/video/ClippedVideoPlayer";
import { api } from "@/api/client";
import { cn, formatTime } from "@/utils";
import type {
  Transcription,
  RawSceneDetectionResult,
  SceneTranscription,
} from "@/types";

interface SceneValidationState {
  is_raw: boolean;
  text: string;
}

interface RawScenePageData {
  detection: RawSceneDetectionResult | null;
  transcription: Transcription | null;
}

type SceneCardPlayResult = "completed" | "load_error" | "audio_blocked";

interface SceneCardProps {
  scene: SceneTranscription;
  projectId: string;
  validation: SceneValidationState | undefined;
  isActive?: boolean;
  playbackRate?: number;
  controlsDisabled?: boolean;
  preloadMode?: "metadata" | "auto";
  onToggleRaw: () => void;
  onTextChange: (text: string) => void;
}

interface SceneCardHandle {
  playAndWait: () => Promise<SceneCardPlayResult>;
  prepareForAutoplay: () => Promise<boolean>;
  releasePreload: () => void;
  stop: () => void;
}

function buildValidationState(
  transcription: Transcription,
  detection: RawSceneDetectionResult,
): Record<number, SceneValidationState> {
  const rawCandidateIndices = new Set(
    detection.candidates.map((c) => c.scene_index),
  );

  const initial: Record<number, SceneValidationState> = {};
  for (const scene of transcription.scenes) {
    initial[scene.scene_index] = {
      // Source of truth is persisted transcription state.
      is_raw: scene.is_raw ?? rawCandidateIndices.has(scene.scene_index),
      text: scene.text,
    };
  }

  return initial;
}

const SceneCard = forwardRef<SceneCardHandle, SceneCardProps>(function SceneCard(
  {
    scene,
    projectId,
    validation,
    isActive = false,
    playbackRate = 1,
    controlsDisabled = false,
    preloadMode = "metadata",
    onToggleRaw,
    onTextChange,
  },
  ref,
) {
  const videoPlayerRef = useRef<ClippedVideoPlayerHandle>(null);
  const pendingResolverRef = useRef<
    ((result: SceneCardPlayResult) => void) | null
  >(null);
  const pendingTimeoutRef = useRef<number | null>(null);
  const startGuardTimeoutRef = useRef<number | null>(null);
  const endedRef = useRef(false);

  const videoUrl = api.getVideoUrl(projectId);
  const isRaw = validation?.is_raw ?? scene.is_raw;
  const sceneDuration = scene.end_time - scene.start_time;
  const fastWatchMinReadyState =
    playbackRate >= 8
      ? HTMLMediaElement.HAVE_ENOUGH_DATA
      : playbackRate >= 3
        ? HTMLMediaElement.HAVE_FUTURE_DATA
        : HTMLMediaElement.HAVE_CURRENT_DATA;
  const fastWatchReadyTimeoutMs =
    playbackRate >= 8 ? 12000 : playbackRate >= 4 ? 9000 : 7000;

  const resolvePendingPlayback = useCallback(
    (result: SceneCardPlayResult = "completed") => {
      if (pendingTimeoutRef.current !== null) {
        window.clearTimeout(pendingTimeoutRef.current);
        pendingTimeoutRef.current = null;
      }
      if (startGuardTimeoutRef.current !== null) {
        window.clearTimeout(startGuardTimeoutRef.current);
        startGuardTimeoutRef.current = null;
      }
      const resolver = pendingResolverRef.current;
      pendingResolverRef.current = null;
      resolver?.(result);
    },
    [],
  );

  const handleClipEnded = useCallback(() => {
    endedRef.current = true;
    resolvePendingPlayback("completed");
  }, [resolvePendingPlayback]);

  const warmupPlayer = useCallback(
    async (timeoutMs: number): Promise<boolean> => {
      const player = videoPlayerRef.current;
      if (!player) return false;

      await player.waitUntilReady({
        minReadyState: fastWatchMinReadyState,
        timeoutMs,
      });

      if (player.hasLoadError()) {
        return false;
      }
      if (player.getReadyState() < fastWatchMinReadyState) {
        return false;
      }

      await player.seekToStart();
      return !player.hasLoadError();
    },
    [fastWatchMinReadyState],
  );

  const recoverLoadOnce = useCallback(async (): Promise<boolean> => {
    const player = videoPlayerRef.current;
    if (!player) return false;

    await player.retryLoad();
    const recoveryTimeout = Math.max(fastWatchReadyTimeoutMs + 1500, 9000);
    return warmupPlayer(recoveryTimeout);
  }, [fastWatchReadyTimeoutMs, warmupPlayer]);

  const playAndWait = useCallback(async (): Promise<SceneCardPlayResult> => {
    const player = videoPlayerRef.current;
    if (!player) {
      return "load_error";
    }

    resolvePendingPlayback();
    endedRef.current = false;

    if (player.hasLoadError()) {
      const recovered = await recoverLoadOnce();
      if (!recovered) {
        return "load_error";
      }
    }

    await player.seekToStart();

    return new Promise<SceneCardPlayResult>((resolve) => {
      pendingResolverRef.current = resolve;

      const clipDurationMs =
        (Math.max(scene.end_time - scene.start_time, 0.1) /
          Math.max(playbackRate, 0.1)) *
        1000;
      pendingTimeoutRef.current = window.setTimeout(() => {
        resolvePendingPlayback("load_error");
      }, clipDurationMs + 6000);

      startGuardTimeoutRef.current = window.setTimeout(() => {
        const currentPlayer = videoPlayerRef.current;
        if (!currentPlayer) {
          resolvePendingPlayback("load_error");
          return;
        }

        if (!currentPlayer.isPlaying() && !endedRef.current) {
          resolvePendingPlayback("audio_blocked");
        }
      }, 1500);

      void player
        .playChecked()
        .then((started) => {
          if (!started) {
            resolvePendingPlayback("audio_blocked");
          }
        })
        .catch(() => {
          resolvePendingPlayback("load_error");
        });
    });
  }, [
    playbackRate,
    recoverLoadOnce,
    resolvePendingPlayback,
    scene.end_time,
    scene.start_time,
  ]);

  useImperativeHandle(
    ref,
    () => ({
      playAndWait,
      prepareForAutoplay: async () => {
        const player = videoPlayerRef.current;
        if (!player) return false;

        player.forceLoad();
        const warmed = await warmupPlayer(fastWatchReadyTimeoutMs);
        if (warmed) {
          return true;
        }

        return recoverLoadOnce();
      },
      releasePreload: () => {
        videoPlayerRef.current?.releaseLoad();
      },
      stop: () => {
        videoPlayerRef.current?.pause();
        resolvePendingPlayback();
      },
    }),
    [
      fastWatchReadyTimeoutMs,
      playAndWait,
      recoverLoadOnce,
      resolvePendingPlayback,
      warmupPlayer,
    ],
  );

  useEffect(() => {
    return () => {
      resolvePendingPlayback();
    };
  }, [resolvePendingPlayback]);

  return (
    <div
      className={cn(
        "bg-[hsl(var(--card))] rounded-lg p-4",
        isRaw ? "ring-2 ring-amber-500/50" : "ring-2 ring-green-500/50",
        isActive && "ring-[hsl(var(--primary))]",
      )}
    >
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <span className="font-medium">Scene {scene.scene_index + 1}</span>
          <span
            className={cn(
              "text-xs px-2 py-0.5 rounded-full font-medium",
              isRaw
                ? "bg-amber-500/20 text-amber-400"
                : "bg-green-500/20 text-green-400",
            )}
          >
            {isRaw ? (
              <span className="flex items-center gap-1">
                <Volume2 className="h-3 w-3" />
                RAW
              </span>
            ) : (
              <span className="flex items-center gap-1">
                <VolumeX className="h-3 w-3" />
                TTS
              </span>
            )}
          </span>
        </div>
        <span className="text-xs text-[hsl(var(--muted-foreground))]">
          {formatTime(scene.start_time)} - {formatTime(scene.end_time)} (
          {formatTime(sceneDuration)})
        </span>
      </div>

      <div className="grid grid-cols-[180px_1fr] gap-4">
        <div className="aspect-[9/16] bg-black rounded overflow-hidden">
          <ClippedVideoPlayer
            ref={videoPlayerRef}
            src={videoUrl}
            startTime={scene.start_time}
            endTime={scene.end_time}
            className={cn("w-full h-full", controlsDisabled && "pointer-events-none")}
            muted={false}
            playbackRate={playbackRate}
            eager={preloadMode === "auto"}
            onClipEnded={handleClipEnded}
          />
        </div>

        <div className="flex flex-col gap-3">
          <textarea
            value={validation?.text ?? scene.text}
            onChange={(e) => onTextChange(e.target.value)}
            className="flex-1 w-full min-h-[120px] p-3 rounded-md border border-[hsl(var(--input))] bg-transparent resize-y text-sm"
            placeholder={
              isRaw
                ? "Raw scene - no TTS text"
                : "No transcription for this scene"
            }
            disabled={isRaw}
          />

          <div className="flex gap-2">
            <Button
              variant={isRaw ? "default" : "outline"}
              size="sm"
              onClick={isRaw ? undefined : onToggleRaw}
              className={isRaw ? "pointer-events-none" : ""}
            >
              <Volume2 className="h-3.5 w-3.5 mr-1.5" />
              Keep as Raw
            </Button>
            <Button
              variant={!isRaw ? "default" : "outline"}
              size="sm"
              onClick={!isRaw ? undefined : onToggleRaw}
              className={!isRaw ? "pointer-events-none" : ""}
            >
              <VolumeX className="h-3.5 w-3.5 mr-1.5" />
              Mark as TTS
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
});

export function RawSceneValidationPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const navigate = useNavigate();

  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fastWatchError, setFastWatchError] = useState<string | null>(null);
  const [detection, setDetection] = useState<RawSceneDetectionResult | null>(
    null,
  );
  const [transcription, setTranscription] = useState<Transcription | null>(
    null,
  );
  const [validations, setValidations] = useState<
    Record<number, SceneValidationState>
  >({});
  const [activeSceneIndex, setActiveSceneIndex] = useState(-1);
  const [autoScroll, setAutoScroll] = useState(true);
  const [fastWatchPlaying, setFastWatchPlaying] = useState(false);
  const [playbackRate, setPlaybackRate] = useState(1);
  const sceneRefs = useRef<Map<number, HTMLDivElement>>(new Map());
  const cardRefs = useRef<Map<number, SceneCardHandle>>(new Map());
  const autoplayTokenRef = useRef(0);
  const preparedScenesRef = useRef<Set<number>>(new Set());
  const failedPreparedScenesRef = useRef<Set<number>>(new Set());
  const preparingScenesRef = useRef<Map<number, Promise<void>>>(new Map());
  const autoScrollRef = useRef(true);
  autoScrollRef.current = autoScroll;

  const applyLoadedData = useCallback((data: RawScenePageData) => {
    setDetection(data.detection);
    setTranscription(data.transcription);

    if (data.transcription && data.detection) {
      setValidations(buildValidationState(data.transcription, data.detection));
    } else {
      setValidations({});
    }
  }, []);

  const candidateScenes = useMemo(() => {
    if (!transcription || !detection) {
      return [];
    }

    const rawCandidateIndices = new Set(
      detection.candidates.map((candidate) => candidate.scene_index),
    );
    return transcription.scenes.filter((scene) =>
      rawCandidateIndices.has(scene.scene_index),
    );
  }, [detection, transcription]);

  const rawCount = useMemo(
    () =>
      candidateScenes.filter(
        (scene) =>
          validations[scene.scene_index]?.is_raw ?? scene.is_raw ?? false,
      ).length,
    [candidateScenes, validations],
  );

  const activeScenePosition = useMemo(
    () =>
      candidateScenes.findIndex(
        (scene) => scene.scene_index === activeSceneIndex,
      ),
    [activeSceneIndex, candidateScenes],
  );

  const fastWatchPrefetchAhead = useMemo(() => {
    if (playbackRate >= 8) return 4;
    if (playbackRate >= 2) return 2;
    return 1;
  }, [playbackRate]);

  const clearFastWatchBuffers = useCallback(() => {
    preparedScenesRef.current.clear();
    failedPreparedScenesRef.current.clear();
    preparingScenesRef.current.clear();
  }, []);

  const stopFastWatch = useCallback(
    (options: { clearError?: boolean } = {}) => {
      autoplayTokenRef.current += 1;
      setFastWatchPlaying(false);
      cardRefs.current.forEach((card) => {
        card.stop();
        card.releasePreload();
      });
      clearFastWatchBuffers();
      if (options.clearError ?? true) {
        setFastWatchError(null);
      }
    },
    [clearFastWatchBuffers],
  );

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
            preparedScenesRef.current.delete(sceneIndex);
            failedPreparedScenesRef.current.delete(sceneIndex);
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

      void ensureScenePrepared(sceneIndex, token);
    },
    [ensureScenePrepared],
  );

  const releaseOutsideFastWatchWindow = useCallback(
    (orderedScenes: SceneTranscription[], currentOffset: number) => {
      const keepEnd = Math.min(
        orderedScenes.length - 1,
        currentOffset + fastWatchPrefetchAhead,
      );
      const keepSceneIndices = new Set<number>();

      for (let i = currentOffset; i <= keepEnd; i += 1) {
        const scene = orderedScenes[i];
        if (scene) {
          keepSceneIndices.add(scene.scene_index);
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

  const scrollToScene = useCallback((sceneIndex: number, force = false) => {
    if (!force && !autoScrollRef.current) return;
    const element = sceneRefs.current.get(sceneIndex);
    if (element) {
      element.scrollIntoView({ behavior: "instant", block: "center" });
    }
  }, []);

  const playFastWatchFromScene = useCallback(
    async (startSceneIndex: number) => {
      if (!candidateScenes.length) return;

      const token = autoplayTokenRef.current + 1;
      autoplayTokenRef.current = token;
      setFastWatchPlaying(true);
      setFastWatchError(null);
      clearFastWatchBuffers();

      const startPosition = candidateScenes.findIndex(
        (scene) => scene.scene_index === startSceneIndex,
      );
      const orderedScenes =
        startPosition >= 0
          ? candidateScenes.slice(startPosition)
          : candidateScenes;
      const initialWindow = orderedScenes.slice(0, fastWatchPrefetchAhead + 1);
      const mandatoryWarmup = initialWindow.slice(
        0,
        Math.min(2, initialWindow.length),
      );

      for (const scene of mandatoryWarmup) {
        await ensureScenePrepared(scene.scene_index, token);
        if (autoplayTokenRef.current !== token) {
          return;
        }
      }

      for (const scene of initialWindow.slice(mandatoryWarmup.length)) {
        scheduleScenePreparation(scene.scene_index, token);
      }

      if (autoplayTokenRef.current !== token) {
        return;
      }

      for (let i = 0; i < orderedScenes.length; i += 1) {
        if (autoplayTokenRef.current !== token) {
          return;
        }

        const scene = orderedScenes[i];
        const card = cardRefs.current.get(scene.scene_index);
        if (!card) {
          continue;
        }

        setActiveSceneIndex(scene.scene_index);
        scrollToScene(scene.scene_index);

        await ensureScenePrepared(scene.scene_index, token);
        if (autoplayTokenRef.current !== token) {
          return;
        }

        if (failedPreparedScenesRef.current.has(scene.scene_index)) {
          setFastWatchError(
            `Scene ${scene.scene_index + 1} failed to preload and was skipped.`,
          );
          releaseOutsideFastWatchWindow(orderedScenes, i + 1);
          continue;
        }

        for (let offset = 1; offset <= fastWatchPrefetchAhead; offset += 1) {
          const nextScene = orderedScenes[i + offset];
          if (!nextScene) break;
          scheduleScenePreparation(nextScene.scene_index, token);
        }

        const result = await card.playAndWait();
        if (autoplayTokenRef.current !== token) {
          return;
        }

        if (result === "audio_blocked") {
          setFastWatchError(
            "Fast Watch stopped: browser blocked unmuted playback. Start a clip once with audio, then retry Fast Watch.",
          );
          stopFastWatch({ clearError: false });
          return;
        }

        if (result === "load_error") {
          setFastWatchError(
            `Scene ${scene.scene_index + 1} failed to play and was skipped.`,
          );
        }

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
      candidateScenes,
      clearFastWatchBuffers,
      ensureScenePrepared,
      fastWatchPrefetchAhead,
      releaseOutsideFastWatchWindow,
      scheduleScenePreparation,
      scrollToScene,
      stopFastWatch,
    ],
  );

  useEffect(() => {
    if (!projectId) return;

    const loadData = async () => {
      setLoading(true);
      setError(null);
      setFastWatchError(null);
      try {
        const data = await api.getRawScenes(projectId);
        applyLoadedData(data);
      } catch (err) {
        setError((err as Error).message);
      } finally {
        setLoading(false);
      }
    };

    loadData();
  }, [applyLoadedData, projectId]);

  useEffect(() => {
    if (candidateScenes.length === 0) {
      setActiveSceneIndex(-1);
      return;
    }

    setActiveSceneIndex((previous) => {
      if (
        previous >= 0 &&
        candidateScenes.some((scene) => scene.scene_index === previous)
      ) {
        return previous;
      }
      return candidateScenes[0].scene_index;
    });
  }, [candidateScenes]);

  useEffect(() => {
    if (fastWatchPlaying || candidateScenes.length === 0) {
      return;
    }

    let rafId: number | null = null;
    const viewportBufferPx = 220;

    const syncActiveSceneFromViewport = () => {
      rafId = null;
      const viewportCenter = window.innerHeight / 2;
      let bestSceneIndex: number | null = null;
      let bestDistance = Number.POSITIVE_INFINITY;

      for (const [sceneIndex, element] of sceneRefs.current.entries()) {
        const rect = element.getBoundingClientRect();
        if (
          rect.bottom < -viewportBufferPx ||
          rect.top > window.innerHeight + viewportBufferPx
        ) {
          continue;
        }

        const sceneCenter = (rect.top + rect.bottom) / 2;
        const distance = Math.abs(sceneCenter - viewportCenter);
        if (distance < bestDistance) {
          bestDistance = distance;
          bestSceneIndex = sceneIndex;
        }
      }

      if (bestSceneIndex !== null) {
        setActiveSceneIndex((previous) =>
          previous === bestSceneIndex ? previous : bestSceneIndex,
        );
      }
    };

    const requestSync = () => {
      if (rafId !== null) return;
      rafId = window.requestAnimationFrame(syncActiveSceneFromViewport);
    };

    requestSync();
    window.addEventListener("scroll", requestSync, { passive: true });
    window.addEventListener("resize", requestSync);

    return () => {
      if (rafId !== null) {
        window.cancelAnimationFrame(rafId);
      }
      window.removeEventListener("scroll", requestSync);
      window.removeEventListener("resize", requestSync);
    };
  }, [candidateScenes, fastWatchPlaying]);

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

  const toggleRaw = useCallback((sceneIndex: number) => {
    setValidations((prev) => ({
      ...prev,
      [sceneIndex]: {
        ...prev[sceneIndex],
        is_raw: !prev[sceneIndex]?.is_raw,
      },
    }));
  }, []);

  const handleTextChange = useCallback((sceneIndex: number, text: string) => {
    setValidations((prev) => ({
      ...prev,
      [sceneIndex]: {
        ...prev[sceneIndex],
        text,
      },
    }));
  }, []);

  const handleReset = useCallback(async () => {
    if (!projectId) return;

    stopFastWatch();
    setSaving(true);
    setError(null);

    try {
      await api.resetRawScenes(projectId);
      const data = await api.getRawScenes(projectId);
      applyLoadedData(data);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  }, [applyLoadedData, projectId, stopFastWatch]);

  const handleConfirm = useCallback(async () => {
    if (!projectId || !transcription) return;

    stopFastWatch();
    setSaving(true);
    setError(null);

    try {
      const rawCandidateIndices = new Set(
        detection?.candidates.map((c) => c.scene_index) ?? [],
      );

      const sceneValidations = Object.entries(validations)
        .filter(([idx]) => rawCandidateIndices.has(Number(idx)))
        .map(([idx, state]) => ({
          scene_index: Number(idx),
          is_raw: state.is_raw,
          text: state.text || undefined,
        }));

      if (sceneValidations.length > 0) {
        const result = await api.validateRawScenes(projectId, sceneValidations);
        setTranscription(result.transcription);
      }

      await api.confirmRawScenes(projectId);
      navigate(`/project/${projectId}/script`);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  }, [
    detection,
    navigate,
    projectId,
    stopFastWatch,
    transcription,
    validations,
  ]);

  const handleToggleFastWatch = useCallback(() => {
    if (fastWatchPlaying) {
      stopFastWatch();
      return;
    }

    const startSceneIndex =
      activeSceneIndex >= 0
        ? activeSceneIndex
        : candidateScenes.length > 0
          ? candidateScenes[0].scene_index
          : undefined;
    if (startSceneIndex === undefined) return;

    void playFastWatchFromScene(startSceneIndex);
  }, [
    activeSceneIndex,
    candidateScenes,
    fastWatchPlaying,
    playFastWatchFromScene,
    stopFastWatch,
  ]);

  const handleTimelineSeek = useCallback(
    (position: number) => {
      const targetScene = candidateScenes[position];
      if (!targetScene) return;

      setActiveSceneIndex(targetScene.scene_index);
      scrollToScene(targetScene.scene_index, true);

      if (fastWatchPlaying) {
        void playFastWatchFromScene(targetScene.scene_index);
      }
    },
    [candidateScenes, fastWatchPlaying, playFastWatchFromScene, scrollToScene],
  );

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <Loader2 className="h-8 w-8 animate-spin text-[hsl(var(--muted-foreground))]" />
      </div>
    );
  }

  if (!transcription || !detection) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <p className="text-[hsl(var(--muted-foreground))]">
          No raw scene data found.
        </p>
      </div>
    );
  }

  return (
    <div className="min-h-screen p-4 pb-32">
      <div className="max-w-4xl mx-auto space-y-6">
        <header className="flex items-center justify-between">
          <div>
            <h1 className="text-xl font-bold">Raw Scene Validation</h1>
            <p className="text-sm text-[hsl(var(--muted-foreground))]">
              {detection.candidates.length} raw scene
              {detection.candidates.length !== 1 ? "s" : ""} detected
              {detection.speaker_count > 0 &&
                ` · ${detection.speaker_count} speakers found`}
              {rawCount > 0 && ` · ${rawCount} marked as raw`}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <Button variant="outline" onClick={handleReset} disabled={saving}>
              <RotateCcw className="h-4 w-4 mr-2" />
              Reset
            </Button>
            <Button onClick={handleConfirm} disabled={saving}>
              {saving ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin mr-2" />
                  Saving...
                </>
              ) : (
                <>
                  Confirm & Continue
                  <ArrowRight className="h-4 w-4 ml-2" />
                </>
              )}
            </Button>
          </div>
        </header>

        {error && (
          <div className="p-3 bg-[hsl(var(--destructive))]/10 rounded-lg">
            <p className="text-sm text-[hsl(var(--destructive))]">{error}</p>
          </div>
        )}

        {candidateScenes.length === 0 ? (
          <div className="rounded-lg border border-[hsl(var(--border))] p-6 text-sm text-[hsl(var(--muted-foreground))]">
            No raw scene candidates to review.
          </div>
        ) : (
          <div className="space-y-4">
            {candidateScenes.map((scene, scenePosition) => (
              <div
                key={scene.scene_index}
                data-scene-index={scene.scene_index}
                className="[content-visibility:auto] [contain-intrinsic-size:720px]"
                ref={(element) => {
                  if (element) {
                    sceneRefs.current.set(scene.scene_index, element);
                  } else {
                    sceneRefs.current.delete(scene.scene_index);
                  }
                }}
              >
                <SceneCard
                  ref={(card) => {
                    if (card) {
                      cardRefs.current.set(scene.scene_index, card);
                    } else {
                      cardRefs.current.delete(scene.scene_index);
                    }
                  }}
                  scene={scene}
                  projectId={projectId!}
                  validation={validations[scene.scene_index]}
                  isActive={activeSceneIndex === scene.scene_index}
                  playbackRate={playbackRate}
                  controlsDisabled={fastWatchPlaying}
                  preloadMode={
                    fastWatchPlaying &&
                    Math.abs(scenePosition - activeScenePosition) <= 2
                      ? "auto"
                      : "metadata"
                  }
                  onToggleRaw={() => toggleRaw(scene.scene_index)}
                  onTextChange={(text) =>
                    handleTextChange(scene.scene_index, text)
                  }
                />
              </div>
            ))}
          </div>
        )}

        <div className="flex justify-end pb-12">
          <Button onClick={handleConfirm} disabled={saving}>
            {saving ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin mr-2" />
                Saving...
              </>
            ) : (
              <>
                Confirm & Continue
                <ArrowRight className="h-4 w-4 ml-2" />
              </>
            )}
          </Button>
        </div>
      </div>

      {candidateScenes.length > 0 && (
        <div className="fixed bottom-0 left-0 right-0 z-50 bg-[hsl(var(--card))] border-t border-[hsl(var(--border))] shadow-lg">
          <div className="max-w-4xl mx-auto px-4 py-2 space-y-2">
            <div className="flex items-center gap-3">
              <Button
                variant={fastWatchPlaying ? "default" : "outline"}
                size="sm"
                onClick={handleToggleFastWatch}
                disabled={saving || candidateScenes.length === 0}
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

              <span className="text-xs text-[hsl(var(--muted-foreground))] min-w-[112px]">
                Scene {Math.max(activeScenePosition + 1, 1)} /{" "}
                {candidateScenes.length}
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
              max={Math.max(0, candidateScenes.length - 1)}
              step={1}
              value={Math.max(activeScenePosition, 0)}
              onChange={(e) => handleTimelineSeek(parseInt(e.target.value, 10))}
              className="w-full h-1 accent-[hsl(var(--primary))]"
              title="Timeline scroller"
            />

            {fastWatchError && (
              <p className="text-xs text-amber-500">{fastWatchError}</p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
