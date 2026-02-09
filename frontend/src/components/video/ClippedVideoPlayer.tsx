import {
  useRef,
  useEffect,
  useCallback,
  useState,
  forwardRef,
  useImperativeHandle,
  useMemo,
} from "react";
import { RotateCcw, Play, Loader2 } from "lucide-react";
import { cn } from "@/utils";

interface ClippedVideoPlayerProps {
  src: string;
  startTime: number;
  endTime: number;
  className?: string;
  muted?: boolean;
  playbackRate?: number;
  onClipEnded?: () => void;
}

export interface ClippedVideoPlayerHandle {
  playFromStart: () => void;
  seekToStart: () => Promise<void>;
  play: () => void;
  pause: () => void;
  waitUntilReady: () => Promise<void>;
  forceLoad: () => void;
  releaseLoad: () => void;
}

/**
 * A video player that strictly clips playback between startTime and endTime.
 * Uses Intersection Observer for lazy loading to prevent loading too many videos at once.
 * - Only loads video when visible in viewport
 * - Starts at startTime when loaded
 * - Pauses at endTime
 * - Allows manual replay from startTime
 */
export const ClippedVideoPlayer = forwardRef<
  ClippedVideoPlayerHandle,
  ClippedVideoPlayerProps
>(function ClippedVideoPlayer(
  { src, startTime, endTime, className, muted = true, playbackRate = 1, onClipEnded },
  ref,
) {
  const containerRef = useRef<HTMLDivElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const readyResolversRef = useRef<Array<() => void>>([]);
  const endNotifiedRef = useRef(false);
  const forceLoadedRef = useRef(false);
  const isIntersectingRef = useRef(false);
  const [isVisible, setIsVisible] = useState(false);
  const [isLoaded, setIsLoaded] = useState(false);
  const [isEnded, setIsEnded] = useState(false);
  const [hasError, setHasError] = useState(false);
  const [retryCount, setRetryCount] = useState(0);
  const [isRetrying, setIsRetrying] = useState(false);

  const resolveReadyWaiters = useCallback(() => {
    const waiters = readyResolversRef.current;
    if (waiters.length === 0) return;
    readyResolversRef.current = [];
    waiters.forEach((resolve) => resolve());
  }, []);

  // Add a small offset to startTime to compensate for keyframe seeking
  // HTML video seeks to nearest keyframe which can be before the target time
  // Adding ~2 frames (at 30fps = 0.067s) ensures we land on correct frame
  const adjustedStartTime = startTime + 0.067;

  // Add cache-busting parameter on retry to bypass browser cache
  const videoSrc = useMemo(() => {
    if (retryCount === 0) return src;
    const separator = src.includes("?") ? "&" : "?";
    // Use retryCount as the cache buster (changes on each retry)
    return `${src}${separator}_retry=${retryCount}`;
  }, [src, retryCount]);

  // Expose playback control methods to parent
  useImperativeHandle(
    ref,
    () => ({
      playFromStart: () => {
        if (videoRef.current) {
          videoRef.current.currentTime = adjustedStartTime;
          endNotifiedRef.current = false;
          setIsEnded(false);
          videoRef.current.play().catch(console.error);
        }
      },
      seekToStart: () => {
        return new Promise<void>((resolve) => {
          const video = videoRef.current;
          if (!video) {
            resolve();
            return;
          }
          video.currentTime = adjustedStartTime;
          endNotifiedRef.current = false;
          setIsEnded(false);
          const onSeeked = () => {
            video.removeEventListener("seeked", onSeeked);
            resolve();
          };
          video.addEventListener("seeked", onSeeked);
        });
      },
      play: () => {
        if (videoRef.current) {
          endNotifiedRef.current = false;
          setIsEnded(false);
          videoRef.current.play().catch(console.error);
        }
      },
      pause: () => {
        videoRef.current?.pause();
      },
      waitUntilReady: () => {
        return new Promise<void>((resolve) => {
          const video = videoRef.current;
          if (video && isLoaded) {
            resolve();
            return;
          }

          readyResolversRef.current.push(resolve);
          // Avoid deadlocks if loading fails or takes too long.
          setTimeout(() => {
            const idx = readyResolversRef.current.indexOf(resolve);
            if (idx >= 0) {
              readyResolversRef.current.splice(idx, 1);
              resolve();
            }
          }, 4000);
        });
      },
      forceLoad: () => {
        forceLoadedRef.current = true;
        setIsVisible(true);
      },
      releaseLoad: () => {
        forceLoadedRef.current = false;
        if (isIntersectingRef.current) return;
        setIsVisible(false);
        setIsLoaded(false);
        setHasError(false);
        setIsEnded(false);
        endNotifiedRef.current = false;
      },
    }),
    [adjustedStartTime, isLoaded],
  );

  // Intersection Observer for lazy loading AND unloading
  // Videos are loaded when entering viewport and unloaded when leaving
  // This prevents too many concurrent video connections which causes failures
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          isIntersectingRef.current = entry.isIntersecting;
          if (entry.isIntersecting) {
            // Video is entering viewport - load it
            setIsVisible(true);
          } else {
            if (forceLoadedRef.current) {
              return;
            }
            // Video is leaving viewport - unload it to free connections
            setIsVisible(false);
            setIsLoaded(false);
            setHasError(false);
            setIsEnded(false);
            endNotifiedRef.current = false;
            // Don't reset retryCount - keep it so next load uses fresh URL if needed
          }
        });
      },
      {
        rootMargin: "200px", // Load slightly before visible, unload after leaving by 200px
        threshold: 0,
      },
    );

    observer.observe(container);

    return () => {
      observer.disconnect();
    };
  }, []);

  // Set initial time when video loads
  const handleLoadedMetadata = useCallback(() => {
    if (videoRef.current) {
      videoRef.current.currentTime = adjustedStartTime;
      videoRef.current.playbackRate = playbackRate;
      setIsLoaded(true);
      setHasError(false);
      resolveReadyWaiters();
    }
  }, [adjustedStartTime, playbackRate, resolveReadyWaiters]);

  const handleError = useCallback(
    (e: React.SyntheticEvent<HTMLVideoElement>) => {
      const video = e.currentTarget;
      const errorCode = video.error?.code;
      const errorMessage = video.error?.message || "Unknown error";
      console.error(
        `Video load error for ${src}: code=${errorCode}, message=${errorMessage}`,
      );
      setHasError(true);
      setIsLoaded(true); // Stop showing loader
      resolveReadyWaiters();
    },
    [src, resolveReadyWaiters],
  );

  // Monitor playback to enforce end boundary only.
  // Start boundary is handled by handleSeeked (user scrubbing) — not needed
  // during normal playback since video always moves forward.
  const handleTimeUpdate = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;

    if (video.currentTime >= endTime) {
      video.pause();
      video.currentTime = endTime;
      setIsEnded(true);
      if (!endNotifiedRef.current) {
        endNotifiedRef.current = true;
        onClipEnded?.();
      }
    }
  }, [endTime, onClipEnded]);

  const handlePlay = useCallback(() => {
    // Only re-seek if clearly out of bounds (with tolerance to avoid
    // floating-point cascading seeks that cause visible stutter)
    if (videoRef.current) {
      videoRef.current.playbackRate = playbackRate;
      const cur = videoRef.current.currentTime;
      if (cur < adjustedStartTime - 0.15 || cur >= endTime) {
        videoRef.current.currentTime = adjustedStartTime;
      }
      endNotifiedRef.current = false;
      setIsEnded(false);
    }
  }, [adjustedStartTime, endTime, playbackRate]);

  const handleSeeked = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;

    // Use tolerance to prevent cascading re-seek loops from floating-point
    // imprecision. Only clamp if genuinely out of bounds.
    if (video.currentTime < adjustedStartTime - 0.15) {
      video.currentTime = adjustedStartTime;
      endNotifiedRef.current = false;
    }
    if (video.currentTime >= endTime) {
      video.currentTime = endTime;
      video.pause();
      setIsEnded(true);
      if (!endNotifiedRef.current) {
        endNotifiedRef.current = true;
        onClipEnded?.();
      }
    }
  }, [adjustedStartTime, endTime, onClipEnded]);

  // Reset to start when src or times change
  useEffect(() => {
    if (videoRef.current && isLoaded) {
      videoRef.current.currentTime = adjustedStartTime;
      videoRef.current.playbackRate = playbackRate;
      endNotifiedRef.current = false;
    }
  }, [src, adjustedStartTime, isLoaded, playbackRate]);

  const handleReplay = useCallback(() => {
    if (videoRef.current) {
      videoRef.current.currentTime = adjustedStartTime;
      videoRef.current.playbackRate = playbackRate;
      endNotifiedRef.current = false;
      setIsEnded(false);
      videoRef.current.play().catch(console.error);
    }
  }, [adjustedStartTime, playbackRate]);

  const handleRetry = useCallback(() => {
    // Set retrying state to force complete unmount
    setIsRetrying(true);
    setHasError(false);
    setIsLoaded(false);

    // Small delay to ensure video element is unmounted before creating new one
    setTimeout(() => {
      setRetryCount((c) => c + 1);
      setIsRetrying(false);
    }, 100);
  }, []);

  return (
    <div
      ref={containerRef}
      className={cn("relative group bg-black", className)}
    >
      {/* Loading placeholder before video is visible */}
      {!isVisible && (
        <div className="absolute inset-0 flex items-center justify-center bg-[hsl(var(--muted))]">
          <div className="flex flex-col items-center gap-2 text-[hsl(var(--muted-foreground))]">
            <Play className="h-8 w-8" />
            <span className="text-xs">Scroll to load</span>
          </div>
        </div>
      )}

      {/* Loading indicator while video is loading */}
      {isVisible && !isLoaded && !hasError && !isRetrying && (
        <div className="absolute inset-0 flex items-center justify-center bg-black/50 z-10">
          <Loader2 className="h-8 w-8 animate-spin text-white" />
        </div>
      )}

      {/* Retrying state */}
      {isRetrying && (
        <div className="absolute inset-0 flex items-center justify-center bg-black/50 z-10">
          <Loader2 className="h-8 w-8 animate-spin text-white" />
        </div>
      )}

      {/* Error state with retry button */}
      {hasError && !isRetrying && (
        <div className="absolute inset-0 flex items-center justify-center bg-[hsl(var(--muted))] z-10">
          <div className="flex flex-col items-center gap-2 text-[hsl(var(--muted-foreground))]">
            <span className="text-xs">Failed to load</span>
            <button
              onClick={handleRetry}
              className="text-xs px-2 py-1 bg-[hsl(var(--accent))] hover:bg-[hsl(var(--accent))]/80 rounded transition-colors"
            >
              Retry
            </button>
          </div>
        </div>
      )}

      {/* Video element - only render when visible and not retrying */}
      {isVisible && !isRetrying && (
        <video
          key={`${src}-${retryCount}`}
          ref={videoRef}
          src={videoSrc}
          className="w-full h-full object-contain"
          onLoadedMetadata={handleLoadedMetadata}
          onError={handleError}
          onTimeUpdate={handleTimeUpdate}
          onPlay={handlePlay}
          onSeeked={handleSeeked}
          controls
          muted={muted}
          preload="metadata"
        />
      )}

      {/* Replay overlay when ended */}
      {isEnded && (
        <div
          className="absolute inset-0 bg-black/50 flex items-center justify-center cursor-pointer z-20"
          onClick={handleReplay}
        >
          <div className="flex flex-col items-center gap-2 text-white">
            <RotateCcw className="h-10 w-10" />
            <span className="text-sm">Replay</span>
          </div>
        </div>
      )}
    </div>
  );
});
