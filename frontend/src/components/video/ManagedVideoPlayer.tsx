import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
} from "react";
import { Loader2, Play, RotateCcw } from "lucide-react";
import { cn } from "@/utils";
import {
  getBrowserMediaCoordinator,
  type MediaSessionGrant,
} from "@/utils/mediaCoordinator";
import { buildVideoSourceCandidates } from "@/utils/mediaSources";

export interface WaitUntilReadyOptions {
  minReadyState?: number;
  timeoutMs?: number;
}

export interface ManagedVideoPlayerHandle {
  playFromStart: () => void;
  seekToStart: () => Promise<void>;
  seekTo: (time: number, autoplay?: boolean) => Promise<void>;
  play: () => void;
  playChecked: () => Promise<boolean>;
  pause: () => void;
  waitUntilReady: (options?: WaitUntilReadyOptions) => Promise<void>;
  hasLoadError: () => boolean;
  isPlaying: () => boolean;
  getReadyState: () => number;
  retryLoad: () => Promise<void>;
  forceLoad: () => void;
  releaseLoad: () => void;
  releaseAndPreventReload: () => void;
}

interface ReadyWaiter {
  minReadyState: number;
  timeoutId: number;
  resolve: () => void;
}

export interface ManagedVideoPlayerProps {
  src: string;
  fallbackSrc?: string | null;
  className?: string;
  muted?: boolean;
  controls?: boolean;
  disableInteraction?: boolean;
  playbackRate?: number;
  onClipEnded?: () => void;
  onTimeUpdate?: (currentTime: number) => void;
  onLoadedMetadata?: (duration: number) => void;
  onPhaseChange?: (
    phase: "poster" | "leasing" | "warming" | "ready" | "playing" | "frozen" | "error",
  ) => void;
  requestLoad?: boolean;
  requestWarmup?: boolean;
  attachedPriority?: number;
  warmupPriority?: number;
  startTime?: number;
  endTime?: number;
  seekOffsetSeconds?: number;
  dedicatedAudio?: boolean;
  placeholderLabel?: string;
}

const INTENTIONAL_DETACH_MS = 200;

function clampTime(value: number, min: number, max: number | null): number {
  const bounded = Math.max(value, min);
  if (max === null || !Number.isFinite(max)) {
    return bounded;
  }
  return Math.min(bounded, max);
}

function capturePoster(video: HTMLVideoElement | null): string | null {
  if (!video || video.videoWidth <= 0 || video.videoHeight <= 0) {
    return null;
  }

  try {
    const canvas = document.createElement("canvas");
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    const context = canvas.getContext("2d");
    if (!context) return null;
    context.drawImage(video, 0, 0, canvas.width, canvas.height);
    return canvas.toDataURL("image/jpeg", 0.82);
  } catch {
    return null;
  }
}

export const ManagedVideoPlayer = forwardRef<
  ManagedVideoPlayerHandle,
  ManagedVideoPlayerProps
