import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
} from "react";
import { Loader2 } from "lucide-react";
import { cn } from "@/utils";
import { acquire, acquirePriority } from "@/utils/videoConnectionPool";

export interface MatchesClipWaitUntilReadyOptions {
  minReadyState?: number;
  timeoutMs?: number;
}

export interface MatchesClipPlayerHandle {
  playFromStart: () => void;
  seekToStart: () => Promise<void>;
  play: () => void;
  pause: () => void;
  waitUntilReady: (options?: MatchesClipWaitUntilReadyOptions) => Promise<void>;
  hasLoadError: () => boolean;
  isPlaying: () => boolean;
  getReadyState: () => number;
  retryLoad: () => Promise<void>;
  forceLoad: () => void;
  releaseLoad: () => void;
}

interface MatchesClipPlayerProps {
  src: string;
  className?: string;
  muted?: boolean;
  playbackRate?: number;
  onClipEnded?: () => void;
  controls?: boolean;
  preloadMode?: "none" | "metadata" | "auto";
  disableInteraction?: boolean;
}

export const MatchesClipPlayer = forwardRef<
  MatchesClipPlayerHandle,
  MatchesClipPlayerProps
>(function MatchesClipPlayer(
  {
    src,
    className,
    muted = true,
    playbackRate = 1,
    onClipEnded,
    controls = true,
    preloadMode = "metadata",
    disableInteraction = false,
  },
  ref,
) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const endNotifiedRef = useRef(false);
  const poolReleaseRef = useRef<(() => void) | null>(null);
  const poolPendingRef = useRef(false);
  const [hasError, setHasError] = useState(false);
  const [isLoaded, setIsLoaded] = useState(false);
  const [retryCount, setRetryCount] = useState(0);
  const [preloadOverride, setPreloadOverride] = useState<
    "none" | "metadata" | "auto" | null
  >(null);
  const [isSourceAttached, setIsSourceAttached] = useState(
    preloadMode !== "none",
  );
  const hasErrorRef = useRef(false);

  const effectivePreload = preloadOverride ?? preloadMode;
  const shouldLoad = effectivePreload !== "none";

  const videoSrc = useMemo(() => {
    if (retryCount === 0) return src;
    const separator = src.includes("?") ? "&" : "?";
    return `${src}${separator}_retry=${retryCount}`;
  }, [src, retryCount]);

  const resetEndState = useCallback(() => {
    endNotifiedRef.current = false;
  }, []);

  const notifyClipEnded = useCallback(() => {
    if (endNotifiedRef.current) return;
    endNotifiedRef.current = true;
    onClipEnded?.();
  }, [onClipEnded]);

  useEffect(() => {
    hasErrorRef.current = hasError;
  }, [hasError]);

  const releasePoolSlot = useCallback(() => {
    if (poolReleaseRef.current) {
      poolReleaseRef.current();
      poolReleaseRef.current = null;
    }
    poolPendingRef.current = false;
  }, []);

  const detachSource = useCallback(() => {
    const video = videoRef.current;
    if (video) {
      video.pause();
      video.removeAttribute("src");
      video.load();
    }
    setIsSourceAttached(false);
    setIsLoaded(false);
    setHasError(false);
    resetEndState();
  }, [resetEndState]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    video.playbackRate = playbackRate;
  }, [playbackRate]);

  useEffect(() => {
    let cancelled = false;

    if (!shouldLoad) {
      detachSource();
      releasePoolSlot();
      return () => {
        cancelled = true;
      };
    }

    if (isSourceAttached || poolPendingRef.current || poolReleaseRef.current) {
      return () => {
        cancelled = true;
      };
    }

    poolPendingRef.current = true;
    const acquireFn = effectivePreload === "auto" ? acquirePriority : acquire;
    void acquireFn()
      .then((release) => {
        if (cancelled) {
          release();
          return;
        }
        poolPendingRef.current = false;
        poolReleaseRef.current = release;
        setIsSourceAttached(true);
      })
      .catch(() => {
        poolPendingRef.current = false;
        setHasError(true);
        setIsLoaded(true);
      });

    return () => {
      cancelled = true;
    };
  }, [
    detachSource,
    effectivePreload,
    isSourceAttached,
    releasePoolSlot,
    shouldLoad,
  ]);

  useEffect(() => {
    return () => {
      releasePoolSlot();
      const video = videoRef.current;
      if (video) {
        video.pause();
        video.removeAttribute("src");
        video.load();
      }
    };
  }, [releasePoolSlot]);

  useImperativeHandle(
    ref,
    () => ({
      playFromStart: () => {
        const video = videoRef.current;
        if (!video) {
          setPreloadOverride("auto");
          return;
        }
        video.currentTime = 0;
        video.playbackRate = playbackRate;
        resetEndState();
        void video.play().catch(() => {
          // Keep UI stable when autoplay is blocked.
        });
      },
      seekToStart: () => {
        return new Promise<void>((resolve) => {
          const video = videoRef.current;
          if (
            !video ||
            video.error ||
            video.readyState < HTMLMediaElement.HAVE_METADATA
          ) {
            resolve();
            return;
          }

          resetEndState();
          let done = false;
          const finalize = () => {
            if (done) return;
            done = true;
            video.removeEventListener("seeked", onSeeked);
            video.removeEventListener("error", onError);
            window.clearTimeout(timeoutId);
            resolve();
          };
          const onSeeked = () => finalize();
          const onError = () => finalize();
          const timeoutId = window.setTimeout(finalize, 1200);

          video.addEventListener("seeked", onSeeked);
          video.addEventListener("error", onError);
          try {
            video.currentTime = 0;
          } catch {
            finalize();
          }
        });
      },
      play: () => {
        const video = videoRef.current;
        if (!video) {
          setPreloadOverride("auto");
          return;
        }
        video.playbackRate = playbackRate;
        resetEndState();
        void video.play().catch(() => {
          // Keep UI stable when autoplay is blocked.
        });
      },
      pause: () => {
        videoRef.current?.pause();
      },
      waitUntilReady: (options) => {
        return new Promise<void>((resolve) => {
          const minReadyState =
            options?.minReadyState ?? HTMLMediaElement.HAVE_CURRENT_DATA;
          const timeoutMs = options?.timeoutMs ?? 6000;
          let done = false;
          let watchedVideo: HTMLVideoElement | null = null;
          let pollId = 0;
          const finalize = () => {
            if (done) return;
            done = true;
            if (watchedVideo) {
              watchedVideo.removeEventListener("canplay", onReady);
              watchedVideo.removeEventListener("loadeddata", onReady);
              watchedVideo.removeEventListener("error", onError);
            }
            window.clearInterval(pollId);
            window.clearTimeout(timeoutId);
            resolve();
          };
          const onReady = () => {
            const video = videoRef.current;
            if (!video || video !== watchedVideo) {
              return;
            }
            if (video.readyState >= minReadyState) {
              finalize();
            }
          };
          const onError = () => finalize();
          const syncWatchedVideo = () => {
            const nextVideo = videoRef.current;
            if (nextVideo === watchedVideo) {
              return nextVideo;
            }
            if (watchedVideo) {
              watchedVideo.removeEventListener("canplay", onReady);
              watchedVideo.removeEventListener("loadeddata", onReady);
              watchedVideo.removeEventListener("error", onError);
            }
            watchedVideo = nextVideo;
            if (watchedVideo) {
              watchedVideo.addEventListener("canplay", onReady);
              watchedVideo.addEventListener("loadeddata", onReady);
              watchedVideo.addEventListener("error", onError);
            }
            return watchedVideo;
          };
          const pollReadyState = () => {
            if (hasErrorRef.current) {
              finalize();
              return;
            }
            const video = syncWatchedVideo();
            if (!video || video.error) {
              return;
            }
            if (video.readyState >= minReadyState) {
              finalize();
            }
          };
          const timeoutId = window.setTimeout(finalize, timeoutMs);
          pollId = window.setInterval(pollReadyState, 50);
          pollReadyState();
        });
      },
      hasLoadError: () => hasError,
      isPlaying: () => {
        const video = videoRef.current;
        if (!video) return false;
        return !video.paused && !video.ended;
      },
      getReadyState: () => {
        return videoRef.current?.readyState ?? HTMLMediaElement.HAVE_NOTHING;
      },
      retryLoad: async () => {
        setHasError(false);
        setIsLoaded(false);
        setPreloadOverride("auto");
        setRetryCount((value) => value + 1);
      },
      forceLoad: () => {
        setPreloadOverride("auto");
      },
      releaseLoad: () => {
        detachSource();
        releasePoolSlot();
        setPreloadOverride(null);
      },
    }),
    [detachSource, hasError, playbackRate, releasePoolSlot, resetEndState],
  );

  const handleLoadStart = useCallback(() => {
    setHasError(false);
    setIsLoaded(false);
    resetEndState();
  }, [resetEndState]);

  const handleLoadedMetadata = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;
    setHasError(false);
    setIsLoaded(true);
    video.currentTime = 0;
    video.playbackRate = playbackRate;
  }, [playbackRate]);

  const handleError = useCallback(() => {
    setHasError(true);
    setIsLoaded(true);
    notifyClipEnded();
  }, [notifyClipEnded]);

  const handleEnded = useCallback(() => {
    notifyClipEnded();
  }, [notifyClipEnded]);

  const handleSeeking = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;
    if (video.currentTime < 0) {
      video.currentTime = 0;
    }
    if (Number.isFinite(video.duration) && video.currentTime > video.duration) {
      video.currentTime = video.duration;
    }
  }, []);

  const handleRetry = useCallback(() => {
    setRetryCount((value) => value + 1);
    setHasError(false);
    setIsLoaded(false);
    setPreloadOverride("auto");
  }, []);

  return (
    <div className={cn("relative bg-black", className)}>
      {shouldLoad && !isLoaded && !hasError && (
        <div className="absolute inset-0 z-10 flex items-center justify-center bg-black/80">
          <Loader2 className="h-7 w-7 animate-spin text-white" />
        </div>
      )}

      {hasError && (
        <div className="absolute inset-0 z-20 flex items-center justify-center bg-black/80">
          <div className="flex flex-col items-center gap-2 text-white">
            <span className="text-xs">Failed to load clip</span>
            <button
              className="rounded bg-white/20 px-2 py-1 text-xs transition-colors hover:bg-white/30"
              onClick={handleRetry}
            >
              Retry
            </button>
          </div>
        </div>
      )}

      {isSourceAttached ? (
        <video
          key={`${src}-${retryCount}`}
          ref={videoRef}
          src={videoSrc}
          className={cn(
            "h-full w-full object-contain",
            disableInteraction && "pointer-events-none",
          )}
          onLoadStart={handleLoadStart}
          onLoadedMetadata={handleLoadedMetadata}
          onCanPlay={() => setIsLoaded(true)}
          onError={handleError}
          onEnded={handleEnded}
          onSeeking={handleSeeking}
          muted={muted}
          controls={controls}
          playsInline
          preload={effectivePreload === "none" ? "metadata" : effectivePreload}
        />
      ) : (
        <div className="h-full w-full bg-black" />
      )}
    </div>
  );
});
