import { useRef, useEffect, useCallback, useState, forwardRef, useImperativeHandle, useMemo } from 'react';
import { RotateCcw, Play, Loader2 } from 'lucide-react';
import { cn } from '@/utils';

interface ClippedVideoPlayerProps {
  src: string;
  startTime: number;
  endTime: number;
  className?: string;
}

export interface ClippedVideoPlayerHandle {
  playFromStart: () => void;
}

/**
 * A video player that strictly clips playback between startTime and endTime.
 * Uses Intersection Observer for lazy loading to prevent loading too many videos at once.
 * - Only loads video when visible in viewport
 * - Starts at startTime when loaded
 * - Pauses at endTime
 * - Allows manual replay from startTime
 */
export const ClippedVideoPlayer = forwardRef<ClippedVideoPlayerHandle, ClippedVideoPlayerProps>(
  function ClippedVideoPlayer({ src, startTime, endTime, className }, ref) {
    const containerRef = useRef<HTMLDivElement>(null);
    const videoRef = useRef<HTMLVideoElement>(null);
    const [isVisible, setIsVisible] = useState(false);
    const [isLoaded, setIsLoaded] = useState(false);
    const [isEnded, setIsEnded] = useState(false);
    const [hasError, setHasError] = useState(false);
    const [retryCount, setRetryCount] = useState(0);
    const [isRetrying, setIsRetrying] = useState(false);

    // Add cache-busting parameter on retry to bypass browser cache
    const videoSrc = useMemo(() => {
      if (retryCount === 0) return src;
      const separator = src.includes('?') ? '&' : '?';
      // Use retryCount as the cache buster (changes on each retry)
      return `${src}${separator}_retry=${retryCount}`;
    }, [src, retryCount]);

    // Expose playFromStart method to parent
    useImperativeHandle(ref, () => ({
      playFromStart: () => {
        if (videoRef.current) {
          videoRef.current.currentTime = startTime;
          setIsEnded(false);
          videoRef.current.play().catch(console.error);
        }
      }
    }), [startTime]);

    // Intersection Observer for lazy loading AND unloading
    // Videos are loaded when entering viewport and unloaded when leaving
    // This prevents too many concurrent video connections which causes failures
    useEffect(() => {
      const container = containerRef.current;
      if (!container) return;

      const observer = new IntersectionObserver(
        (entries) => {
          entries.forEach((entry) => {
            if (entry.isIntersecting) {
              // Video is entering viewport - load it
              setIsVisible(true);
            } else {
              // Video is leaving viewport - unload it to free connections
              setIsVisible(false);
              setIsLoaded(false);
              setHasError(false);
              setIsEnded(false);
              // Don't reset retryCount - keep it so next load uses fresh URL if needed
            }
          });
        },
        {
          rootMargin: '200px', // Load slightly before visible, unload after leaving by 200px
          threshold: 0,
        }
      );

      observer.observe(container);

      return () => {
        observer.disconnect();
      };
    }, []);

    // Set initial time when video loads
    const handleLoadedMetadata = useCallback(() => {
      if (videoRef.current) {
        videoRef.current.currentTime = startTime;
        setIsLoaded(true);
        setHasError(false);
      }
    }, [startTime]);

    const handleError = useCallback((e: React.SyntheticEvent<HTMLVideoElement>) => {
      const video = e.currentTarget;
      const errorCode = video.error?.code;
      const errorMessage = video.error?.message || 'Unknown error';
      console.error(`Video load error for ${src}: code=${errorCode}, message=${errorMessage}`);
      setHasError(true);
      setIsLoaded(true); // Stop showing loader
    }, [src]);

    // Monitor playback to enforce end boundary - use onTimeUpdate prop instead of effect
    const handleTimeUpdate = useCallback(() => {
      const video = videoRef.current;
      if (!video) return;
      
      if (video.currentTime >= endTime) {
        video.pause();
        video.currentTime = endTime;
        setIsEnded(true);
      }
      // Also check start boundary
      if (video.currentTime < startTime) {
        video.currentTime = startTime;
      }
    }, [startTime, endTime]);

    const handlePlay = useCallback(() => {
      // When play is triggered, ensure we're within bounds
      if (videoRef.current) {
        if (videoRef.current.currentTime < startTime || videoRef.current.currentTime >= endTime) {
          videoRef.current.currentTime = startTime;
        }
        setIsEnded(false);
      }
    }, [startTime, endTime]);

    const handleSeeked = useCallback(() => {
      const video = videoRef.current;
      if (!video) return;
      
      // If user seeks before start, reset to start
      if (video.currentTime < startTime) {
        video.currentTime = startTime;
      }
      // If user seeks past end, set to end and show ended state
      if (video.currentTime >= endTime) {
        video.currentTime = endTime;
        video.pause();
        setIsEnded(true);
      }
    }, [startTime, endTime]);

    // Reset to start when src or times change
    useEffect(() => {
      if (videoRef.current && isLoaded) {
        videoRef.current.currentTime = startTime;
        setIsEnded(false);
      }
    }, [src, startTime, isLoaded]);

    const handleReplay = useCallback(() => {
      if (videoRef.current) {
        videoRef.current.currentTime = startTime;
        setIsEnded(false);
        videoRef.current.play().catch(console.error);
      }
    }, [startTime]);

    const handleRetry = useCallback(() => {
      // Set retrying state to force complete unmount
      setIsRetrying(true);
      setHasError(false);
      setIsLoaded(false);
      
      // Small delay to ensure video element is unmounted before creating new one
      setTimeout(() => {
        setRetryCount(c => c + 1);
        setIsRetrying(false);
      }, 100);
    }, []);

    return (
      <div ref={containerRef} className={cn('relative group bg-black', className)}>
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
            muted
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
  }
);