>(function ManagedVideoPlayer(
  {
    src,
    fallbackSrc,
    className,
    muted = true,
    controls = true,
    disableInteraction = false,
    playbackRate = 1,
    onClipEnded,
    onTimeUpdate,
    onLoadedMetadata,
    onPhaseChange,
    requestLoad = true,
    requestWarmup = false,
    attachedPriority = 100,
    warmupPriority = 100,
    startTime = 0,
    endTime,
    seekOffsetSeconds = 0,
    dedicatedAudio = false,
    placeholderLabel = "Media deferred",
  },
  ref,
) {
  const playerIdRef = useRef(
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `media-${Math.random().toString(36).slice(2)}`,
  );
  const videoRef = useRef<HTMLVideoElement>(null);
  const retryAttemptedRef = useRef(false);
  const mountedRef = useRef(false);
  const intentionalDetachRef = useRef(false);
  const endedNotifiedRef = useRef(false);
  const pendingSeekRef = useRef<number | null>(null);
  const pendingAutoplayRef = useRef(false);
  const readyWaitersRef = useRef<ReadyWaiter[]>([]);
  const forceRequestedRef = useRef(false);
  const forceWarmupRef = useRef(false);
  const suppressReloadRef = useRef(false);
  const playingRef = useRef(false);
  const activeSourceIndexRef = useRef(0);

  const onPhaseChangeRef = useRef(onPhaseChange);
  const onTimeUpdateRef = useRef(onTimeUpdate);
  const onLoadedMetadataRef = useRef(onLoadedMetadata);
  const onClipEndedRef = useRef(onClipEnded);
  useEffect(() => {
    onPhaseChangeRef.current = onPhaseChange;
    onTimeUpdateRef.current = onTimeUpdate;
    onLoadedMetadataRef.current = onLoadedMetadata;
    onClipEndedRef.current = onClipEnded;
  });
  const [grant, setGrant] = useState<MediaSessionGrant>({
    attachedGranted: false,
    warmupGranted: false,
  });
  const [phase, setPhase] = useState<
    "poster" | "leasing" | "warming" | "ready" | "playing" | "frozen" | "error"
  >("poster");
  const [posterDataUrl, setPosterDataUrl] = useState<string | null>(null);
  const [renderVersion, setRenderVersion] = useState(0);
  const [hasError, setHasError] = useState(false);
  const [activeSourceIndex, setActiveSourceIndex] = useState(0);

  const sourceCandidates = useMemo(
    () => buildVideoSourceCandidates(src, fallbackSrc),
    [fallbackSrc, src],
  );
  const activeSrc = sourceCandidates[activeSourceIndex] ?? src;

  const effectiveStartTime = useMemo(
    () => Math.max(0, startTime + seekOffsetSeconds),
    [seekOffsetSeconds, startTime],
  );

  const effectiveRequestLoad =
    !suppressReloadRef.current && (requestLoad || forceRequestedRef.current);
  const effectiveRequestWarmup =
    effectiveRequestLoad && (requestWarmup || forceWarmupRef.current);

  const resolveReadyWaiters = useCallback((force = false) => {
    const readyState =
      videoRef.current?.readyState ?? HTMLMediaElement.HAVE_NOTHING;
    const remaining: ReadyWaiter[] = [];
    for (const waiter of readyWaitersRef.current) {
      if (force || hasError || readyState >= waiter.minReadyState) {
        window.clearTimeout(waiter.timeoutId);
        waiter.resolve();
      } else {
        remaining.push(waiter);
      }
    }
    readyWaitersRef.current = remaining;
  }, [hasError]);

  const updatePhase = useCallback(
    (
      next:
        | "poster"
        | "leasing"
        | "warming"
        | "ready"
        | "playing"
        | "frozen"
        | "error",
    ) => {
      setPhase(next);
      onPhaseChangeRef.current?.(next);
    },
    [],
  );

  const markEnded = useCallback(() => {
    if (endedNotifiedRef.current) return;
    endedNotifiedRef.current = true;
    onClipEndedRef.current?.();
  }, []);

  const applyPendingSeek = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;

    const requestedTime = pendingSeekRef.current ?? effectiveStartTime;
    const maxTime =
      endTime !== undefined
        ? endTime
        : Number.isFinite(video.duration)
          ? video.duration
          : null;
    const targetTime = clampTime(requestedTime, effectiveStartTime, maxTime ?? null);
    pendingSeekRef.current = null;
    video.currentTime = targetTime;
    onTimeUpdateRef.current?.(targetTime);

    if (!pendingAutoplayRef.current) {
      return;
    }
    pendingAutoplayRef.current = false;
    video.playbackRate = playbackRate;
    void video.play().catch(() => {
      playingRef.current = false;
      updatePhase("ready");
    });
  }, [effectiveStartTime, endTime, playbackRate, updatePhase]);

  const markReady = useCallback(() => {
    retryAttemptedRef.current = false;
    if (hasError) {
      setHasError(false);
    }
    if (playingRef.current) {
      updatePhase("playing");
    } else {
      updatePhase("ready");
    }
    resolveReadyWaiters();
  }, [hasError, resolveReadyWaiters, updatePhase]);

  const captureAndFreeze = useCallback(() => {
    const nextPoster = capturePoster(videoRef.current);
    if (nextPoster) {
      setPosterDataUrl(nextPoster);
    }
    intentionalDetachRef.current = true;
    window.setTimeout(() => {
      intentionalDetachRef.current = false;
    }, INTENTIONAL_DETACH_MS);
    resolveReadyWaiters(true);
    updatePhase(nextPoster ? "frozen" : "poster");
  }, [resolveReadyWaiters, updatePhase]);

  const forceReload = useCallback(async () => {
    retryAttemptedRef.current = true;
    setHasError(false);
    forceRequestedRef.current = true;
    forceWarmupRef.current = true;
    suppressReloadRef.current = false;
    updatePhase("leasing");
    setRenderVersion((value) => value + 1);
  }, [updatePhase]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      resolveReadyWaiters(true);
    };
  }, [resolveReadyWaiters]);

  const sessionUpdateRef = useRef<((next: Partial<{
    requestLoad: boolean;
    requestWarmup: boolean;
    attachedPriority: number;
    warmupPriority: number;
    kind: "video" | "audio";
  }>) => void) | null>(null);

  useEffect(() => {
    const coordinator = getBrowserMediaCoordinator();
    const handle = coordinator.registerSession(
      {
        id: playerIdRef.current,
        requestLoad: effectiveRequestLoad,
        requestWarmup: effectiveRequestWarmup,
        attachedPriority,
        warmupPriority,
        kind: dedicatedAudio ? "audio" : "video",
      },
      (nextGrant) => {
        if (!mountedRef.current) return;
        setGrant(nextGrant);
      },
    );
    sessionUpdateRef.current = handle.update;
    return () => {
      handle.release();
      sessionUpdateRef.current = null;
    };
  }, []);

  useEffect(() => {
    sessionUpdateRef.current?.({
      requestLoad: effectiveRequestLoad,
      requestWarmup: effectiveRequestWarmup,
      attachedPriority,
      warmupPriority,
      kind: dedicatedAudio ? "audio" : "video",
    });
  }, [
    attachedPriority,
    dedicatedAudio,
    effectiveRequestLoad,
    effectiveRequestWarmup,
    warmupPriority,
  ]);

  useEffect(() => {
    if (!effectiveRequestLoad) {
      captureAndFreeze();
      return;
    }

    if (!grant.attachedGranted) {
      if (videoRef.current) {
        captureAndFreeze();
      } else if (posterDataUrl) {
        updatePhase("frozen");
      } else {
        updatePhase("leasing");
      }
      return;
    }

    if (!videoRef.current) {
      setHasError(false);
      if (grant.warmupGranted) {
        updatePhase("warming");
      } else {
        updatePhase("leasing");
      }
      return;
    }

    if (grant.warmupGranted && phase === "leasing") {
      updatePhase("warming");
    }
  }, [
    captureAndFreeze,
    effectiveRequestLoad,
    grant.attachedGranted,
    grant.warmupGranted,
    phase,
    posterDataUrl,
    updatePhase,
  ]);

  useEffect(() => {
    endedNotifiedRef.current = false;
    retryAttemptedRef.current = false;
    activeSourceIndexRef.current = 0;
    setActiveSourceIndex(0);
    pendingSeekRef.current = effectiveStartTime;
    pendingAutoplayRef.current = false;
    setHasError(false);
    updatePhase(effectiveRequestLoad ? "leasing" : "poster");
    setRenderVersion((value) => value + 1);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- updatePhase is stable; including it would remount the <video> on every parent render
  }, [effectiveRequestLoad, effectiveStartTime, endTime, sourceCandidates]);

  useEffect(() => {
    if (videoRef.current) {
      videoRef.current.playbackRate = playbackRate;
    }
  }, [playbackRate]);

  const handleLoadStart = useCallback(() => {
    if (hasError) {
      setHasError(false);
    }
    if (grant.warmupGranted) {
      updatePhase("warming");
    } else {
      updatePhase("leasing");
    }
  }, [grant.warmupGranted, hasError, updatePhase]);

  const handleLoadedMetadata = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;
    video.playbackRate = playbackRate;
    onLoadedMetadataRef.current?.(video.duration);
    pendingSeekRef.current =
      pendingSeekRef.current === null ? effectiveStartTime : pendingSeekRef.current;
    applyPendingSeek();
  }, [applyPendingSeek, effectiveStartTime, playbackRate]);

  const handleLoadedData = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;

    if ("requestVideoFrameCallback" in video) {
      (video as HTMLVideoElement & {
        requestVideoFrameCallback?: (
          callback: () => void,
        ) => number;
      }).requestVideoFrameCallback?.(() => {
        markReady();
      });
      return;
    }

    markReady();
  }, [markReady]);

  const handleError = useCallback(() => {
    if (intentionalDetachRef.current) {
      return;
    }

    if (!retryAttemptedRef.current) {
      void forceReload();
      return;
    }

    const nextSourceIndex = activeSourceIndexRef.current + 1;
    if (nextSourceIndex < sourceCandidates.length) {
      captureAndFreeze();
      retryAttemptedRef.current = false;
      playingRef.current = false;
      setHasError(false);
      activeSourceIndexRef.current = nextSourceIndex;
      setActiveSourceIndex(nextSourceIndex);
      updatePhase("leasing");
      setRenderVersion((value) => value + 1);
      return;
    }

    setHasError(true);
    playingRef.current = false;
    updatePhase("error");
    resolveReadyWaiters(true);
    markEnded();
  }, [
    captureAndFreeze,
    forceReload,
    markEnded,
    resolveReadyWaiters,
    sourceCandidates.length,
    updatePhase,
  ]);

  const handleTimeUpdate = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;
    onTimeUpdateRef.current?.(video.currentTime);

    if (endTime === undefined) {
      return;
    }

    if (video.currentTime >= endTime) {
      if (!video.paused) {
        video.pause();
      }
      if (Math.abs(video.currentTime - endTime) > 0.03) {
        video.currentTime = endTime;
      }
      playingRef.current = false;
      updatePhase("ready");
      markEnded();
    }
  }, [endTime, markEnded, updatePhase]);

  const handlePlay = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;
    playingRef.current = true;
    if (video.currentTime < effectiveStartTime - 0.05) {
      video.currentTime = effectiveStartTime;
    }
    if (endTime !== undefined && video.currentTime >= endTime) {
      video.currentTime = effectiveStartTime;
    }
    endedNotifiedRef.current = false;
    updatePhase("playing");
  }, [effectiveStartTime, endTime, updatePhase]);

  const handlePause = useCallback(() => {
    if (hasError) return;
    playingRef.current = false;
    updatePhase("ready");
  }, [hasError, updatePhase]);

  const handleSeeked = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;
    if (video.currentTime < effectiveStartTime - 0.05) {
      video.currentTime = effectiveStartTime;
    }
    if (endTime !== undefined && video.currentTime > endTime) {
      video.currentTime = endTime;
    }
  }, [effectiveStartTime, endTime]);

  useImperativeHandle(
    ref,
    () => ({
      playFromStart: () => {
        forceRequestedRef.current = true;
        forceWarmupRef.current = true;
        suppressReloadRef.current = false;
        pendingSeekRef.current = effectiveStartTime;
        pendingAutoplayRef.current = true;
        sessionUpdateRef.current?.({
          requestLoad: true,
          requestWarmup: true,
        });
        const video = videoRef.current;
        if (!video) {
          setRenderVersion((value) => value + 1);
          return;
        }
        video.currentTime = effectiveStartTime;
        video.playbackRate = playbackRate;
        void video.play().catch(() => {
          playingRef.current = false;
          updatePhase("ready");
        });
      },
      seekToStart: () => {
        return new Promise<void>((resolve) => {
          pendingSeekRef.current = effectiveStartTime;
          pendingAutoplayRef.current = false;
          const video = videoRef.current;
          if (!video || video.readyState < HTMLMediaElement.HAVE_METADATA) {
            resolve();
            return;
          }
          let done = false;
          const finalize = () => {
            if (done) return;
            done = true;
            window.clearTimeout(timeoutId);
            video.removeEventListener("seeked", onSeeked);
            video.removeEventListener("error", onError);
            resolve();
          };
          const onSeeked = () => finalize();
          const onError = () => finalize();
          const timeoutId = window.setTimeout(finalize, 1200);
          video.addEventListener("seeked", onSeeked);
          video.addEventListener("error", onError);
          try {
            video.currentTime = effectiveStartTime;
          } catch {
            finalize();
          }
        });
      },
      seekTo: (time, autoplay = false) => {
        return new Promise<void>((resolve) => {
          forceRequestedRef.current = true;
          suppressReloadRef.current = false;
          pendingSeekRef.current = time;
          pendingAutoplayRef.current = autoplay;
          sessionUpdateRef.current?.({ requestLoad: true });
          const video = videoRef.current;
          if (!video || video.readyState < HTMLMediaElement.HAVE_METADATA) {
            setRenderVersion((value) => value + 1);
            resolve();
            return;
          }
          let done = false;
          const finalize = () => {
            if (done) return;
            done = true;
            window.clearTimeout(timeoutId);
            video.removeEventListener("seeked", onSeeked);
            video.removeEventListener("error", onError);
            resolve();
          };
          const onSeeked = () => finalize();
          const onError = () => finalize();
          const timeoutId = window.setTimeout(finalize, 1200);
          video.addEventListener("seeked", onSeeked);
          video.addEventListener("error", onError);
          try {
            const maxTime =
              endTime !== undefined
                ? endTime
                : Number.isFinite(video.duration)
                  ? video.duration
                  : null;
            video.currentTime = clampTime(time, effectiveStartTime, maxTime);
            if (autoplay) {
              void video.play().catch(() => {
                playingRef.current = false;
                updatePhase("ready");
              });
            }
          } catch {
            finalize();
          }
        });
      },
      play: () => {
        forceRequestedRef.current = true;
        forceWarmupRef.current = true;
        suppressReloadRef.current = false;
        pendingAutoplayRef.current = true;
        sessionUpdateRef.current?.({
          requestLoad: true,
          requestWarmup: true,
        });
        const video = videoRef.current;
        if (!video) {
          setRenderVersion((value) => value + 1);
          return;
        }
        video.playbackRate = playbackRate;
        void video.play().catch(() => {
          playingRef.current = false;
          updatePhase("ready");
        });
      },
      playChecked: async () => {
        const video = videoRef.current;
        if (!video) {
          forceRequestedRef.current = true;
          forceWarmupRef.current = true;
          suppressReloadRef.current = false;
          sessionUpdateRef.current?.({
            requestLoad: true,
            requestWarmup: true,
          });
          return false;
        }
        try {
          video.playbackRate = playbackRate;
          await video.play();
          return true;
        } catch {
          return false;
        }
      },
      pause: () => {
        videoRef.current?.pause();
      },
      waitUntilReady: (options) => {
        forceRequestedRef.current = true;
        forceWarmupRef.current = true;
        suppressReloadRef.current = false;
        sessionUpdateRef.current?.({
          requestLoad: true,
          requestWarmup: true,
        });
        return new Promise<void>((resolve) => {
          const minReadyState =
            options?.minReadyState ?? HTMLMediaElement.HAVE_CURRENT_DATA;
          const timeoutMs = options?.timeoutMs ?? 6000;
          const video = videoRef.current;
          if (!video) {
            setRenderVersion((value) => value + 1);
          }
          if (
            hasError ||
            (video && video.readyState >= minReadyState)
          ) {
            resolve();
            return;
          }
          const timeoutId = window.setTimeout(() => {
            const idx = readyWaitersRef.current.indexOf(waiter);
            if (idx >= 0) {
              readyWaitersRef.current.splice(idx, 1);
            }
            resolve();
          }, timeoutMs);
          const waiter: ReadyWaiter = {
            minReadyState,
            timeoutId,
            resolve,
          };
          readyWaitersRef.current.push(waiter);
        });
      },
      hasLoadError: () => hasError,
      isPlaying: () => {
        const video = videoRef.current;
        return Boolean(video && !video.paused && !video.ended);
      },
      getReadyState: () =>
        videoRef.current?.readyState ?? HTMLMediaElement.HAVE_NOTHING,
      retryLoad: async () => {
        await forceReload();
      },
      forceLoad: () => {
        forceRequestedRef.current = true;
        forceWarmupRef.current = true;
        suppressReloadRef.current = false;
        setHasError(false);
        sessionUpdateRef.current?.({
          requestLoad: true,
          requestWarmup: true,
        });
        updatePhase("leasing");
        setRenderVersion((value) => value + 1);
      },
      releaseLoad: () => {
        forceRequestedRef.current = false;
        forceWarmupRef.current = false;
        suppressReloadRef.current = false;
        sessionUpdateRef.current?.({
          requestLoad,
          requestWarmup,
        });
        setRenderVersion((value) => value + 1);
      },
      releaseAndPreventReload: () => {
        forceRequestedRef.current = false;
        forceWarmupRef.current = false;
        suppressReloadRef.current = true;
        sessionUpdateRef.current?.({
          requestLoad: false,
          requestWarmup: false,
        });
        if (videoRef.current) {
          captureAndFreeze();
        }
        setRenderVersion((value) => value + 1);
      },
    }),
    [
      captureAndFreeze,
      effectiveStartTime,
      endTime,
      forceReload,
      hasError,
      playbackRate,
      requestLoad,
      requestWarmup,
      updatePhase,
    ],
  );

  const shouldRenderVideo =
    effectiveRequestLoad && grant.attachedGranted && Boolean(activeSrc);

  return (
    <div className={cn("relative bg-black", className)}>
      {posterDataUrl ? (
        <img
          src={posterDataUrl}
          alt=""
          className={cn(
            "absolute inset-0 h-full w-full object-contain",
            shouldRenderVideo && "opacity-0",
          )}
        />
      ) : (
        !shouldRenderVideo && (
          <div className="absolute inset-0 flex items-center justify-center bg-[hsl(var(--muted))]">
            <div className="flex flex-col items-center gap-2 text-[hsl(var(--muted-foreground))]">
              <Play className="h-7 w-7" />
              <span className="text-xs">{placeholderLabel}</span>
            </div>
          </div>
        )
      )}

      {phase !== "error" && shouldRenderVideo && phase !== "ready" && phase !== "playing" && (
        <div className="absolute inset-0 z-10 flex items-center justify-center bg-black/65">
          <Loader2 className="h-7 w-7 animate-spin text-white" />
        </div>
      )}

      {hasError && (
        <div className="absolute inset-0 z-20 flex items-center justify-center bg-black/80">
          <div className="flex flex-col items-center gap-2 text-white">
            <span className="text-xs">Failed to load</span>
            <button
              type="button"
              onClick={() => {
                void forceReload();
              }}
              className="rounded bg-white/20 px-2 py-1 text-xs transition-colors hover:bg-white/30"
            >
              <span className="inline-flex items-center gap-1">
                <RotateCcw className="h-3.5 w-3.5" />
                Retry
              </span>
            </button>
          </div>
        </div>
      )}

      {shouldRenderVideo && (
        <video
          key={`${activeSrc}-${renderVersion}`}
          ref={videoRef}
          src={activeSrc}
          className={cn(
            "h-full w-full object-contain",
            disableInteraction && "pointer-events-none",
          )}
          onLoadStart={handleLoadStart}
          onLoadedMetadata={handleLoadedMetadata}
          onLoadedData={handleLoadedData}
          onCanPlay={markReady}
          onError={handleError}
          onTimeUpdate={handleTimeUpdate}
          onPlay={handlePlay}
          onPause={handlePause}
          onEnded={markEnded}
          onSeeked={handleSeeked}
          controls={controls}
          muted={muted}
          playsInline
          crossOrigin="anonymous"
          preload={grant.warmupGranted ? "auto" : "metadata"}
        />
      )}
    </div>
  );
});
